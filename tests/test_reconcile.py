"""Tests for the exactly-once invariant."""

from __future__ import annotations

from pathlib import Path

import pytest

from fsa.mapping.reconcile import coverage, descendants, reconcile
from fsa.model.mapping import Decider, MappingRule, MappingSet, Target, TargetKind
from fsa.model.schema import (
    ConsolidatedRow,
    ConsolidatedTable,
    RowKind,
    Severity,
    StatementType,
)

BS = StatementType.BS


class _Line:
    def __init__(self, label, placeholder=False, block=None):
        self.label = label
        self.placeholder = placeholder
        self.block = block


class _Spec:
    """Minimal stand-in for TemplateSpec: only `find` is used here."""

    def __init__(self, lines):
        self._lines = {l.label.lower(): l for l in lines}

    def find(self, _statement, label):
        return self._lines.get((label or "").lower())


SPEC = _Spec(
    [
        _Line("Accounts Receivable"),
        _Line("Cash and Cash Equivalents"),
        _Line("Investments"),
        _Line("Operating Expenses"),
        _Line("Other Income (Expense) 2", placeholder=True, block="Pre-Tax Income"),
    ]
)


def _row(label, kind, depth, section=None, value=1.0):
    return ConsolidatedRow(
        raw_label=label,
        norm_label=label.lower(),
        kind=kind,
        section=section,
        values={2025: value},
        depth=depth,
    )


def _table(rows):
    return ConsolidatedTable(statement=BS, years=[2025], rows=rows)


def _rule(account, target_label, sign=1, kind=TargetKind.ASSIGN, decided_by=Decider.ALIAS):
    return MappingRule(
        client_account=account,
        norm_account=account.lower(),
        target=Target(statement=BS, label=target_label, kind=kind),
        decided_by=decided_by,
        confidence=0.9,
        sign=sign,
    )


# --- hierarchy -------------------------------------------------------------


def test_a_rollup_owns_the_deeper_rows_above_it():
    """QuickBooks prints the total after its members."""
    rows = [
        _row("Cash", RowKind.DATA, 2),
        _row("Savings", RowKind.DATA, 2),
        _row("Total Checking", RowKind.SUBTOTAL, 1),
        _row("Equipment", RowKind.DATA, 2),
    ]
    kids = descendants(_table(rows), 2)
    assert {k.raw_label for k in kids} == {"Cash", "Savings"}


def test_a_rollup_does_not_reach_past_a_shallower_row():
    rows = [
        _row("Land", RowKind.DATA, 1),
        _row("Cash", RowKind.DATA, 2),
        _row("Total Checking", RowKind.SUBTOTAL, 1),
    ]
    kids = descendants(_table(rows), 2)
    assert {k.raw_label for k in kids} == {"Cash"}


def test_hierarchy_falls_back_to_sections_without_depth():
    rows = [
        _row("Cash", RowKind.DATA, None, section="Current Assets"),
        _row("Equipment", RowKind.DATA, None, section="Fixed Assets"),
        _row("Total Current Assets", RowKind.SUBTOTAL, None, section="Current Assets"),
    ]
    kids = descendants(_table(rows), 2)
    assert {k.raw_label for k in kids} == {"Cash"}


# --- double counting -------------------------------------------------------


def test_rollup_and_its_detail_both_mapped_is_an_error():
    """The exact shape sample 2 produced: alias took the child, LLM the parent."""
    rows = [
        _row("Accounts Receivable", RowKind.DATA, 2),
        _row("Total Accounts Receivable", RowKind.SUBTOTAL, 1),
    ]
    ms = MappingSet(statement=BS)
    ms.rules.append(_rule("Accounts Receivable", "Accounts Receivable"))
    ms.rules.append(
        _rule("Total Accounts Receivable", "Accounts Receivable", decided_by=Decider.LLM)
    )

    rep = reconcile(ms, _table(rows), SPEC)
    assert rep.conflicts == 1
    f = next(f for f in rep.findings if f.code == "double_count")
    assert f.severity is Severity.ERROR
    assert f.account == "Accounts Receivable"


def test_opposite_signs_are_a_deliberate_subtraction_not_a_conflict():
    """The delivered TS model maps a rollup then subtracts a component."""
    rows = [
        _row("Cash", RowKind.DATA, 2),
        _row("Total Current Assets", RowKind.SUBTOTAL, 1),
    ]
    ms = MappingSet(statement=BS)
    ms.rules.append(_rule("Cash", "Cash and Cash Equivalents", sign=-1))
    ms.rules.append(_rule("Total Current Assets", "Cash and Cash Equivalents", sign=1))

    rep = reconcile(ms, _table(rows), SPEC)
    assert rep.conflicts == 0


