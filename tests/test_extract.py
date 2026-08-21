"""Unit tests for fsa.ingest.extract, on synthetic in-memory workbooks.

extract_workbook / extract_reference_grid both take a *path*, so each test
builds a workbook with openpyxl.Workbook() and saves it to tmp_path before
calling in -- unlike discover.py's functions, which take a Workbook directly.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from fsa.ingest.extract import extract_reference_grid, extract_workbook
from fsa.model.schema import CellRef, ColumnRole, RowKind, StatementType


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _save(wb: Workbook, tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    wb.save(path)
    return path


def _build_client_bs(tmp_path: Path, name="client.xlsx") -> Path:
    """A single-year BS shaped like the real files: label col A, current-year
    col C, prior-year comparative col E, a section header, a blank separator
    row, a real zero, a genuinely blank cell (blank-vs-zero case), and a
    subtotal.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Balance Sheet"
    ws["A1"] = "TS Distributors, Inc."
    ws["A2"] = "Balance Sheet"
    ws["A3"] = "As of December 31, 2024"
    ws["C5"] = datetime(2024, 12, 30)  # deliberately not the 31st
    ws["E5"] = datetime(2023, 12, 31)
    ws["G5"] = "% net Change"

    # row 6: section header (blank in both value columns)
    ws["A6"] = "Current Assets"

    # row 7: ordinary data row
    ws["A7"] = "Cash"
    ws["C7"] = 1000.0
    ws["E7"] = 900.0

    # row 8: real zero in both years
    ws["A8"] = "Prepaid Payroll"
    ws["C8"] = 0.0
    ws["E8"] = 0.0

    # row 9: genuinely blank in the current year, populated in the comparative
    ws["A9"] = "Prepaid Taxes"
    ws["E9"] = 19782.0

    # row 10: blank separator row (label empty) -- must be BLANK regardless
    # of anything else.

    # row 11: subtotal
    ws["A11"] = "Total Current Assets"
    ws["C11"] = 1000.0
    ws["E11"] = 900.0

    return _save(wb, tmp_path, name)


def _build_client_is(tmp_path: Path, name="client_is.xlsx") -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Consolidated IS"
    ws["A1"] = "TS Distributors, Inc."

    months = ["January", "February"]
    col = 2
    for m in months:
        ws[f"{get_column_letter(col)}7"] = m
        ws[f"{get_column_letter(col + 1)}7"] = "%"
        col += 2
    ws["Z7"] = "YTD 2021"
    ws["BA7"] = "YTD 2020"

    ws["A9"] = "REVENUE"
    ws["A10"] = "Sales"
    ws["Z10"] = 68009052.46
    ws["BA10"] = 58799492.87
    ws["B10"] = 999999.0  # monthly column -- must never be read as a value column

    ws["A11"] = "Total Revenue"
    ws["Z11"] = 68009052.46
    ws["BA11"] = 58799492.87

    return _save(wb, tmp_path, name)


def _build_weaver_grid(tmp_path: Path, name="weaver.xlsx") -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Balance Sheets"
    ws["A1"] = "TS Distributors, Inc."
    ws["A2"] = "Balance Sheets"

    years = [2020, 2021, 2022]
    for i, y in enumerate(years):
        col = get_column_letter(2 + i)  # B, C, D
        ws[f"{col}5"] = datetime(y, 12, 1)

    ws["A6"] = "Current Assets"  # section header, blank everywhere

    ws["A7"] = "Cash"
    ws["B7"] = 100.0
    ws["C7"] = 200.0
    ws["D7"] = 300.0

    # Certificate of Deposit: present only in 2020/2021, absent in 2022.
    ws["A8"] = "Certificate of Deposit"
    ws["B8"] = 3_000_000.0
    ws["C8"] = 3_000_000.0
    # D8 intentionally blank

    ws["A9"] = "Total Current Assets"
    ws["B9"] = 3_100_000.0
    ws["C9"] = 3_200_000.0
    ws["D9"] = 300.0

    return _save(wb, tmp_path, name)


# ---------------------------------------------------------------------------
# extract_workbook -- balance sheet
# ---------------------------------------------------------------------------

