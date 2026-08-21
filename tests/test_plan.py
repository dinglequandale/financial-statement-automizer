"""Unit tests for fsa.write.plan -- row allocation and formula generation.

Pure: no Excel, no sample files. The formula cases are transcribed verbatim
from the delivered TS Distributors model, so a regression here means we would
produce formulas an analyst would not recognize.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fsa.mapping.template import TemplateBlock, TemplateLine, TemplateSpec
from fsa.model.mapping import (
    Decider,
    MappingRule,
    MappingSet,
    Target,
    TargetKind,
)
from fsa.model.schema import StatementType
from fsa.write.plan import PlanError, build_formula, build_plan

BS = StatementType.BS


def _line(row, label, **kw):
    return TemplateLine(
        statement=BS,
        row=row,
        label=label,
        frozen=kw.get("frozen", False),
        derived=kw.get("derived", False),
        collapsible=kw.get("collapsible", False),
        placeholder=kw.get("placeholder", False),
        block=kw.get("block"),
    )


def _spec() -> TemplateSpec:
    """A miniature fixed-assets block: 3 named lines, one of them frozen."""
    lines = [
        _line(16, "Computers and Software", block="Total Fixed Assets"),
        _line(17, "Furniture and Fixtures", block="Total Fixed Assets"),
        _line(18, "Land and Buildings", block="Total Fixed Assets"),
        _line(19, "Frozen Line", block="Total Fixed Assets", frozen=True),
        _line(20, "Total Fixed Assets", derived=True, collapsible=True),
    ]
    blocks = [
        TemplateBlock(
            statement=BS,
            subtotal_label="Total Fixed Assets",
            subtotal_row=20,
            first_row=16,
            last_row=19,
        )
    ]
    return TemplateSpec(source=Path("x.xlsx"), lines=lines, blocks=blocks)


def _rule(account, label, *, kind=TargetKind.ASSIGN, block=None, slot=None, sign=1):
    return MappingRule(
        client_account=account,
        norm_account=account.casefold(),
        target=Target(statement=BS, label=label, kind=kind, block=block, slot_label=slot),
        sign=sign,
        decided_by=Decider.HUMAN,
        confidence=1.0,
    )


# --- formula generation (verbatim from the delivered model) --------------


@pytest.mark.parametrize(
    "sources,expected",
    [
        ([(18, 1), (19, 1), (20, 1), (21, 1), (27, 1)],
         "=SUM('Historical BS'!G18:G21,'Historical BS'!G27)"),
        ([(8, 1), (9, 1)], "=SUM('Historical BS'!G8:G9)"),
        ([(41, 1)], "=SUM('Historical BS'!G41)"),
        ([(135, -1)], "=-SUM('Historical BS'!G135)"),
    ],
)
def test_formula_matches_house_style(sources, expected):
    assert build_formula("Historical BS", "G", sources) == expected


def test_formula_separates_contra_sources():
    """Mirrors IS!F34: positives summed, negatives subtracted."""
    got = build_formula("Historical IS", "Z", [(16, 1), (126, 1), (136, -1), (137, -1)])
    assert got == "=SUM('Historical IS'!Z16,'Historical IS'!Z126)-SUM('Historical IS'!Z136:Z137)"


def test_formula_empty_sources_is_empty():
    assert build_formula("Historical BS", "G", []) == ""


# --- row allocation ------------------------------------------------------


def test_existing_line_keeps_its_row():
    ms = MappingSet(statement=BS, rules=[_rule("Furniture & Fixtures", "Furniture and Fixtures")])
    plan = build_plan(ms, _spec(), {"furniture & fixtures": 39})
    a = plan.action_for("Furniture and Fixtures")
    assert a is not None and a.kind == "existing" and a.row == 17


def test_unused_named_line_is_repurposed_not_inserted():
    """The analyst turned `Land and Buildings` into `Vehicles` rather than
    inserting a row. Repurposing a free line must be preferred over inserting."""
    ms = MappingSet(
        statement=BS,
        rules=[_rule("Vehicles", "Vehicles", kind=TargetKind.INSERT, block="Total Fixed Assets")],
    )
    plan = build_plan(ms, _spec(), {"vehicles": 38})
    a = plan.action_for("Vehicles")
    assert a is not None
    assert a.kind == "repurpose", f"expected repurpose, got {a.kind}"
    assert a.old_label in {"Computers and Software", "Furniture and Fixtures", "Land and Buildings"}
    assert plan.inserts == []


def test_insert_only_once_the_block_is_full():
    """Three targets for three free lines, then a fourth must insert."""
    ms = MappingSet(
        statement=BS,
        rules=[
            _rule(f"acct{i}", f"New Line {i}", kind=TargetKind.INSERT, block="Total Fixed Assets")
            for i in range(4)
        ],
    )
    plan = build_plan(ms, _spec(), {f"acct{i}": 30 + i for i in range(4)})
    kinds = sorted(a.kind for a in plan.actions)
    assert kinds.count("repurpose") == 3
    assert kinds.count("insert") == 1


def test_frozen_line_is_never_repurposed():
    ms = MappingSet(
        statement=BS,
        rules=[
            _rule(f"acct{i}", f"New Line {i}", kind=TargetKind.INSERT, block="Total Fixed Assets")
            for i in range(4)
        ],
    )
    plan = build_plan(ms, _spec(), {f"acct{i}": 30 + i for i in range(4)})
    assert all(a.old_label != "Frozen Line" for a in plan.actions)
    assert all(a.row != 19 for a in plan.actions)


def test_unconfirmed_rules_are_refused_by_default():
    """An LLM proposal must never reach a delivered model unreviewed."""
    r = _rule("Vehicles", "Furniture and Fixtures")
    r.decided_by = Decider.LLM
    ms = MappingSet(statement=BS, rules=[r])
    with pytest.raises(PlanError):
        build_plan(ms, _spec(), {"vehicles": 38})


def test_sources_carry_sign_and_are_row_sorted():
    ms = MappingSet(
        statement=BS,
        rules=[
            _rule("b", "Furniture and Fixtures"),
            _rule("a", "Furniture and Fixtures", sign=-1),
        ],
    )
    plan = build_plan(ms, _spec(), {"a": 50, "b": 12})
    assert plan.sources["Furniture and Fixtures"] == [(12, 1), (50, -1)]


def test_target_without_a_block_is_skipped_not_guessed():
    """An inserted line with no block has no defined position -- refuse it
    rather than placing it somewhere arbitrary."""
    r = _rule("Mystery", "Mystery Line")
    object.__setattr__(r.target, "kind", TargetKind.ASSIGN)
    ms = MappingSet(statement=BS, rules=[r, _rule("ok", "Furniture and Fixtures")])
    plan = build_plan(ms, _spec(), {"mystery": 60, "ok": 12})
    assert plan.action_for("Mystery Line") is None
    assert "Mystery Line" not in plan.sources
    assert any("no block" in w for w in plan.warnings)


def test_insert_lands_inside_the_block_sum_range():
    """Regression: an inserted row must fall INSIDE its subtotal's SUM range.

    Excel widens `=SUM(E9:E13)` only when the insertion point is strictly
    inside it. Planning an insert at `last_row + 1` leaves the formula
    unchanged, pushes the subtotal down, and orphans the new row outside its
    own total -- the value is written, looks correct on screen, and reaches no
    total. End-to-end verification caught exactly this: an inserted
    `Employee Advances` row was excluded from Total Current Assets in all five
    years, and the balance check was off by that account every year.
    """
    spec = _spec()
    blk = spec.blocks[0]  # rows 16-19, subtotal at 20
    ms = MappingSet(
        statement=BS,
        rules=[
            _rule(f"acct{i}", f"New Line {i}", kind=TargetKind.INSERT, block="Total Fixed Assets")
            for i in range(4)
        ],
    )
    plan = build_plan(ms, spec, {f"acct{i}": 30 + i for i in range(4)})
    ins = plan.inserts
    assert len(ins) == 1
    row = ins[0].row
    assert blk.first_row <= row <= blk.last_row, (
        f"insert planned at row {row}, outside the summed range "
        f"{blk.first_row}-{blk.last_row}; Excel would orphan it"
    )
    assert row != blk.subtotal_row, "must never insert onto the subtotal row itself"
