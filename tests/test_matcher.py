"""Unit tests for fsa.mapping.matcher.propose() -- the L0-L3 pipeline.

Everything is built directly from the schema/template dataclasses (no Excel
files), same approach as tests/test_consolidate.py.
"""

from __future__ import annotations

from pathlib import Path

from fsa.ingest.normalize import normalize
from fsa.mapping.matcher import FUZZY_THRESHOLD, UNRESOLVED_LABEL, propose
from fsa.mapping.template import TemplateLine, TemplateSpec
from fsa.model.mapping import (
    ClientProfile,
    Decider,
    Exclusion,
    MappingRule,
    Target,
    TargetKind,
)
from fsa.model.schema import ConsolidatedRow, ConsolidatedTable, RowKind, StatementType

BS = StatementType.BS
IS = StatementType.IS


def _tl(
    statement: StatementType,
    row: int,
    label: str,
    *,
    block: str | None = None,
    placeholder: bool = False,
    derived: bool = False,
    collapsible: bool = False,
    frozen: bool = False,
) -> TemplateLine:
    return TemplateLine(
        statement=statement,
        row=row,
        label=label,
        frozen=frozen,
        derived=derived,
        collapsible=collapsible,
        placeholder=placeholder,
        block=block,
    )


def _bs_spec() -> TemplateSpec:
    lines = [
        _tl(BS, 9, "Cash and Cash Equivalents", block="Total Current Assets"),
        _tl(BS, 10, "Accounts Receivable", block="Total Current Assets"),
        _tl(BS, 11, "Inventory", block="Total Current Assets"),
        _tl(BS, 12, "Prepaid Expenses", block="Total Current Assets"),
        _tl(BS, 13, "Widget Assembly Fee", block="Block Alpha"),
        _tl(BS, 14, "Widget Assembly", block="Block Beta"),
        _tl(BS, 22, "Accumulated Depreciation", block="Net Fixed Assets"),
        _tl(BS, 37, "Accounts Payable", block="Total Current Liabilities"),
        _tl(BS, 38, "Other Payables", block="Total Current Liabilities"),
        _tl(BS, 39, "Accrued Expenses", block="Total Current Liabilities"),
        _tl(BS, 50, "Long-Term Debt", block="Total Non-Current Liabilities"),
        _tl(BS, 56, "Total Equity", collapsible=True),
        _tl(BS, 33, "Total Assets", derived=True),  # not a target: derived, not collapsible
    ]
    return TemplateSpec(source=Path("dummy_bs.xlsx"), lines=lines, blocks=[])


def _is_spec() -> TemplateSpec:
    lines = [
        _tl(IS, 11, "Revenue Component 1", block="Revenue", placeholder=True),
        _tl(IS, 12, "Revenue Component 2", block="Revenue", placeholder=True),
        _tl(IS, 14, "Revenue", collapsible=True),
        _tl(IS, 40, "Interest Income", block="Pre-Tax Income"),
        _tl(IS, 41, "Interest (Expense)", block="Pre-Tax Income"),
    ]
    return TemplateSpec(source=Path("dummy_is.xlsx"), lines=lines, blocks=[])


def _row(
    label: str,
    section: str | None,
    kind: RowKind = RowKind.DATA,
    values: dict[int, float | None] | None = None,
) -> ConsolidatedRow:
    return ConsolidatedRow(
        raw_label=label,
        norm_label=normalize(label),
        kind=kind,
        section=section,
        values=values or {2025: 100.0},
    )


def _table(statement: StatementType, rows: list[ConsolidatedRow]) -> ConsolidatedTable:
    return ConsolidatedTable(statement=statement, years=[2025], rows=rows)


# --- L1 alias ---------------------------------------------------------


def test_alias_resolves() -> None:
    table = _table(BS, [_row("Petty Cash", "Current Assets")])
    result = propose(table, _bs_spec())
    assert len(result.rules) == 1
    rule = result.rules[0]
    assert rule.decided_by is Decider.ALIAS
    assert rule.target.label == "Cash and Cash Equivalents"
    assert rule.target.kind is TargetKind.ASSIGN
    assert rule.sign == 1
    assert rule.confidence == 0.95