def test_extract_workbook_bs_two_columns_correct_roles(tmp_path):
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS,))

    assert len(cols) == 2
    by_role = {c.role: c for c in cols}
    assert by_role[ColumnRole.PRIMARY].fiscal_year == 2024
    assert by_role[ColumnRole.COMPARATIVE].fiscal_year == 2023
    assert by_role[ColumnRole.PRIMARY].value_column == "C"
    assert by_role[ColumnRole.COMPARATIVE].value_column == "E"
    assert by_role[ColumnRole.PRIMARY].label_column == "A"
    assert by_role[ColumnRole.PRIMARY].source_sheet == "Balance Sheet"


def test_extract_workbook_bs_year_only_match(tmp_path):
    """Header date 2024-12-30 must still yield fiscal_year 2024."""
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS,))
    primary = next(c for c in cols if c.role is ColumnRole.PRIMARY)
    assert primary.fiscal_year == 2024
    assert primary.period_end.year == 2024


def test_extract_workbook_bs_section_header_classification(tmp_path):
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS,))
    primary = next(c for c in cols if c.role is ColumnRole.PRIMARY)

    section_row = next(r for r in primary.rows if r.raw_label == "Current Assets")
    assert section_row.kind is RowKind.SECTION_HEADER
    assert section_row.value is None


def test_extract_workbook_bs_data_row_carries_section(tmp_path):
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS,))
    primary = next(c for c in cols if c.role is ColumnRole.PRIMARY)

    cash = primary.find("cash")
    assert cash is not None
    assert cash.kind is RowKind.DATA
    assert cash.value == pytest.approx(1000.0)
    assert cash.section == "Current Assets"


def test_extract_workbook_bs_blank_row_is_blank_kind(tmp_path):
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS,))
    primary = next(c for c in cols if c.role is ColumnRole.PRIMARY)

    blank_row = next(r for r in primary.rows if r.row_index == 10)
    assert blank_row.kind is RowKind.BLANK
    assert blank_row.value is None


def test_extract_workbook_bs_title_rows_above_header(tmp_path):
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS,))
    primary = next(c for c in cols if c.role is ColumnRole.PRIMARY)

    title_rows = [r for r in primary.rows if r.row_index in (1, 2, 3)]
    assert all(r.kind is RowKind.TITLE for r in title_rows)


def test_extract_workbook_bs_subtotal_classification(tmp_path):
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS,))
    primary = next(c for c in cols if c.role is ColumnRole.PRIMARY)

    subtotal = primary.find("total current assets")
    assert subtotal is not None
    assert subtotal.kind is RowKind.SUBTOTAL
    assert subtotal.value == pytest.approx(1000.0)


def test_extract_workbook_blank_vs_zero_distinction(tmp_path):
    """The load-bearing distinction: a genuinely blank cell must be None, a
    real zero must stay 0.0 -- and a row blank in *this* column but populated
    in the sheet's *other* value column must still be classified DATA, not
    misread as a section header.
    """
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS,))
    primary = next(c for c in cols if c.role is ColumnRole.PRIMARY)
    comparative = next(c for c in cols if c.role is ColumnRole.COMPARATIVE)

    zero_row = primary.find("prepaid payroll")
    assert zero_row.kind is RowKind.DATA
    assert zero_row.value == 0.0
    assert zero_row.value is not None

    blank_row_primary = primary.find("prepaid taxes")
    assert blank_row_primary is not None
    assert blank_row_primary.kind is RowKind.DATA
    assert blank_row_primary.value is None

    blank_row_comparative = comparative.find("prepaid taxes")
    assert blank_row_comparative is not None
    assert blank_row_comparative.kind is RowKind.DATA
    assert blank_row_comparative.value == pytest.approx(19782.0)


def test_extract_workbook_ignores_percent_column(tmp_path):
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS,))
    assert all(c.value_column != "G" for c in cols)


def test_extract_workbook_cellref_provenance_on_every_row(tmp_path):
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS,))
    for col in cols:
        for row in col.rows:
            assert row.ref is not None
            assert isinstance(row.ref, CellRef)
            assert row.ref.sheet == "Balance Sheet"
            assert row.ref.file == path


