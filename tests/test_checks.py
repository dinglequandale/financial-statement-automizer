"""Unit tests for fsa.validate.checks, against synthetic fixtures.

The FY2022/FY2023/FY2025 shapes below are taken directly from
SPEC-PHASE0.md's "Ground truth for the acceptance test". They exist so that
row_alignment_suspected's run-vs-isolated discrimination is proven here, on
data we fully control, independent of the real sample files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fsa.ingest.normalize import normalize
from fsa.model.schema import (
    AccountRow,
    CellRef,
    ColumnRole,
    ConsolidatedRow,
    ConsolidatedTable,
    ExtractedColumn,
    RowKind,
    Severity,
    StatementSet,
    StatementType,
)
from fsa.validate.checks import (
    account_added,
    account_removed,
    account_renamed,
    audit_against_reference,
    balance_sheet_unbalanced,
    check_comparative_agreement,
    comparative_alignment_suspected,
    comparative_disagreement,
    row_alignment_suspected,
    run_all,
    subtotal_mismatch,
    unparsed_row,
    value_delta,
)

FILE = Path("dummy.xlsx")


def _row(label, value, idx, kind=RowKind.DATA, section="Current Assets"):
    return AccountRow(
        raw_label=label,
        norm_label=normalize(label),
        kind=kind,
        value=value,
        section=section,
        row_index=idx,
        ref=CellRef(file=FILE, sheet="Balance Sheet", cell=f"C{idx}"),
    )


def _column(year, rows, statement=StatementType.BS, role=ColumnRole.PRIMARY):
    return ExtractedColumn(
        statement=statement,
        fiscal_year=year,
        role=role,
        source_file=FILE,
        source_sheet="Balance Sheet",
        value_column="C",
        label_column="A",
        rows=rows,
    )


def _crow(label, values, kind=RowKind.DATA, section=None):
    return ConsolidatedRow(
        raw_label=label,
        norm_label=normalize(label),
        kind=kind,
        section=section,
        values=values,
    )


def _table(statement, years, rows):
    return ConsolidatedTable(statement=statement, years=list(years), rows=rows)


# --------------------------------------------------------------------------
# subtotal_mismatch
# --------------------------------------------------------------------------


def test_subtotal_matches_no_finding():
    rows = [
        _row("Cash", 100.0, 10),
        _row("Accounts Receivable", 200.0, 11),
        _row("Total Current Assets", 300.0, 12, kind=RowKind.SUBTOTAL),
    ]
    findings = subtotal_mismatch(_column(2023, rows))
    assert findings == []


def test_subtotal_mismatch_flagged():
    rows = [
        _row("Cash", 100.0, 10),
        _row("Accounts Receivable", 200.0, 11),
        _row("Total Current Assets", 999.0, 12, kind=RowKind.SUBTOTAL),
    ]
    findings = subtotal_mismatch(_column(2023, rows))
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "subtotal_mismatch"
    assert f.severity is Severity.WARNING
    assert f.detail["expected"] == 300.0
    assert f.detail["actual"] == 999.0


def test_subtotal_mismatch_tolerance():
    rows = [
        _row("Cash", 100.0, 10),
        _row("Total Current Assets", 100.005, 11, kind=RowKind.SUBTOTAL),
    ]
    assert subtotal_mismatch(_column(2023, rows)) == []


# --------------------------------------------------------------------------
# balance_sheet_unbalanced
# --------------------------------------------------------------------------


def test_balance_sheet_balanced_no_finding():
    rows = [
        _row("Cash", 100.0, 10),
        _row("Total Assets", 100.0, 11, kind=RowKind.SUBTOTAL),
        _row("Total Liabilities", 40.0, 12, kind=RowKind.SUBTOTAL),
        _row("Total Owners Equity", 60.0, 13, kind=RowKind.SUBTOTAL),
    ]
    assert balance_sheet_unbalanced(_column(2023, rows)) == []


def test_balance_sheet_unbalanced_flagged():
    rows = [
        _row("Cash", 100.0, 10),
        _row("Total Assets", 100.0, 11, kind=RowKind.SUBTOTAL),
        _row("Total Liabilities", 40.0, 12, kind=RowKind.SUBTOTAL),
        _row("Total Owners Equity", 50.0, 13, kind=RowKind.SUBTOTAL),
    ]
    findings = balance_sheet_unbalanced(_column(2023, rows))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].code == "balance_sheet_unbalanced"


def test_balance_sheet_unbalanced_ignores_income_statement():
    rows = [_row("Sales", 100.0, 10)]
    assert balance_sheet_unbalanced(_column(2023, rows, statement=StatementType.IS)) == []


# --------------------------------------------------------------------------
# unparsed_row
# --------------------------------------------------------------------------


def test_unparsed_row_flags_blank_kind_with_value():
    rows = [
        _row("Mystery Row", 5.0, 10, kind=RowKind.BLANK),
        _row("Cash", 100.0, 11),
    ]
    findings = unparsed_row(_column(2023, rows))
    assert len(findings) == 1
    assert findings[0].code == "unparsed_row"
    assert findings[0].account == "Mystery Row"


def test_unparsed_row_ignores_genuine_blank():
    rows = [_row("", None, 10, kind=RowKind.BLANK)]
    assert unparsed_row(_column(2023, rows)) == []


# --------------------------------------------------------------------------
# comparative_disagreement / check_comparative_agreement
# --------------------------------------------------------------------------


def test_comparative_agrees_no_finding():
    comp = _column(2022, [_row("Cash", 100.0, 10)], role=ColumnRole.COMPARATIVE)
    prior = _column(2022, [_row("Cash", 100.0, 10)], role=ColumnRole.PRIMARY)
    assert comparative_disagreement(comp, prior) == []


def test_comparative_disagreement_flagged():
    # WARNING, not ERROR: the client genuinely restates prior periods between
    # filings (see SPEC-PHASE0.md "Prior-period restatements" and the
    # severity policy above the check table) -- this is a disagreement
    # between two source documents for a human to weigh, not proof our own
    # extraction is broken, so it must never block (exit code 1).
    comp = _column(2022, [_row("Cash", 100.0, 10)], role=ColumnRole.COMPARATIVE)
    prior = _column(2022, [_row("Cash", 150.0, 10)], role=ColumnRole.PRIMARY)
    findings = comparative_disagreement(comp, prior)
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert findings[0].code == "comparative_disagreement"
    assert findings[0].detail["delta"] == pytest.approx(-50.0)


def test_check_comparative_agreement_end_to_end():
    ss = StatementSet()
    # FY2022 file: primary col is FY2022, comparative col holds FY2021 data.
    ss.add(_column(2022, [_row("Cash", 150.0, 10)], role=ColumnRole.PRIMARY))
    ss.add(_column(2021, [_row("Cash", 100.0, 10)], role=ColumnRole.COMPARATIVE))
    # FY2021 file's own primary column disagrees with FY2022 file's comparative.
    ss.add(_column(2021, [_row("Cash", 999.0, 10)], role=ColumnRole.PRIMARY))

    findings = check_comparative_agreement(ss)
    assert len(findings) == 1
    assert findings[0].fiscal_year == 2021
    assert findings[0].code == "comparative_disagreement"
    assert findings[0].severity is Severity.WARNING


# --------------------------------------------------------------------------
# comparative_alignment_suspected -- shift / swap patterns within the
# comparative-column cross-check (distinct from audit-mode
# row_alignment_suspected, but reusing the same run detector).
# --------------------------------------------------------------------------


def test_comparative_alignment_shift_detected_and_excluded_from_disagreement():
    # prior_primary (FY2022's own file) is authoritative; comparative (FY2023
    # file's comparative column, representing FY2022) is shifted by one row
    # across a run of 4 accounts.
    prior = _column(
        2022,
        [
            _row("Cash", 100.0, 10),
            _row("A", 10.0, 11),
            _row("B", 20.0, 12),
            _row("C", 30.0, 13),
            _row("D", 40.0, 14),
        ],
        role=ColumnRole.PRIMARY,
    )
    comp = _column(
        2022,
        [
            _row("Cash", 100.0, 10),
            _row("A", 100.0, 11),  # holds Cash's value
            _row("B", 10.0, 12),  # holds A's value
            _row("C", 20.0, 13),  # holds B's value
            _row("D", 30.0, 14),  # holds C's value
        ],
        role=ColumnRole.COMPARATIVE,
    )

    findings = comparative_alignment_suspected(comp, prior)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity is Severity.WARNING
    assert f.code == "comparative_alignment_suspected"
    assert f.fiscal_year == 2022
    assert f.detail["pattern"] == "shift"
    assert set(f.detail["accounts"]) >= {"A", "B", "C", "D"}

    # The shifted accounts must not also be reported as scattered
    # comparative_disagreement noise.
    disagreements = comparative_disagreement(comp, prior)
    assert disagreements == []


def test_comparative_alignment_swap_detected_with_account_and_delta():
    # Real-world shape: TS Distributors FY2021 BS -- "Certificate of Deposit"
    # and "Accounts Receivable - Trade" swap values between the comparative
    # and prior primary columns.
    prior = _column(
        2021,
        [
            _row("Operating Account", 1_960_313.38, 10),
            _row("Certificate of Deposit", 0.0, 11),
            _row("Accounts Receivable - Trade", 4_105_570.88, 12),
        ],
        role=ColumnRole.PRIMARY,
    )
    comp = _column(
        2021,
        [
            _row("Operating Account", 1_960_313.38, 10),
            _row("Certificate of Deposit", 4_105_570.88, 11),  # swapped
            _row("Accounts Receivable - Trade", 0.0, 12),  # swapped
        ],
        role=ColumnRole.COMPARATIVE,
    )

    findings = comparative_alignment_suspected(comp, prior)
    assert len(findings) == 2  # one per account, not one combined finding
    by_account = {f.account: f for f in findings}
    assert set(by_account) == {"Certificate of Deposit", "Accounts Receivable - Trade"}
    for f in findings:
        assert f.severity is Severity.WARNING
        assert f.fiscal_year == 2021
        assert f.detail["pattern"] == "swap"
    assert abs(by_account["Certificate of Deposit"].detail["delta"]) == pytest.approx(4_105_570.88)
    assert abs(by_account["Accounts Receivable - Trade"].detail["delta"]) == pytest.approx(4_105_570.88)

    # This is a structural fault, not a scattered restatement -- must not
    # ALSO show up as comparative_disagreement noise.
    disagreements = comparative_disagreement(comp, prior)
    assert disagreements == []


def test_comparative_alignment_no_pattern_for_scattered_restatements():
    # Ordinary, unrelated restatements (not adjacent, not forming a shift or
    # swap) must not trigger comparative_alignment_suspected.
    prior = _column(
        2023,
        [
            _row("Sales", 9_937_524.83, 10),
            _row("Cost of Goods Sold", 100.0, 11),
            _row("Total Overhead", 14_443_013.39, 12),
        ],
        role=ColumnRole.PRIMARY,
    )
    comp = _column(
        2023,
        [
            _row("Sales", 9_787_524.83, 10),  # restated, -150,000
            _row("Cost of Goods Sold", 100.0, 11),
            _row("Total Overhead", 14_293_013.39, 12),  # restated, -150,000
        ],
        role=ColumnRole.COMPARATIVE,
    )

    assert comparative_alignment_suspected(comp, prior) == []
    disagreements = comparative_disagreement(comp, prior)
    assert len(disagreements) == 2
    for d in disagreements:
        assert d.severity is Severity.WARNING
        assert d.detail["delta"] == pytest.approx(-150_000.0)


# --------------------------------------------------------------------------
# account_added / account_removed / account_renamed
# --------------------------------------------------------------------------


def test_account_added_and_removed():
    table = _table(
        StatementType.BS,
        [2023, 2024],
        [
            _crow("Cash", {2023: 100.0, 2024: 110.0}),
            _crow("Certificate of Deposit", {2023: 3_000_000.0, 2024: None}),
            _crow("Goodwill", {2023: None, 2024: 500.0}),
        ],
    )
    added = account_added(table)
    removed = account_removed(table)

    assert len(added) == 1
    assert added[0].code == "account_added"
    assert added[0].severity is Severity.INFO
    assert added[0].account == "Goodwill"
    assert added[0].fiscal_year == 2024

    assert len(removed) == 1
    assert removed[0].code == "account_removed"
    assert removed[0].severity is Severity.INFO
    assert removed[0].account == "Certificate of Deposit"
    assert removed[0].fiscal_year == 2024


def test_account_renamed_detected_and_excluded_from_added_removed():
    table = _table(
        StatementType.BS,
        [2022, 2023],
        [
            _crow("Cash", {2022: 100.0, 2023: 110.0}),
            _crow("Accounts Receivable - Lawler", {2022: 500.0, 2023: None}),
            _crow("Accounts Receivable - Indital", {2022: None, 2023: 500.0}),
        ],
    )
    renamed = account_renamed(table)
    assert len(renamed) == 1
    f = renamed[0]
    assert f.code == "account_renamed"
    assert f.severity is Severity.WARNING
    assert f.fiscal_year == 2023
    assert "Lawler" in f.message and "Indital" in f.message

    # The renamed pair must not also show up as separate added/removed noise.
    assert account_added(table) == []
    assert account_removed(table) == []


def test_unrelated_add_and_remove_not_treated_as_rename():
    table = _table(
        StatementType.BS,
        [2023, 2024],
        [
            _crow("Cash", {2023: 100.0, 2024: 110.0}),
            _crow("Certificate of Deposit", {2023: 3_000_000.0, 2024: None}),
            _crow("Goodwill - Exclusivity Contract", {2023: None, 2024: 500.0}),
        ],
    )
    # "Certificate of Deposit" and "Goodwill - Exclusivity Contract" share no
    # meaningful similarity -- must not be paired as a rename.
    assert account_renamed(table) == []
    assert len(account_added(table)) == 1
    assert len(account_removed(table)) == 1


# --------------------------------------------------------------------------
# run_all
# --------------------------------------------------------------------------


def test_run_all_clean_statement_set_is_ok():
    ss = StatementSet()
    ss.add(
        _column(
            2023,
            [
                _row("Cash", 100.0, 10),
                _row("Total Assets", 100.0, 11, kind=RowKind.SUBTOTAL),
                _row("Total Liabilities", 40.0, 12, kind=RowKind.SUBTOTAL),
                _row("Total Owners Equity", 60.0, 13, kind=RowKind.SUBTOTAL),
            ],
        )
    )
    table = _table(StatementType.BS, [2023], [_crow("Cash", {2023: 100.0})])
    report = run_all(ss, {StatementType.BS: table})
    assert report.ok


def test_run_all_chart_of_accounts_drift_is_info_not_error():
    """Accounts appearing/disappearing between years must be INFO, never
    ERROR -- the client's chart of accounts legitimately drifts every year."""
    ss = StatementSet()
    table = _table(
        StatementType.BS,
        [2023, 2024],
        [
            _crow("Cash", {2023: 100.0, 2024: 110.0}),
            _crow("Certificate of Deposit", {2023: 3_000_000.0, 2024: None}),
            _crow("Other Payables", {2023: None, 2024: 500.0}),
        ],
    )
    report = run_all(ss, {StatementType.BS: table})
    assert report.ok
    assert report.errors == []
    codes = {f.code for f in report.findings}
    assert "account_added" in codes
    assert "account_removed" in codes


