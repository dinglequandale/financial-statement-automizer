"""Arithmetic verification of a mapping (PLAN 7.3).

The check exists to catch placement errors without ground truth, so the tests
are built the same way: a small synthetic client whose own subtotals tie, and
mappings that break them in each of the ways a real one can.
"""

from __future__ import annotations

import pytest

from fsa.model.mapping import Decider, Exclusion, MappingRule, MappingSet, Target
from fsa.model.schema import (
    ConsolidatedRow,
    ConsolidatedTable,
    RowKind,
    Severity,
    StatementType,
)
from fsa.validate.mapping_checks import (
    COMPUTED,
    GRAND_TOTAL,
    GROUP,
    classify_client_rows,
    verify_mapping,
)

YEAR = 2025

_TEMPLATE = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "Konrad Project"
    / "sample_1_TS"
    / "BVAL Model (Weaver Template).xlsx"
)


@pytest.fixture(scope="module")
def blank_spec():
    """Weaver's real blank template.

    Deliberately not a stub: the check reads `derived`/`collapsible` off real
    template lines to decide what a legal collapse is, so a hand-built spec
    would test the fixture rather than the code.
    """
    if not _TEMPLATE.is_file():
        pytest.skip(f"template not available at {_TEMPLATE}")
    from fsa.mapping.template import load_template

    return load_template(_TEMPLATE)


def _row(label, kind, value, section=None, depth=None):
    return ConsolidatedRow(
        raw_label=label,
        norm_label=label.lower().strip(),
        kind=kind,
        section=section,
        values={YEAR: value},
        depth=depth,
    )


@pytest.fixture
def table():
    """A balance sheet whose subtotals tie, as every real client's does.

    Current assets 300 (cash 100 + AR 200), current liabilities 50 (AP 50),
    then a grand total over the two and a computed result that is neither.
    """
    t = ConsolidatedTable(statement=StatementType.BS, years=[YEAR])
    # Depths mirror QuickBooks: detail deepest, block subtotals one level up,
    # grand totals at the margin. `descendants` reads this ladder, so flat
    # depths would be testing the fixture rather than the classifier.
    t.rows = [
        _row("Cash", RowKind.DATA, 100.0, "Current Assets", 2),
        _row("Accounts Receivable", RowKind.DATA, 200.0, "Current Assets", 2),
        _row("Total Current Assets", RowKind.SUBTOTAL, 300.0, "Current Assets", 1),
        _row("Accounts Payable", RowKind.DATA, 50.0, "Current Liabilities", 2),
        _row("Total Current Liabilities", RowKind.SUBTOTAL, 50.0, "Current Liabilities", 1),
        _row("Total Assets & Liabilities", RowKind.SUBTOTAL, 350.0, None, 0),
        _row("Working Capital", RowKind.SUBTOTAL, 250.0, None, 0),
    ]
    return t


def _rule(account, target_label, block, *, by=Decider.LLM, conf=0.9, sign=1):
    return MappingRule(
        client_account=account,
        norm_account=account.lower().strip(),
        target=Target(statement=StatementType.BS, label=target_label, block=block),
        sign=sign,
        confidence=conf,
        decided_by=by,
    )


def _set(*rules, exclusions=()):
    ms = MappingSet(statement=StatementType.BS)
    ms.rules.extend(rules)
    ms.exclusions.extend(exclusions)
    return ms


# ---------------------------------------------------------------------------
# 1. classification
# ---------------------------------------------------------------------------


def test_classifies_rollups_grand_totals_and_computed_results(table):
    kinds = classify_client_rows(table)
    assert kinds["total current assets"] == GROUP
    assert kinds["total current liabilities"] == GROUP
    # a sum whose constituents are themselves subtotals
    assert kinds["total assets & liabilities"] == GRAND_TOTAL
    # 250 is not the sum of anything above it
    assert kinds["working capital"] == COMPUTED


def test_classification_is_arithmetic_not_name_based(table):
    """Renaming a row must not change what it is. `Gross Margin` is detected
    because it does not add up, never because of the word 'margin'."""
    table.rows[-1].raw_label = "Schedule 4b"
    table.rows[-1].norm_label = "schedule 4b"
    assert classify_client_rows(table)["schedule 4b"] == COMPUTED


# ---------------------------------------------------------------------------
# 2. placement
# ---------------------------------------------------------------------------