def test_alias_sign_accumulated_depreciation() -> None:
    table = _table(BS, [_row("Accumulated Depreciation & Amort.", "Fixed Assets")])
    result = propose(table, _bs_spec())
    rule = result.rules[0]
    assert rule.decided_by is Decider.ALIAS
    assert rule.target.label == "Accumulated Depreciation"
    assert rule.sign == 1  # arrives negative already -- never negated


def test_alias_sign_interest_expense_negated_on_is() -> None:
    table = _table(IS, [_row("Interest Expense", "Other Expenses")])
    result = propose(table, _is_spec())
    rule = result.rules[0]
    assert rule.decided_by is Decider.ALIAS
    assert rule.target.label == "Interest (Expense)"
    assert rule.sign == -1


def test_alias_does_not_fire_when_target_absent_from_this_template() -> None:
    # "Notes Payable" -> "Long-Term Debt" is a seeded alias, but this
    # template happens not to define that line -- must not invent it.
    spec = TemplateSpec(source=Path("x.xlsx"), lines=[], blocks=[])
    table = _table(BS, [_row("Notes Payable", "Long Term Liabilities")])
    result = propose(table, spec)
    rule = result.rules[0]
    assert rule.decided_by is Decider.UNRESOLVED


# --- L0 profile ---------------------------------------------------------


def test_profile_beats_alias() -> None:
    profile = ClientProfile(client_name="Acme")
    profile.get(BS).rules.append(
        MappingRule(
            client_account="Petty Cash",
            norm_account="petty cash",
            target=Target(statement=BS, label="Accounts Payable", kind=TargetKind.ASSIGN),
            sign=-1,
            decided_by=Decider.HUMAN,
        )
    )
    table = _table(BS, [_row("Petty Cash", "Current Assets")])
    result = propose(table, _bs_spec(), profile=profile)
    rule = result.rules[0]
    assert rule.decided_by is Decider.PROFILE
    assert rule.confidence == 1.0
    # Stored target and sign are preserved verbatim, even though they
    # disagree with what L1 would have proposed.
    assert rule.target.label == "Accounts Payable"
    assert rule.sign == -1


def test_profile_exclusion_is_replayed() -> None:
    profile = ClientProfile(client_name="Acme")
    profile.get(BS).exclusions.append(
        Exclusion(
            client_account="Suspense Account",
            norm_account="suspense account",
            reason="Not a real account -- data entry artifact.",
            decided_by=Decider.HUMAN,
        )
    )
    table = _table(BS, [_row("Suspense Account", "Current Assets")])
    result = propose(table, _bs_spec(), profile=profile)
    assert result.rules == []
    assert len(result.exclusions) == 1
    assert result.exclusions[0].norm_account == "suspense account"


# --- L2 + L3 structure/fuzzy ---------------------------------------------


def test_fuzzy_typo_resolves_above_threshold() -> None:
    table = _table(BS, [_row("Other Paybles", "Current Liabilities")])
    result = propose(table, _bs_spec())
    rule = result.rules[0]
    assert rule.decided_by is Decider.FUZZY
    assert rule.target.label == "Other Payables"
    assert rule.confidence >= FUZZY_THRESHOLD


def test_structural_restriction_changes_fuzzy_outcome() -> None:
    # Unrestricted, "Widget Assembly Fee" is a textually perfect (1.0)
    # fuzzy match for the contrived "Widget Assembly Fee" line -- but that
    # line lives in "Block Alpha", while the client's own section matches
    # "Block Beta", home of the lower-scoring "Widget Assembly" line.
    # Structural restriction must steer L3 to the client's own block even
    # though it is not the globally best textual match.
    table = _table(BS, [_row("Widget Assembly Fee", "Beta")])
    result = propose(table, _bs_spec())
    rule = result.rules[0]
    assert rule.decided_by is Decider.FUZZY
    assert rule.target.label == "Widget Assembly"


def test_fuzzy_without_section_uses_full_candidate_set() -> None:
    table = _table(BS, [_row("Other Paybles", None)])
    result = propose(table, _bs_spec())
    rule = result.rules[0]
    assert rule.decided_by is Decider.FUZZY
    assert rule.target.label == "Other Payables"


# --- UNRESOLVED / never-invent guarantees --------------------------------