# --------------------------------------------------------------------------
# Audit mode -- the headline check
#
# Fixtures below reproduce the exact shapes from SPEC-PHASE0.md's
# "Ground truth for the acceptance test":
#   - FY2023: an 11-account run shifted by one row, then row 12 realigns.
#   - FY2022: same fault, shorter/offset run.
#   - FY2025: four isolated, non-adjacent deliberate adjustments -- must NOT
#     trigger row_alignment_suspected.
# --------------------------------------------------------------------------


def _fy2023_ours_labels_values():
    # Client's own, correctly ordered FY2023 BS values (a prefix account is
    # included so the run's first position has a "previous" neighbour, just
    # like the real sheet).
    return [
        ("Operating Account", 6_015_809.17),
        ("Certificate of Deposit", 0.0),
        ("Accounts Receivable - Trade", 4_317_772.76),
        ("Accounts Receivable - Lawler", 0.0),
        ("A/R terms - Allowed write off", -10.06),
        ("Allowance for doubtful accounts", 0.0),
        ("Other A/R", 2_407.53),
        ("NSF Clearing Account", 21_020.25),
        ("Notes Receivable", 25_547.44),
        ("Employee Advances", 4_703.16),
        ("Inventory", 16_881_097.15),
        ("Inventory - Offsite", -790_884.25),
        ("Inventory - Capitalized Sec 236A", 196_412.62),
    ]