def test_an_unmapped_rollup_creates_no_conflict_and_is_itself_covered():
    """Mapping the detail settles the rollup -- it is not an open question.

    Leaving it merely unresolved put `Total Accounts Receivable` in the review
    sheet as something for the analyst to map, and mapping it is precisely the
    double-count this module prevents. So it is covered, explicitly.
    """
    rows = [
        _row("Accounts Receivable", RowKind.DATA, 2),
        _row("Total Accounts Receivable", RowKind.SUBTOTAL, 1),
    ]
    ms = MappingSet(statement=BS)
    ms.rules.append(_rule("Accounts Receivable", "Accounts Receivable"))
    rep = reconcile(ms, _table(rows), SPEC)
    assert rep.conflicts == 0
    assert rep.covered == 1
    assert [e.norm_account for e in ms.exclusions] == ["total accounts receivable"]


def test_a_rollup_whose_members_are_all_unmapped_is_left_alone():
    """Nothing covers it, so excluding it would silently drop the money."""
    rows = [
        _row("Accounts Receivable", RowKind.DATA, 2),
        _row("Total Accounts Receivable", RowKind.SUBTOTAL, 1),
    ]
    ms = MappingSet(statement=BS)
    rep = reconcile(ms, _table(rows), SPEC)
    assert rep.covered == 0
    assert ms.exclusions == []
    assert "Accounts Receivable" in rep.uncovered


def test_a_partially_mapped_rollup_is_not_treated_as_covered():
    rows = [
        _row("Checking", RowKind.DATA, 2),
        _row("Savings", RowKind.DATA, 2),
        _row("Total Checking/Savings", RowKind.SUBTOTAL, 1),
    ]
    ms = MappingSet(statement=BS)
    ms.rules.append(_rule("Checking", "Cash and Cash Equivalents"))
    rep = reconcile(ms, _table(rows), SPEC)
    # `Savings` never reaches the model, so the total is not represented either.
    assert "total checking/savings" not in {e.norm_account for e in ms.exclusions}
    assert "Savings" in rep.uncovered


# --- coverage --------------------------------------------------------------


def test_mapping_a_rollup_covers_its_details_instead_of_stranding_them():
    rows = [
        _row("Rent", RowKind.DATA, 2),
        _row("Utilities", RowKind.DATA, 2),
        _row("Total Expense", RowKind.SUBTOTAL, 1),
    ]
    ms = MappingSet(statement=BS)
    ms.rules.append(_rule("Total Expense", "Operating Expenses", decided_by=Decider.LLM))

    rep = reconcile(ms, _table(rows), SPEC)
    assert rep.covered == 2
    reasons = {e.norm_account: e.reason for e in ms.exclusions}
    assert "Total Expense" in reasons["rent"]
    assert rep.uncovered == []


def test_coverage_counts_rollup_covered_rows_not_just_rows_with_a_rule():
    """Counting rules alone understates the income statement badly."""
    rows = [
        _row("Rent", RowKind.DATA, 2),
        _row("Utilities", RowKind.DATA, 2),
        _row("Total Expense", RowKind.SUBTOTAL, 1),
    ]
    table = _table(rows)
    ms = MappingSet(statement=BS)
    ms.rules.append(_rule("Total Expense", "Operating Expenses", decided_by=Decider.LLM))
    assert coverage(ms, table) == (0, 2)  # before reconcile: nothing accounted
    reconcile(ms, table, SPEC)
    assert coverage(ms, table) == (2, 2)  # after: both covered by the rollup


def test_an_account_reaching_nothing_is_reported():
    rows = [_row("Mystery Account", RowKind.DATA, 1)]
    ms = MappingSet(statement=BS)
    rep = reconcile(ms, _table(rows), SPEC)
    assert rep.uncovered == ["Mystery Account"]
    f = next(f for f in rep.findings if f.code == "uncovered_accounts")
    assert f.severity is Severity.WARNING


# --- placeholders ----------------------------------------------------------


def test_assigning_to_a_placeholder_becomes_a_rename():
    """Otherwise the model ships with the template's filler text as a line."""
    rows = [_row("Dividend Income", RowKind.DATA, 1)]
    ms = MappingSet(statement=BS)
    ms.rules.append(
        _rule("Dividend Income", "Other Income (Expense) 2", decided_by=Decider.LLM)
    )

    rep = reconcile(ms, _table(rows), SPEC)
    assert rep.renamed_slots == 1
    t = ms.rules[0].target
    assert t.kind is TargetKind.NAME_SLOT
    assert t.label == "Dividend Income"
    assert t.slot_label == "Other Income (Expense) 2"
    assert t.block == "Pre-Tax Income"


