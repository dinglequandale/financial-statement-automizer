"""What a subtotal is actually totalling, when the source has no indentation.

A flat spreadsheet carries no depth, so a subtotal's scope has to be inferred,
and "everything since the previous subtotal" is wrong on any nested statement.
Client four showed both ways it goes wrong: a standalone expense printed
between two groups is charged to whichever group comes next, and a total of
totals is compared against a fraction of itself.

The rules below are drawn from what the document itself states -- a subtotal
that names its parent, and a parent that totals its children -- and they may
only ever *exonerate*. A subtotal the original check declined to judge is still
declined, because that guard is what keeps derived lines (`Gross Margin`,
`Net Income`) from producing confident nonsense.
"""

from __future__ import annotations

from pathlib import Path

from fsa.ingest.normalize import normalize
from fsa.model.schema import (
    AccountRow,
    ColumnRole,
    ExtractedColumn,
    RowKind,
    StatementType,
)
from fsa.validate.checks import subtotal_mismatch


def row(label, value, kind=RowKind.DATA, index=0):
    return AccountRow(
        raw_label=label, norm_label=normalize(label), kind=kind,
        value=value, section=None, row_index=index,
    )


def column(rows, statement=StatementType.IS, year=2024):
    return ExtractedColumn(
        statement=statement, fiscal_year=year, role=ColumnRole.PRIMARY,
        source_file=Path("a.xlsx"), source_sheet="Sheet1",
        value_column="B", label_column="A", rows=rows,
    )


def codes(col):
    return [f.account for f in subtotal_mismatch(col)]


# --------------------------------------------------------------------------
# a subtotal that names its own scope
# --------------------------------------------------------------------------


def test_a_subtotal_naming_its_header_is_scoped_to_that_header():
    """`Total for 64000 Legal & accounting services` totals the group it names.

    The insurance line printed above the group has nothing to do with it, and
    charging the group with it produced a mismatch of exactly that amount.
    """
    col = column([
        row("63300 Insurance", 15885.46),
        row("64000 Legal & accounting services", None, RowKind.SECTION_HEADER),
        row("64002 Legal fees", 8140.0),
        row("Total for 64000 Legal & accounting services", 8140.0, RowKind.SUBTOTAL),
    ])
    assert subtotal_mismatch(col) == []


def test_a_parent_account_with_its_own_balance_is_included():
    """QuickBooks lets a parent carry a posting of its own.

    `66000 Payroll expenses` holds 200.86 directly and is totalled together
    with its five children, so it reads as data rather than as a header.
    """
    col = column([
        row("Something else entirely", 1514.58),
        row("66000 Payroll expenses", 200.86),
        row("66003 Salaries & wages", 22600.0),
        row("66004 Taxes", 20239.8),
        row("Total for 66000 Payroll expenses", 43040.66, RowKind.SUBTOTAL),
    ])
    assert subtotal_mismatch(col) == []


def test_naming_something_else_does_not_earn_a_pass():
    col = column([
        row("64000 Legal & accounting services", None, RowKind.SECTION_HEADER),
        row("64002 Legal fees", 8140.0),
        row("Total for 64000 Legal & accounting services", 9999.0, RowKind.SUBTOTAL),
    ])
    assert codes(col) == ["Total for 64000 Legal & accounting services"]


# --------------------------------------------------------------------------
# a subtotal that totals other subtotals
# --------------------------------------------------------------------------


def test_a_total_of_totals_reconciles_against_them():
    """`Total Liabilities` is current plus long-term, not the long-term row."""
    col = column([
        row("Accounts payable", 162095.0),
        row("Finance lease liability", 72513.0),
        row("Total Current Liabilities", 234608.0, RowKind.SUBTOTAL),
        row("Finance lease liability, net of current", 362737.0),
        row("Total Liabilities", 597345.0, RowKind.SUBTOTAL),
    ], statement=StatementType.BS)
    assert subtotal_mismatch(col) == []


def test_a_rollup_replaces_what_it_rolled_up_rather_than_stacking():
    """Otherwise the grand total is charged with its children twice."""
    col = column([
        row("Accounts payable", 162095.0),
        row("Finance lease liability", 72513.0),
        row("Total Current Liabilities", 234608.0, RowKind.SUBTOTAL),
        row("Finance lease liability, net of current", 362737.0),
        row("Total Liabilities", 597345.0, RowKind.SUBTOTAL),
        row("Members' Equity", 654261.0),
        row("Total Liabilities and Members' Equity", 1251606.0, RowKind.SUBTOTAL),
    ], statement=StatementType.BS)
    assert subtotal_mismatch(col) == []


# --------------------------------------------------------------------------
# the guard that must survive
# --------------------------------------------------------------------------


def test_a_subtotal_with_no_data_rows_is_still_not_judged():
    """Derived lines net whole blocks; guessing at them invents mismatches."""
    col = column([
        row("Cash", 100.0),
        row("Total Assets", 100.0, RowKind.SUBTOTAL),
        row("Total Liabilities", 40.0, RowKind.SUBTOTAL),
        row("Total Owners Equity", 60.0, RowKind.SUBTOTAL),
    ], statement=StatementType.BS)
    assert subtotal_mismatch(col) == []


def test_a_derived_line_is_skipped_by_name():
    col = column([
        row("Revenue", 1000.0),
        row("Gross Margin", 12345.0, RowKind.SUBTOTAL),
    ])
    assert subtotal_mismatch(col) == []


def test_a_source_that_carries_depth_is_left_to_the_hierarchy_check():
    rows = [
        row("Cash", 100.0),
        row("Total Assets", 999.0, RowKind.SUBTOTAL),
    ]
    rows = [
        AccountRow(raw_label=r.raw_label, norm_label=r.norm_label, kind=r.kind,
                   value=r.value, section=None, row_index=0, depth=1)
        for r in rows
    ]
    assert subtotal_mismatch(column(rows)) == []


# --------------------------------------------------------------------------
# and a real one still gets caught
# --------------------------------------------------------------------------


def test_a_genuinely_missing_figure_is_still_reported():
    """Client four's `Accounts payable` cell is corrupted to `5 694,379`.

    The figure is unreadable, so the block is short by exactly it, and no
    reading of the document's structure can explain the gap away.
    """
    col = column([
        row("Accounts payable", None),          # the corrupted cell
        row("Accrued liabilities", 32129.0),
        row("Billings in excess of costs", 88392.0),
        row("Notes payable, current portion", 181436.0),
        row("Total current liabilities", 996336.0, RowKind.SUBTOTAL),
    ], statement=StatementType.BS)
    found = subtotal_mismatch(col)
    assert [f.account for f in found] == ["Total current liabilities"]
    assert found[0].detail["expected"] == 301957.0
    assert found[0].detail["actual"] == 996336.0
