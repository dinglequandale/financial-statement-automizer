"""Tests for the format-agnostic layer: periods, PDF geometry, interpretation.

Everything here is synthetic. The sample files are the acceptance test
(`test_acceptance.py`); these are the unit tests that must keep passing on a
machine that has never seen a client file.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fsa.ingest.interpret import _classify, _segment, interpret
from fsa.ingest.raw import Period, RawDoc, RawPart, RawRow, parse_period
from fsa.model.schema import RowKind, Severity, StatementType
from fsa.validate.checks import check_hierarchy

# --------------------------------------------------------------------------
# period parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,end,months",
    [
        ("As of December 31, 2025", date(2025, 12, 31), None),
        ("Dec 31, 25", date(2025, 12, 31), None),
        ("January through December 2025", date(2025, 12, 31), 12),
        ("Jan - Dec 25", date(2025, 12, 31), 12),
        ("January through August 2025", date(2025, 8, 31), 8),
        ("Jan - Aug 25", date(2025, 8, 31), 8),
        ("Accrual Basis As of August 31, 2025", date(2025, 8, 31), None),
        ("Sept 30, 2024", date(2024, 9, 30), None),
    ],
)
def test_parse_period_shapes(text, end, months):
    p = parse_period(text)
    assert p is not None, text
    assert p.end == end
    assert p.months == months


def test_period_straddling_a_year_end_and_leap_day():
    p = parse_period("November through February 2024")
    assert p.months == 4
    assert p.end == date(2024, 2, 29)  # 2024 is a leap year


def test_partial_period_is_flagged_but_a_full_year_is_not():
    assert parse_period("Jan - Aug 25").is_partial
    assert not parse_period("Jan - Dec 25").is_partial
    # A balance sheet is a point in time: it has no length, so it is not
    # "partial" on its own terms.
    assert not parse_period("As of August 31, 2025").is_partial


def test_unparseable_text_returns_none_rather_than_guessing():
    assert parse_period("Accrual Basis") is None
    assert parse_period("") is None


# --------------------------------------------------------------------------
# PDF geometry: the label/value split
# --------------------------------------------------------------------------


def _w(x0, x1, text):
    """A PyMuPDF-shaped word tuple: (x0, y0, x1, y1, text, ...)."""
    return (x0, 100.0, x1, 110.0, text, 0, 0, 0)


def test_digits_inside_an_account_name_are_not_read_as_the_value():
    """`Visa AE - 6777` is a card number, not a balance. Regression: FY2022."""
    from fsa.ingest.readers.pdf import _split

    words = [
        _w(198.6, 212.0, "Visa"),
        _w(217.4, 227.0, "AE"),
        _w(230.5, 233.0, "-"),
        _w(235.4, 253.0, "6777"),
        _w(400.0, 427.0, "3,015.46"),
    ]
    labels, values = _split(words)
    assert " ".join(w[4] for w in labels) == "Visa AE - 6777"
    assert [w[4] for w in values] == ["3,015.46"]


def test_a_caption_does_not_donate_its_year_as_a_value():
    """`... December 31, 2025` must stay caption text, not yield 2025.0."""
    from fsa.ingest.readers.pdf import _split

    words = [
        _w(36.0, 62.0, "Accrual"),
        _w(67.3, 82.0, "Basis"),
        _w(247.6, 259.0, "As"),
        _w(262.6, 271.0, "of"),
        _w(274.8, 320.0, "December"),
        _w(325.7, 340.0, "31,"),
        _w(342.2, 358.0, "2025"),
    ]
    labels, values = _split(words)
    assert values == []
    assert "2025" in " ".join(w[4] for w in labels)


def test_ordinary_label_and_right_aligned_value_split_normally():
    from fsa.ingest.readers.pdf import _split

    words = [_w(204.7, 250.0, "Security"), _w(252.0, 290.0, "Deposits"), _w(396.5, 420.0, "450.00")]
    labels, values = _split(words)
    assert [w[4] for w in labels] == ["Security", "Deposits"]
    assert [w[4] for w in values] == ["450.00"]


@pytest.mark.parametrize(
    "tok,expect",
    [("1,234.56", 1234.56), ("-61,700.00", -61700.0), ("(1,234.56)", -1234.56), ("0.00", 0.0)],
)
def test_number_parsing(tok, expect):
    from fsa.ingest.readers.pdf import _to_number

    assert _to_number(tok) == expect


def test_non_numbers_are_rejected():
    from fsa.ingest.readers.pdf import _to_number

    for tok in ("Visa", "-", "", "1-2-3", "Q4"):
        assert _to_number(tok) is None


def test_indent_step_is_the_recurring_gap_not_the_largest():
    from fsa.ingest.readers.pdf import _indent_step

    # A ladder at ~6.85pt with one outlier offset far to the right.
    offsets = [191.0, 197.9, 204.7, 211.6, 218.3, 191.0, 197.9, 204.7, 300.0]
    step = _indent_step(offsets)
    assert step is not None and 6.5 < step < 7.2


# --------------------------------------------------------------------------
# interpretation
# --------------------------------------------------------------------------


def _row(i, label, depth, value=None, truncated=False):
    return RawRow(
        index=i,
        label=label,
        values=[value] if value is not None else [],
        depth=depth,
        locator=f"y={i}",
        truncated=truncated,
    )


def _bs_part(name="p1", year=2025):
    """A miniature QuickBooks balance sheet with real nesting."""
    rows = [
        _row(1, "ASSETS", 0),
        _row(2, "Current Assets", 1),
        _row(3, "Checking/Savings", 2),
        _row(4, "Operating", 3, 100.0),
        _row(5, "Payroll", 3, 25.0),
        _row(6, "Total Checking/Savings", 2, 125.0),
        _row(7, "Total Current Assets", 1, 125.0),
        _row(8, "TOTAL ASSETS", 0, 125.0),
    ]
    return RawPart(
        name=name,
        rows=rows,
        captions=["Acme Inc.", "Balance Sheet", f"As of December 31, {year}"],
    )


def test_depth_gives_each_row_its_section_without_heuristics():
    out = _classify(_bs_part().rows)
    kinds = {r.label: k for r, k, _ in out}
    sections = {r.label: s for r, _, s in out}
    assert kinds["Operating"] is RowKind.DATA
    assert kinds["Checking/Savings"] is RowKind.SECTION_HEADER
    assert kinds["Total Checking/Savings"] is RowKind.SUBTOTAL
    assert sections["Operating"] == "Checking/Savings"
    assert sections["Total Checking/Savings"] == "Current Assets"


def test_a_group_and_its_only_child_may_share_a_name():
    """QuickBooks does this constantly; depth is what separates them."""
    rows = [
        _row(1, "ASSETS", 0),
        _row(2, "Accounts Receivable", 1),
        _row(3, "Accounts Receivable", 2, 500.0),
        _row(4, "Total Accounts Receivable", 1, 500.0),
    ]
    out = _classify(rows)
    kinds = [(r.label, k) for r, k, _ in out]
    assert kinds[1] == ("Accounts Receivable", RowKind.SECTION_HEADER)
    assert kinds[2] == ("Accounts Receivable", RowKind.DATA)


def test_one_part_holding_two_statements_is_split():
    """The converted sheet stacks a balance sheet and a P&L vertically."""
    rows = [
        _row(1, "Balance Sheet", None),
        _row(2, "As of December 31, 2024", None),
        _row(3, "TOTAL ASSETS", 0, 10.0),
        _row(4, "Profit & Loss", None),
        _row(5, "January through December 2024", None),
        _row(6, "Total Income", 1, 99.0),
    ]
    segs = _segment(RawPart(name="Sheet1", rows=rows))
    assert [s.statement for s in segs] == [StatementType.BS, StatementType.IS]
    assert segs[0].period.end == date(2024, 12, 31)
    assert segs[1].period.months == 12


def _plausible_rows(last_label="TOTAL ASSETS", truncated=False):
    """The smallest thing that is genuinely a statement, not a mention of one."""
    return [
        _row(1, "ASSETS", 0),
        _row(2, "Cash", 1, 10.0),
        _row(3, "Inventory", 1, 20.0),
        _row(4, "Receivables", 1, 30.0),
        _row(5, last_label, 0, 60.0, truncated=truncated),
    ]


def test_a_stub_period_is_an_error_not_a_silent_extra_year():
    part = RawPart(
        name="p1",
        rows=_plausible_rows(),
        captions=["Balance Sheet", "January through August 2025"],
    )
    _cols, findings = interpret(RawDoc(source=Path("x.pdf"), kind="pdf", parts=[part]))
    partial = [f for f in findings if f.code == "partial_period"]
    assert len(partial) == 1
    assert partial[0].severity is Severity.ERROR
    assert partial[0].detail["months"] == 8


def test_identical_pages_are_deduplicated():
    doc = RawDoc(
        source=Path("x.pdf"),
        kind="pdf",
        parts=[_bs_part("p1"), _bs_part("p2")],  # same content, different page
    )
    cols, findings = interpret(doc)
    assert len(cols) == 1
    assert [f.code for f in findings] == ["duplicate_page_skipped"]


def test_pages_that_differ_are_both_kept():
    doc = RawDoc(
        source=Path("x.pdf"),
        kind="pdf",
        parts=[_bs_part("p1", 2024), _bs_part("p2", 2025)],
    )
    cols, _ = interpret(doc)
    assert sorted(c.fiscal_year for c in cols) == [2024, 2025]


def test_a_period_we_cannot_read_is_reported_never_guessed():
    part = RawPart(name="p1", rows=_plausible_rows(), captions=["Balance Sheet"])
    cols, findings = interpret(RawDoc(source=Path("x.pdf"), kind="pdf", parts=[part]))
    assert cols == []
    assert [f.code for f in findings] == ["period_unreadable"]
    assert findings[0].severity is Severity.ERROR


def test_a_sheet_that_merely_names_a_statement_is_not_one():
    """The DataSnipper index sheet lists `2025 Balance Sheet.pdf` as a filename."""
    rows = [
        _row(1, "DataSnipper", None),
        _row(2, "The following documents are contained in this workbook.", None),
        _row(3, "File name", None),
        _row(4, "2025 Balance Sheet.pdf", None, 0.03),
        _row(5, "2025 P&L.pdf", None, 0.03),
    ]
    cols, _ = interpret(
        RawDoc(source=Path("x.xlsx"), kind="xlsx", parts=[RawPart("DataSnipper", rows)])
    )
    assert cols == []


def test_a_real_statement_still_passes_the_plausibility_gate():
    cols, _ = interpret(RawDoc(source=Path("x.pdf"), kind="pdf", parts=[_bs_part()]))
    assert len(cols) == 1


def test_period_falls_back_to_the_sheet_name_and_says_so():
    """Converting to Excel strips the masthead; the tab still reads 'Dec 2025'."""
    part = RawPart(name="Dec 2025", rows=_bs_part().rows, captions=["ASSETS"])
    cols, findings = interpret(RawDoc(source=Path("bs.xlsx"), kind="xlsx", parts=[part]))
    assert [c.fiscal_year for c in cols] == [2025]
    inferred = [f for f in findings if f.code == "period_inferred"]
    assert len(inferred) == 1
    assert inferred[0].severity is Severity.WARNING


def test_a_printed_period_beats_the_sheet_name():
    part = RawPart(
        name="Dec 2025",
        rows=_bs_part().rows,
        captions=["Balance Sheet", "As of December 31, 2021"],
    )
    cols, findings = interpret(RawDoc(source=Path("bs.xlsx"), kind="xlsx", parts=[part]))
    assert [c.fiscal_year for c in cols] == [2021]
    assert not any(f.code == "period_inferred" for f in findings)


def test_flat_subtotal_check_stands_down_when_depth_is_available():
    """Without depth it cannot see group boundaries and reports false alarms."""
    from fsa.validate.checks import subtotal_mismatch

    col = _column(_bs_part().rows)
    assert any(r.depth is not None for r in col.rows)
    assert subtotal_mismatch(col) == []


def test_truncated_labels_are_surfaced():
    part = RawPart(
        name="p1",
        rows=_plausible_rows("Total Other Current Liabili...", truncated=True),
        captions=["Balance Sheet", "As of December 31, 2024"],
    )
    _cols, findings = interpret(RawDoc(source=Path("x.pdf"), kind="pdf", parts=[part]))
    assert any(f.code == "label_truncated_at_source" for f in findings)


# --------------------------------------------------------------------------
# hierarchical arithmetic
# --------------------------------------------------------------------------


def _column(rows):
    doc = RawDoc(
        source=Path("x.pdf"),
        kind="pdf",
        parts=[RawPart("p1", rows, ["Balance Sheet", "As of December 31, 2025"])],
    )
    cols, _ = interpret(doc)
    return cols[0]


def test_hierarchy_check_passes_when_every_level_ties():
    assert check_hierarchy(_column(_bs_part().rows)) == []


def test_hierarchy_check_catches_a_broken_subtotal():
    rows = _bs_part().rows
    rows[5] = _row(6, "Total Checking/Savings", 2, 999.0)  # should be 125
    findings = check_hierarchy(_column(rows))
    broken = {f.account: f.detail for f in findings}
    assert broken["Total Checking/Savings"]["expected"] == 125.0
    assert broken["Total Checking/Savings"]["actual"] == 999.0
    # The error propagates: the level above now disagrees too. Reporting both
    # is correct -- an analyst needs to see how far the damage reaches.
    assert "Total Current Assets" in broken


def test_hierarchy_check_counts_nested_subtotals_once():
    """The grand total sums the level below it, not every descendant."""
    rows = [
        _row(1, "ASSETS", 0),
        _row(2, "Current Assets", 1),
        _row(3, "Cash", 2, 10.0),
        _row(4, "Total Current Assets", 1, 10.0),
        _row(5, "Fixed Assets", 1),
        _row(6, "Equipment", 2, 40.0),
        _row(7, "Total Fixed Assets", 1, 40.0),
        _row(8, "TOTAL ASSETS", 0, 50.0),  # 10 + 40, not 10+40+10+40
    ]
    assert check_hierarchy(_column(rows)) == []


def test_hierarchy_check_ignores_derived_lines():
    """Gross Profit is a difference between groups, not a sum of children."""
    rows = [
        _row(1, "Ordinary Income/Expense", 0),
        _row(2, "Total Income", 1, 100.0),
        _row(3, "Total COGS", 1, 60.0),
        _row(4, "Gross Profit", 1, 40.0),  # not 160
    ]
    assert check_hierarchy(_column(rows)) == []


def test_hierarchy_check_is_silent_without_depth():
    rows = [
        _row(1, "Cash", None, 10.0),
        _row(2, "Inventory", None, 20.0),
        _row(3, "Receivables", None, 30.0),
        _row(4, "Total Assets", None, 999.0),  # wrong on purpose
    ]
    part = RawPart("p1", rows, ["Balance Sheet", "As of December 31, 2025"])
    cols, _ = interpret(RawDoc(source=Path("x.pdf"), kind="pdf", parts=[part]))
    assert check_hierarchy(cols[0]) == []