def test_clean_mapping_verifies(table, blank_spec):
    ms = _set(
        _rule("Cash", "Cash and Cash Equivalents", "Total Current Assets"),
        _rule("Accounts Receivable", "Accounts Receivable", "Total Current Assets"),
        _rule("Accounts Payable", "Accounts Payable", "Total Current Liabilities"),
    )
    rep = verify_mapping(ms, table, blank_spec)
    assert rep.failed_blocks == 0
    assert all(r.verified is not False for r in ms.rules)


def test_a_reconciling_block_records_positive_evidence(table, blank_spec):
    """`verified` is tri-state and the True half is real information.

    It is recorded for display only. Using it to *lower* a row's review
    priority was tried and reverted: a block reconciles when we have reproduced
    the client's own presentation, which is exactly the case a deliberate
    analyst reclassification overrules. It may raise an alarm, never lower one.
    """
    ms = _set(
        _rule("Cash", "Cash and Cash Equivalents", "Total Current Assets"),
        _rule("Accounts Receivable", "Accounts Receivable", "Total Current Assets"),
        _rule("Accounts Payable", "Accounts Payable", "Total Current Liabilities"),
    )
    rep = verify_mapping(ms, table, blank_spec)
    assert rep.verified_rules == 3
    assert all(r.verified is True for r in ms.rules)


def test_an_unchecked_block_stays_unknown_not_passing(table, blank_spec):
    """Only 5 blocks align between client and template. The rest must keep
    saying None -- 'nobody checked' is not 'checked and fine'."""
    table.rows[2].raw_label = "Total Widgets"
    table.rows[2].norm_label = "total widgets"
    ms = _set(_rule("Cash", "Cash and Cash Equivalents", "Total Current Assets"))
    verify_mapping(ms, table, blank_spec)
    assert ms.rules[0].verified is None


def test_catches_an_account_crossing_a_block_boundary(table, blank_spec):
    """The `Inventory Receipts - Clearing Account` shape: a liability mapped as
    an asset. Both blocks break, by equal and opposite amounts."""
    ms = _set(
        _rule("Cash", "Cash and Cash Equivalents", "Total Current Assets"),
        _rule("Accounts Receivable", "Accounts Receivable", "Total Current Assets"),
        _rule("Accounts Payable", "Inventory", "Total Current Assets"),
    )
    rep = verify_mapping(ms, table, blank_spec)
    assert rep.failed_blocks == 2
    assert not rep.ok
    misplaced = [r for r in ms.rules if r.verified is False]
    assert [r.client_account for r in misplaced] == ["Accounts Payable"]


def test_names_the_offending_account_in_the_finding(table, blank_spec):
    ms = _set(
        _rule("Cash", "Cash and Cash Equivalents", "Total Current Assets"),
        _rule("Accounts Receivable", "Accounts Receivable", "Total Current Assets"),
        _rule("Accounts Payable", "Inventory", "Total Current Assets"),
    )
    rep = verify_mapping(ms, table, blank_spec)
    assert any("Accounts Payable" in f.message for f in rep.findings)


def test_a_dropped_account_breaks_its_block(table, blank_spec):
    """Silence is the failure this exists to rule out: AR mapped nowhere and
    not excluded leaves current assets 200 short."""
    ms = _set(
        _rule("Cash", "Cash and Cash Equivalents", "Total Current Assets"),
        _rule("Accounts Payable", "Accounts Payable", "Total Current Liabilities"),
    )
    rep = verify_mapping(ms, table, blank_spec)
    assert rep.failed_blocks == 1
    assert any("Accounts Receivable" in f.message for f in rep.findings)


def test_sign_is_respected(table, blank_spec):
    """A contra account mapped with the wrong sign breaks the block by 2x."""
    ms = _set(
        _rule("Cash", "Cash and Cash Equivalents", "Total Current Assets"),
        _rule("Accounts Receivable", "Accounts Receivable",
              "Total Current Assets", sign=-1),
        _rule("Accounts Payable", "Accounts Payable", "Total Current Liabilities"),
    )
    rep = verify_mapping(ms, table, blank_spec)
    assert rep.failed_blocks == 1


def test_human_decisions_are_never_overruled(table, blank_spec):
    ms = _set(
        _rule("Cash", "Cash and Cash Equivalents", "Total Current Assets"),
        _rule("Accounts Receivable", "Accounts Receivable", "Total Current Assets"),
        _rule("Accounts Payable", "Inventory", "Total Current Assets",
              by=Decider.HUMAN, conf=1.0),
    )
    verify_mapping(ms, table, blank_spec)
    assert ms.rules[-1].verified is not False


