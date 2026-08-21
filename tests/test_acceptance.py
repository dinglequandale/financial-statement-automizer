"""Phase 0 acceptance test against the real TS Distributors sample files.

This is the gate for Phase 0. Every number asserted here was verified by direct
inspection of the source workbooks -- see PLAN.md 2.4 and SPEC-PHASE0.md
"Ground truth for the acceptance test".

The point of the audit assertions is DISCRIMINATION: FY2022 and FY2023 must be
flagged as row misalignments, FY2025 must be flagged as ordinary value deltas,
and FY2021/FY2024 must be flagged as neither. A checker that fires on
everything is as useless as one that fires on nothing.

Marked `sample`; skips cleanly when the sample data is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fsa.ingest.normalize import normalize
from fsa.model.schema import ColumnRole, Severity, StatementType

pytestmark = pytest.mark.sample

REPO = Path(__file__).resolve().parents[1]
SAMPLES = REPO / "Konrad Project" / "sample_1_TS"
CLIENT_DIR = SAMPLES / "TS Distributors Financial Statements (from client)"
WEAVER_CONSOLIDATION = CLIENT_DIR / "Dec 2020 -2025 Financias (Weaver edited).xlsx"

CLIENT_FILES = {
    2021: CLIENT_DIR / "Dec 2021 Financials.xlsx",
    2022: CLIENT_DIR / "Dec 2022 Financials.xlsx",
    2023: CLIENT_DIR / "Dec 2023 Financials.xlsx",
    2024: CLIENT_DIR / "Dec 2024 Financials.xlsx",
    2025: CLIENT_DIR / "Dec 2025 Financials.xlsx",
}

if not CLIENT_DIR.is_dir():  # pragma: no cover
    pytest.skip("sample data not present", allow_module_level=True)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _value(col, label: str) -> float | None:
    """Look up an account's value in an ExtractedColumn by label."""
    row = col.find(normalize(label))
    assert row is not None, f"account {label!r} not found in {col.fiscal_year} {col.statement}"
    return row.value


@pytest.fixture(scope="module")
def statement_set():
    from fsa.model.schema import StatementSet
    from fsa.ingest.extract import extract_workbook

    ss = StatementSet(entity_name="TS Distributors, Inc.")
    for path in CLIENT_FILES.values():
        for col in extract_workbook(path):
            ss.add(col)
    return ss


@pytest.fixture(scope="module")
def tables(statement_set):
    from fsa.consolidate import consolidate

    return {
        StatementType.BS: consolidate(statement_set, StatementType.BS),
        StatementType.IS: consolidate(statement_set, StatementType.IS),
    }


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def test_all_client_files_ingest(statement_set):
    assert statement_set.years(StatementType.BS) == [2021, 2022, 2023, 2024, 2025]
    assert statement_set.years(StatementType.IS) == [2021, 2022, 2023, 2024, 2025]


def test_fy2023_balance_sheet_values(statement_set):
    """The year whose accounts Weaver's consolidation shifted by one row."""
    bs = statement_set.get(StatementType.BS, 2023)
    assert bs is not None

    assert _value(bs, "Operating Account") == pytest.approx(6_015_809.17)
    assert _value(bs, "Accounts Receivable - Trade") == pytest.approx(4_317_772.76)
    assert _value(bs, "Other A/R") == pytest.approx(2_407.53)
    assert _value(bs, "NSF Clearing Account") == pytest.approx(21_020.25)
    assert _value(bs, "Notes Receivable") == pytest.approx(25_547.44)
    assert _value(bs, "Employee Advances") == pytest.approx(4_703.16)
    assert _value(bs, "Inventory") == pytest.approx(16_881_097.15)
    assert _value(bs, "Inventory - Offsite") == pytest.approx(-790_884.25)
    assert _value(bs, "Total Current Assets") == pytest.approx(27_023_852.03)
    assert _value(bs, "Total Assets") == pytest.approx(28_621_957.68)


def test_fy2022_certificate_of_deposit(statement_set):
    """The $3.0M that Weaver's consolidation files under Accounts Receivable."""
    bs = statement_set.get(StatementType.BS, 2022)
    assert _value(bs, "Certificate of Deposit") == pytest.approx(3_000_000.00)
    assert _value(bs, "Accounts Receivable - Trade") == pytest.approx(3_412_281.79)
    assert _value(bs, "Total Assets") == pytest.approx(26_843_111.28)


def test_blank_is_not_zero(statement_set):
    """FY2022 Prepaid Taxes is genuinely blank; FY2022 Prepaid Payroll is a real 0."""
    bs = statement_set.get(StatementType.BS, 2022)
    assert _value(bs, "Prepaid Taxes") is None
    assert _value(bs, "Prepaid Payroll") == pytest.approx(0.0)


def test_income_statement_ytd_extraction(statement_set):
    is21 = statement_set.get(StatementType.IS, 2021)
    assert _value(is21, "Sales") == pytest.approx(68_009_052.46)

    is25 = statement_set.get(StatementType.IS, 2025)
    assert _value(is25, "Sales") == pytest.approx(84_132_942.36)
    assert _value(is25, "Total Cost of Goods Sold") == pytest.approx(60_800_047.60)


def test_comparative_column_is_captured(statement_set):
    """FY2021's file carries FY2020, which has no standalone file of its own."""
    comp = statement_set.get(StatementType.BS, 2020, role=ColumnRole.COMPARATIVE)
    assert comp is not None
    assert _value(comp, "Operating Account") == pytest.approx(3_726_916.24)

    is_comp = statement_set.get(StatementType.IS, 2020, role=ColumnRole.COMPARATIVE)
    assert _value(is_comp, "Sales") == pytest.approx(58_799_492.87)