def test_a_real_named_line_is_left_alone():
    rows = [_row("Cash", RowKind.DATA, 1)]
    ms = MappingSet(statement=BS)
    ms.rules.append(_rule("Cash", "Cash and Cash Equivalents"))
    rep = reconcile(ms, _table(rows), SPEC)
    assert rep.renamed_slots == 0
    assert ms.rules[0].target.kind is TargetKind.ASSIGN


def _mapped_targets(ms):
    return {r.client_account: r.target.label for r in ms.rules}


def test_conflict_keeps_the_rollup_when_the_detail_is_incomplete():
    """Only the rollup is guaranteed to cover every member, so the total is
    right even though the detail's placement is lost."""
    rows = [
        _row("Cash", RowKind.DATA, 2),
        _row("Petty Cash", RowKind.DATA, 2),  # never mapped
        _row("Total Cash", RowKind.SUBTOTAL, 1),
    ]
    ms = MappingSet(statement=BS)
    ms.rules.append(_rule("Cash", "Investments"))
    ms.rules.append(_rule("Total Cash", "Cash and Cash Equivalents", decided_by=Decider.LLM))

    rep = reconcile(ms, _table(rows), SPEC)
    assert rep.conflicts == 1
    assert _mapped_targets(ms) == {"Total Cash": "Cash and Cash Equivalents"}
    # The losing side is excluded, not deleted -- visible and reversible.
    assert {e.norm_account for e in ms.exclusions} == {"cash", "petty cash"}
    assert "double-count" in next(e for e in ms.exclusions if e.norm_account == "cash").reason


def test_conflict_keeps_the_detail_when_it_covers_the_whole_group():
    """Every member is mapped, so the finer breakdown is safe to keep."""
    rows = [
        _row("Cash", RowKind.DATA, 2),
        _row("Petty Cash", RowKind.DATA, 2),
        _row("Total Cash", RowKind.SUBTOTAL, 1),
    ]
    ms = MappingSet(statement=BS)
    ms.rules.append(_rule("Cash", "Cash and Cash Equivalents"))
    ms.rules.append(_rule("Petty Cash", "Cash and Cash Equivalents"))
    ms.rules.append(_rule("Total Cash", "Cash and Cash Equivalents", decided_by=Decider.LLM))

    rep = reconcile(ms, _table(rows), SPEC)
    assert rep.conflicts == 2
    assert _mapped_targets(ms) == {
        "Cash": "Cash and Cash Equivalents",
        "Petty Cash": "Cash and Cash Equivalents",
    }
    assert [e.norm_account for e in ms.exclusions] == ["total cash"]


def test_every_account_reaches_the_model_exactly_once_after_a_conflict():
    """The invariant, stated as arithmetic rather than as bookkeeping."""
    rows = [
        _row("Cash", RowKind.DATA, 2, value=40.0),
        _row("Petty Cash", RowKind.DATA, 2, value=10.0),
        _row("Total Cash", RowKind.SUBTOTAL, 1, value=50.0),
    ]
    table = _table(rows)
    ms = MappingSet(statement=BS)
    ms.rules.append(_rule("Cash", "Cash and Cash Equivalents"))
    ms.rules.append(_rule("Total Cash", "Cash and Cash Equivalents", decided_by=Decider.LLM))
    reconcile(ms, table, SPEC)

    total = 0.0
    for r in ms.rules:
        if r.decided_by is not Decider.UNRESOLVED:
            total += r.sign * (table.find(r.norm_account).values[2025] or 0.0)
    assert total == 50.0  # not 90.0, which is what writing both produced


def test_opposite_signs_survive_untouched():
    """`rollup minus component` is the analyst's own construction (PLAN 7.4)."""
    rows = [
        _row("Cash", RowKind.DATA, 2),
        _row("Total Cash", RowKind.SUBTOTAL, 1),
    ]
    ms = MappingSet(statement=BS)
    ms.rules.append(_rule("Cash", "Cash and Cash Equivalents", sign=-1))
    ms.rules.append(_rule("Total Cash", "Cash and Cash Equivalents", sign=1))
    rep = reconcile(ms, _table(rows), SPEC)
    assert rep.conflicts == 0
    assert len(ms.rules) == 2