# ---------------------------------------------------------------------------
# 3. suppression of rows that are not accounts
# ---------------------------------------------------------------------------


def test_computed_rows_become_exclusions_not_decisions(table, blank_spec):
    """`Working Capital` has no right answer. Leaving it unresolved costs the
    analyst a from-scratch decision; it should arrive pre-excluded."""
    ms = _set(
        _rule("Working Capital", "", None, by=Decider.UNRESOLVED, conf=0.0),
    )
    rep = verify_mapping(ms, table, blank_spec)
    assert rep.suppressed_rows == 1
    assert not ms.rules
    assert [e.norm_account for e in ms.exclusions] == ["working capital"]


def test_suppression_is_reversible_and_explained(table, blank_spec):
    ms = _set(_rule("Working Capital", "", None, by=Decider.UNRESOLVED, conf=0.0))
    verify_mapping(ms, table, blank_spec)
    assert "computed result" in ms.exclusions[0].reason


def test_mapping_a_computed_row_is_an_error(table, blank_spec):
    ms = _set(_rule("Working Capital", "Accounts Receivable", "Total Current Assets"))
    rep = verify_mapping(ms, table, blank_spec)
    assert not rep.ok
    assert ms.rules[0].verified is False


# ---------------------------------------------------------------------------
# 4. exclusions
# ---------------------------------------------------------------------------


def test_exclusion_covered_by_a_mapped_rollup_is_safe(table, blank_spec):
    """Map the total, exclude the detail: the money is still in the model."""
    ms = _set(
        _rule("Total Current Assets", "Cash and Cash Equivalents",
              "Total Current Assets"),
        exclusions=[
            Exclusion(client_account="Cash", norm_account="cash",
                      reason="covered by rollup", decided_by=Decider.STRUCTURE),
            Exclusion(client_account="Accounts Receivable",
                      norm_account="accounts receivable",
                      reason="covered by rollup", decided_by=Decider.STRUCTURE),
        ],
    )
    rep = verify_mapping(ms, table, blank_spec)
    assert rep.unsafe_exclusions == 0


def test_exclusion_of_a_rollup_whose_detail_is_mapped_is_safe(table, blank_spec):
    """The opposite direction, which an earlier version of this check reported
    as dropped money on every one of TS's eight legitimate rollup exclusions."""
    ms = _set(
        _rule("Cash", "Cash and Cash Equivalents", "Total Current Assets"),
        _rule("Accounts Receivable", "Accounts Receivable", "Total Current Assets"),
        exclusions=[
            Exclusion(client_account="Total Current Assets",
                      norm_account="total current assets",
                      reason="components mapped individually",
                      decided_by=Decider.STRUCTURE),
        ],
    )
    rep = verify_mapping(ms, table, blank_spec)
    assert rep.unsafe_exclusions == 0


def test_exclusion_that_actually_drops_money_is_caught(table, blank_spec):
    """Nothing maps AR and nothing covers it, but it was excluded anyway."""
    ms = _set(
        _rule("Cash", "Cash and Cash Equivalents", "Total Current Assets"),
        _rule("Accounts Payable", "Accounts Payable", "Total Current Liabilities"),
        exclusions=[
            Exclusion(client_account="Accounts Receivable",
                      norm_account="accounts receivable",
                      reason="covered by rollup", decided_by=Decider.STRUCTURE),
        ],
    )
    rep = verify_mapping(ms, table, blank_spec)
    assert rep.unsafe_exclusions == 1
    assert any("Accounts Receivable" in f.message for f in rep.findings)
    assert any(f.severity is Severity.ERROR for f in rep.findings)


def test_an_analysts_own_exclusion_is_left_alone(table, blank_spec):
    ms = _set(
        _rule("Cash", "Cash and Cash Equivalents", "Total Current Assets"),
        _rule("Accounts Payable", "Accounts Payable", "Total Current Liabilities"),
        exclusions=[
            Exclusion(client_account="Accounts Receivable",
                      norm_account="accounts receivable",
                      reason="written off, per analyst", decided_by=Decider.HUMAN),
        ],
    )
    rep = verify_mapping(ms, table, blank_spec)
    assert rep.unsafe_exclusions == 0