# --------------------------------------------------------------------------
# validation of the client files themselves
# --------------------------------------------------------------------------

def test_client_files_validate_clean(statement_set, tables):
    """The client's own files are internally consistent.

    Accounts appearing/disappearing between years are INFO, not errors -- the
    chart of accounts legitimately drifts.
    """
    from fsa.validate.checks import run_all

    report = run_all(statement_set, tables)
    assert report.ok, "unexpected errors:\n" + "\n".join(
        f"  [{f.code}] {f.fiscal_year} {f.account}: {f.message}" for f in report.errors
    )


@pytest.fixture(scope="module")
def comparative_findings(statement_set):
    from fsa.validate.checks import check_comparative_agreement

    return check_comparative_agreement(statement_set)


def test_comparative_disagreements_never_block(comparative_findings):
    """The client restates prior periods. That is normal, and must not block.

    Measured: 20 disagreeing IS accounts in FY2021, 4 in FY2022, 8 in FY2023,
    9 in FY2024. If this were an ERROR the tool would never pass on this client.
    """
    assert not [f for f in comparative_findings if f.severity is Severity.ERROR]
    assert comparative_findings, "restatements exist in the samples and must be reported"


def test_material_restatement_is_surfaced(comparative_findings):
    """FY2023 Advertising - Catalogs was refiled 11,991.60 -> 18,098.98.

    Which version to use is an analyst judgement. The tool's job is to make sure
    nobody has to notice it by hand.
    """
    hits = [
        f
        for f in comparative_findings
        if f.fiscal_year == 2023
        and f.account
        and normalize(f.account) == normalize("Advertising - Catalogs")
    ]
    assert hits, "FY2023 restatement of Advertising - Catalogs not reported"
    assert abs(hits[0].detail.get("delta", 0)) == pytest.approx(6_107.38, abs=0.01)


def test_fy2021_certificate_of_deposit_swap_is_flagged(comparative_findings):
    """A $4.1M balance that three documents classify three different ways.

    Dec 2021 files it as Accounts Receivable - Trade; Dec 2022's comparative
    column, with identical row labels, files it as Certificate of Deposit;
    Weaver's consolidation files it as Accounts Receivable - Indital.
    """
    fy21 = [f for f in comparative_findings if f.fiscal_year == 2021]
    accounts = {normalize(f.account) for f in fy21 if f.account}
    assert normalize("Certificate of Deposit") in accounts
    assert normalize("Accounts Receivable - Trade") in accounts

    magnitudes = [abs(f.detail.get("delta", 0)) for f in fy21 if f.account]
    assert max(magnitudes) == pytest.approx(4_105_570.88, abs=0.01)


def test_swap_is_recognised_as_positional_not_restatement(comparative_findings):
    """A 2-account swap is a structural fault, not a bookkeeping restatement."""
    codes = {
        f.code
        for f in comparative_findings
        if f.fiscal_year == 2021 and f.statement is StatementType.BS
    }
    assert "comparative_alignment_suspected" in codes


# --------------------------------------------------------------------------
# audit mode -- the headline capability
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def audit_findings(tables):
    from fsa.ingest.extract import extract_reference_grid
    from fsa.validate.checks import audit_against_reference

    reference = extract_reference_grid(
        WEAVER_CONSOLIDATION, "Balance Sheets", StatementType.BS
    )
    return audit_against_reference(tables[StatementType.BS], reference)


def _codes_for_year(findings, year: int) -> set[str]:
    return {f.code for f in findings if f.fiscal_year == year}


def test_audit_flags_fy2022_and_fy2023_misalignment(audit_findings):
    for year in (2022, 2023):
        assert "row_alignment_suspected" in _codes_for_year(audit_findings, year), (
            f"FY{year} misalignment not detected"
        )


def test_audit_does_not_flag_clean_years(audit_findings):
    for year in (2021, 2024):
        assert "row_alignment_suspected" not in _codes_for_year(audit_findings, year), (
            f"FY{year} wrongly reported as misaligned"
        )


def test_fy2025_differences_are_adjustments_not_misalignment(audit_findings):
    """Weaver's FY2025 deltas are deliberate analyst adjustments.

    Reporting these as an alignment fault would be a false positive that
    destroys trust in the checker.
    """
    codes = _codes_for_year(audit_findings, 2025)
    assert "row_alignment_suspected" not in codes
    assert "value_delta" in codes

    deltas = {
        f.account: f.detail.get("delta")
        for f in audit_findings
        if f.fiscal_year == 2025 and f.code == "value_delta"
    }
    by_norm = {normalize(k): v for k, v in deltas.items() if k}
    assert by_norm.get(normalize("Prepaid Inventory")) == pytest.approx(21_294.79)
    assert by_norm.get(normalize("Accounts Payable")) == pytest.approx(21_294.79)
    assert by_norm.get(normalize("Vehicles")) == pytest.approx(17_777.98)


def test_audit_identifies_the_three_million_dollar_misclassification(audit_findings):
    """FY2022's run must name the Certificate of Deposit / AR - Trade pair."""
    runs = [
        f
        for f in audit_findings
        if f.fiscal_year == 2022 and f.code == "row_alignment_suspected"
    ]
    assert runs, "no FY2022 alignment run reported"

    blob = " ".join(
        (f.message or "") + " " + repr(f.detail) for f in runs
    ).lower()
    assert "certificate of deposit" in blob or "3000000" in blob.replace(",", "")


def test_audit_run_is_reported_once_not_per_row(audit_findings):
    """One finding per run. Nine findings for one paste error is noise."""
    for year in (2022, 2023):
        runs = [
            f
            for f in audit_findings
            if f.fiscal_year == year and f.code == "row_alignment_suspected"
        ]
        assert len(runs) <= 2, f"FY{year} produced {len(runs)} alignment findings"