def _make_ours_table(year, labels_values):
    rows = [_crow(label, {year: value}) for label, value in labels_values]
    return _table(StatementType.BS, [year], rows)


def _make_reference_table(year, ref_labels_values):
    rows = [_crow(label, {year: value}) for label, value in ref_labels_values]
    return _table(StatementType.BS, [year], rows)


def test_fy2023_shift_detected_as_single_run_not_per_row():
    ours_lv = _fy2023_ours_labels_values()
    ours = _make_ours_table(2023, ours_lv)

    # Weaver's grid: label list one step "ahead" -- position j's label holds
    # position (j-1)'s value, for the 11 accounts starting at "Certificate of
    # Deposit". "Accounts Receivable - Lawler" is Weaver's standing rename
    # "Accounts Receivable - Indital" (unrelated to the shift, but sitting
    # inside the run). The last account realigns (matches itself).
    ref_labels = [label for label, _ in ours_lv]
    ref_labels[3] = "Accounts Receivable - Indital"  # standing rename
    ref_values = [ours_lv[0][1]] + [v for _, v in ours_lv[:-1]]
    # ref_values[i] should equal ours_lv[i-1] for i=1..11, and ref_values[12]
    # (last) should realign to ours_lv[12] itself.
    ref_lv = list(zip(ref_labels, ref_values))
    ref_lv[-1] = (ref_labels[-1], ours_lv[-1][1])  # realign the last row
    ref_lv[0] = (ref_labels[0], ours_lv[0][1])  # first row (Operating Account) unaffected

    reference = _make_reference_table(2023, ref_lv)

    alignment_findings = row_alignment_suspected(ours, reference)
    assert len(alignment_findings) >= 1, "expected at least one run to be detected"
    assert len(alignment_findings) <= 2, "at most one finding per run, not per row"

    f = alignment_findings[0]
    assert f.severity is Severity.ERROR
    assert f.code == "row_alignment_suspected"
    assert f.fiscal_year == 2023
    assert f.detail["length"] >= 3
    all_accounts = set()
    for finding in alignment_findings:
        all_accounts.update(finding.detail["accounts"])
    assert "Certificate of Deposit" in all_accounts

    deltas = value_delta(ours, reference)
    delta_accounts = {d.account for d in deltas}
    # The shifted rows must not ALSO show up as isolated value_delta noise.
    assert "Certificate of Deposit" not in delta_accounts