def test_unresolved_when_nothing_matches() -> None:
    table = _table(BS, [_row("Warehouse - Equipment & Racks", "Fixed Assets")])
    result = propose(table, _bs_spec())
    rule = result.rules[0]
    assert rule.decided_by is Decider.UNRESOLVED
    assert rule.confidence == 0.0
    assert rule.sign == 1
    assert rule.target.label == UNRESOLVED_LABEL


def test_never_proposes_placeholder_target() -> None:
    table = _table(
        IS,
        [
            _row("Sales", "REVENUE"),
            _row("Freight Income", "REVENUE"),
            _row("Finance Revenues", "REVENUE"),
        ],
    )
    result = propose(table, _is_spec())
    placeholder_labels = {"Revenue Component 1", "Revenue Component 2"}
    for rule in result.rules:
        assert rule.target.label not in placeholder_labels
        assert rule.target.kind is TargetKind.ASSIGN


def test_never_proposes_name_slot_or_insert() -> None:
    table = _table(
        BS,
        [
            _row("Petty Cash", "Current Assets"),
            _row("Other Paybles", "Current Liabilities"),
            _row("Something Completely Unmatched Xyz", "Fixed Assets"),
        ],
    )
    result = propose(table, _bs_spec())
    assert len(result.rules) == 3
    for rule in result.rules:
        assert rule.target.kind is TargetKind.ASSIGN


# --- subtotal handling / coverage invariant -------------------------------


def test_subtotal_rows_are_surfaced_but_never_auto_resolved() -> None:
    """Client subtotals are mapping candidates, not deterministic decisions.

    PLAN.md 7.0b: four ground-truth labels map FROM a client subtotal, so the
    matcher must surface them -- but choosing rollup-vs-detail is a judgement,
    and an alias hit on `Total Current Assets` would double-count against its
    own components. So: present as UNRESOLVED, decide nowhere in this module.
    """
    table = _table(
        BS,
        [
            _row("Petty Cash", "Current Assets"),
            _row("Total Current Assets", "Current Assets", kind=RowKind.SUBTOTAL),
        ],
    )
    result = propose(table, _bs_spec())
    assert len(result.rules) == 2
    assert "total current assets" in result.covered()

    sub = result.for_account("total current assets")
    assert sub is not None
    assert sub.decided_by is Decider.UNRESOLVED, "a subtotal must never be auto-resolved"
    assert sub.confidence == 0.0

    cash = result.for_account("petty cash")
    assert cash is not None and cash.decided_by is not Decider.UNRESOLVED


def test_covered_equals_all_mappable_accounts() -> None:
    """No account is silently dropped -- data rows AND client subtotals."""
    rows = [
        _row("Petty Cash", "Current Assets"),
        _row("Other Paybles", "Current Liabilities"),
        _row("Something Completely Unmatched Xyz", "Fixed Assets"),
        _row("Total Current Assets", "Current Assets", kind=RowKind.SUBTOTAL),
    ]
    table = _table(BS, rows)
    profile = ClientProfile(client_name="Acme")
    profile.get(BS).exclusions.append(
        Exclusion(
            client_account="Something Completely Unmatched Xyz",
            norm_account=normalize("Something Completely Unmatched Xyz"),
            reason="test exclusion",
            decided_by=Decider.HUMAN,
        )
    )
    result = propose(table, _bs_spec(), profile=profile)
    expected = {
        normalize(r.raw_label)
        for r in rows
        if r.kind in (RowKind.DATA, RowKind.SUBTOTAL)
    }
    assert result.covered() == expected
    # No row was silently dropped: every DATA row is either a rule or an
    # exclusion, never both, never neither.
    assert len(result.rules) + len(result.exclusions) == len(expected)


def test_every_rule_has_explicit_sign_and_confidence() -> None:
    table = _table(
        BS,
        [
            _row("Petty Cash", "Current Assets"),
            _row("Other Paybles", "Current Liabilities"),
            _row("Something Completely Unmatched Xyz", "Fixed Assets"),
        ],
    )
    result = propose(table, _bs_spec())
    for rule in result.rules:
        assert rule.sign in (1, -1)
        assert 0.0 <= rule.confidence <= 1.0
        assert rule.decided_by is not Decider.HUMAN  # nothing here was human-confirmed