def test_extract_workbook_data_row_ref_points_at_value_cell(tmp_path):
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS,))
    primary = next(c for c in cols if c.role is ColumnRole.PRIMARY)
    cash = primary.find("cash")
    assert cash.ref.cell == "C7"


# ---------------------------------------------------------------------------
# extract_workbook -- income statement
# ---------------------------------------------------------------------------

def test_extract_workbook_is_two_columns_via_ytd(tmp_path):
    path = _build_client_is(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.IS,))

    assert len(cols) == 2
    by_role = {c.role: c for c in cols}
    assert by_role[ColumnRole.PRIMARY].fiscal_year == 2021
    assert by_role[ColumnRole.PRIMARY].value_column == "Z"
    assert by_role[ColumnRole.COMPARATIVE].fiscal_year == 2020
    assert by_role[ColumnRole.COMPARATIVE].value_column == "BA"


def test_extract_workbook_is_sales_values(tmp_path):
    path = _build_client_is(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.IS,))
    primary = next(c for c in cols if c.role is ColumnRole.PRIMARY)
    comparative = next(c for c in cols if c.role is ColumnRole.COMPARATIVE)

    assert primary.find("sales").value == pytest.approx(68009052.46)
    assert comparative.find("sales").value == pytest.approx(58799492.87)


def test_extract_workbook_is_never_reads_monthly_column(tmp_path):
    path = _build_client_is(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.IS,))
    primary = next(c for c in cols if c.role is ColumnRole.PRIMARY)
    # If the extractor accidentally used column B, Sales would read 999999.0.
    assert primary.find("sales").value != pytest.approx(999999.0)


# ---------------------------------------------------------------------------
# extract_workbook -- statement not present
# ---------------------------------------------------------------------------

def test_extract_workbook_skips_absent_statement(tmp_path):
    """A workbook with only a BS sheet shouldn't error when IS is requested too."""
    path = _build_client_bs(tmp_path)
    cols = extract_workbook(path, statements=(StatementType.BS, StatementType.IS))
    assert all(c.statement is StatementType.BS for c in cols)
    assert len(cols) == 2


# ---------------------------------------------------------------------------
# extract_reference_grid
# ---------------------------------------------------------------------------

def test_extract_reference_grid_years_and_row_count(tmp_path):
    path = _build_weaver_grid(tmp_path)
    table = extract_reference_grid(path, "Balance Sheets", StatementType.BS)

    assert table.years == [2020, 2021, 2022]
    assert table.statement is StatementType.BS
    # 3 accounts: Cash, Certificate of Deposit, Total Current Assets
    # (the section header row "Current Assets" is not itself a row)
    assert len(table.rows) == 3


def test_extract_reference_grid_account_present_in_some_years_only(tmp_path):
    path = _build_weaver_grid(tmp_path)
    table = extract_reference_grid(path, "Balance Sheets", StatementType.BS)

    cd = table.find("certificate of deposit")
    assert cd is not None
    assert cd.values[2020] == pytest.approx(3_000_000.0)
    assert cd.values[2021] == pytest.approx(3_000_000.0)
    assert cd.values[2022] is None  # absent, not zero


def test_extract_reference_grid_section_propagates_to_data_rows(tmp_path):
    path = _build_weaver_grid(tmp_path)
    table = extract_reference_grid(path, "Balance Sheets", StatementType.BS)

    cash = table.find("cash")
    assert cash.section == "Current Assets"


def test_extract_reference_grid_subtotal_classification(tmp_path):
    path = _build_weaver_grid(tmp_path)
    table = extract_reference_grid(path, "Balance Sheets", StatementType.BS)

    subtotal = table.find("total current assets")
    assert subtotal.kind is RowKind.SUBTOTAL
    assert subtotal.values[2020] == pytest.approx(3_100_000.0)


def test_extract_reference_grid_provenance_populated(tmp_path):
    path = _build_weaver_grid(tmp_path)
    table = extract_reference_grid(path, "Balance Sheets", StatementType.BS)

    cash = table.find("cash")
    assert set(cash.provenance.keys()) == {2020, 2021, 2022}
    assert all(isinstance(ref, CellRef) for ref in cash.provenance.values())
    assert cash.provenance[2020].cell == "B7"
    assert cash.provenance[2021].cell == "C7"
    assert cash.provenance[2022].cell == "D7"