def test_fy2022_variant_shift_detected():
    # Same shape, offset begins one row later, matching the spec's FY2022
    # description: Weaver's "Accounts Receivable - Trade" holds the client's
    # Certificate of Deposit value of 3,000,000.00; Weaver's
    # "Accounts Receivable - Indital" holds the client's
    # "Accounts Receivable - Trade" value of 3,412,281.79.
    ours_lv = [
        ("Operating Account", 5_000_000.00),
        ("Certificate of Deposit", 3_000_000.00),
        ("Accounts Receivable - Trade", 3_412_281.79),
        ("Other A/R", 2_000.00),
        ("NSF Clearing Account", 20_000.00),
        ("Notes Receivable", 25_000.00),
        ("Employee Advances", 4_000.00),
        ("Inventory", 15_000_000.00),
    ]
    ours = _make_ours_table(2022, ours_lv)

    # Shift the whole tail by one position: ref[label_i] = ours[label_(i-1)]
    # for i = 1..7, ref[0] unaffected (Operating Account matches itself).
    ref_labels = [label for label, _ in ours_lv]
    ref_values = [ours_lv[0][1]] + [v for _, v in ours_lv[:-1]]
    ref_lv = list(zip(ref_labels, ref_values))

    reference = _make_reference_table(2022, ref_lv)

    alignment_findings = row_alignment_suspected(ours, reference)
    assert len(alignment_findings) >= 1
    assert len(alignment_findings) <= 2

    blob = " ".join(f.message for f in alignment_findings) + repr(
        [f.detail for f in alignment_findings]
    )
    assert "Certificate of Deposit" in blob or "3000000" in blob.replace(",", "").replace(".00", "")


def test_fy2025_isolated_adjustments_are_value_delta_not_alignment():
    # Four isolated, non-adjacent deliberate adjustments -- must be reported
    # as value_delta, never as row_alignment_suspected.
    ours_lv = [
        ("Prepaid Inventory", 10_000.00),
        ("Cash", 500_000.00),
        ("Accounts Payable", 200_000.00),
        ("Retained Earnings", 1_000_000.00),
        ("Machinery & Equipment", 300_000.00),
        ("Vehicles", 50_000.00),
    ]
    ours = _make_ours_table(2025, ours_lv)

    adjustments = {
        "prepaid inventory": 21_294.79,
        "accounts payable": 21_294.79,
        "machinery and equipment": 7_500.00,
        "vehicles": 17_777.98,
    }
    ref_lv = []
    for label, value in ours_lv:
        bump = adjustments.get(normalize(label), 0.0)
        ref_lv.append((label, value + bump))

    reference = _make_reference_table(2025, ref_lv)

    alignment_findings = row_alignment_suspected(ours, reference)
    assert alignment_findings == [], "FY2025 deliberate adjustments must never trigger alignment"

    deltas = value_delta(ours, reference)
    assert len(deltas) == 4
    for d in deltas:
        assert d.severity is Severity.WARNING
        assert d.code == "value_delta"
        assert d.fiscal_year == 2025

    by_account = {d.account: d.detail["delta"] for d in deltas}
    assert by_account["Prepaid Inventory"] == pytest.approx(21_294.79)
    assert by_account["Accounts Payable"] == pytest.approx(21_294.79)
    assert by_account["Vehicles"] == pytest.approx(17_777.98)


def test_audit_against_reference_combines_both_checks():
    ours_lv = _fy2023_ours_labels_values()
    ours = _make_ours_table(2023, ours_lv)
    ref_lv = [(label, value) for label, value in ours_lv]  # perfectly aligned
    reference = _make_reference_table(2023, ref_lv)

    findings = audit_against_reference(ours, reference)
    assert findings == []
    for f in findings:
        assert f.fiscal_year is not None


def test_short_run_below_threshold_is_value_delta_not_alignment():
    # Only 2 consecutive accounts shifted -- below MIN_RUN_LENGTH (3), so it
    # must be reported as isolated value_delta findings, not an alignment run.
    ours_lv = [
        ("Cash", 100.0),
        ("A", 10.0),
        ("B", 20.0),
        ("C", 30.0),
    ]
    ours = _make_ours_table(2023, ours_lv)
    # Shift only "A" and "B": ref[A] = ours[Cash]=100 (mismatch vs neighbours,
    # not part of a >=3 run), ref[B] = ours[A]=10. "C" realigns.
    ref_lv = [
        ("Cash", 100.0),
        ("A", 100.0),
        ("B", 10.0),
        ("C", 30.0),
    ]
    reference = _make_reference_table(2023, ref_lv)

    alignment_findings = row_alignment_suspected(ours, reference)
    assert alignment_findings == []

    deltas = value_delta(ours, reference)
    delta_accounts = {d.account for d in deltas}
    assert "A" in delta_accounts
    assert "B" in delta_accounts
