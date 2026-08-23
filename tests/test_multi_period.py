"""Statements laid out with a year in every column.

Client five sends six years side by side. Read one column at a time, five of
those years vanish and nothing downstream can tell: the column that was read
reconciles perfectly against the client's own subtotals, so the arithmetic gate
passes it. That is the one blind spot arithmetic structurally cannot cover --
it verifies what was read, never what was never read.

Whether a header row names years or names companies is decided by the period
parser that already exists, not by a new vocabulary. Measured on the real
files: client five's headers read as periods 100% of the time; AOK Holdings'
entity tabs and consolidation schedules manage 0-50%.
"""

from __future__ import annotations

from datetime import date

import pytest

from fsa.ingest.interpret import period_columns
from fsa.ingest.raw import RawRow
from fsa.model.schema import StatementType


def row(index, label, texts=(), values=(), cols=()):
    return RawRow(
        index=index, label=label, values=list(values),
        value_cols=list(cols), texts=list(texts),
    )


def grid(headers, first_col=2):
    """A header row plus two data rows carrying figures beneath it."""
    cols = list(range(first_col, first_col + len(headers)))
    return [
        row(1, None, texts=list(zip(cols, headers))),
        row(2, "Cash", values=[100.0] * len(cols), cols=cols),
        row(3, "Receivables", values=[200.0] * len(cols), cols=cols),
    ]


# --------------------------------------------------------------------------
# reading a figure out of a named column
# --------------------------------------------------------------------------


def test_a_row_can_be_read_by_column():
    r = row(1, "Cash", values=[10.0, 20.0, 30.0], cols=[2, 3, 4])
    assert r.value_at(2) == 10.0
    assert r.value_at(4) == 30.0
    assert r.value_at(9) is None


def test_no_column_means_the_first_figure():
    """What every caller wanted before grids had to be read."""
    r = row(1, "Cash", values=[10.0, 20.0], cols=[2, 3])
    assert r.value_at(None) == 10.0
    assert row(1, "Blank").value_at(None) is None


# --------------------------------------------------------------------------
# years versus companies
# --------------------------------------------------------------------------


def test_a_row_of_dates_is_a_period_header():
    rows = grid(["Dec 31, 19", "Dec 31, 20", "Dec 31, 21"])
    found = period_columns(rows, None, StatementType.BS)
    assert [p.end for _, p in sorted(found.items())] == [
        date(2019, 12, 31), date(2020, 12, 31), date(2021, 12, 31)
    ]


def test_a_row_of_company_names_is_not():
    """AOK Holdings' tabs and schedules are entities, and lose nothing."""
    rows = grid(["US", "Canada", "Bermuda", "Eliminations", "Total"])
    assert period_columns(rows, None, StatementType.BS) == {}


def test_a_total_column_drops_out_on_its_own():
    """Client five's Profit and Loss ends in a TOTAL column across the years."""
    rows = grid(["Jan - Dec 19", "Jan - Dec 20", "TOTAL"])
    found = period_columns(rows, None, StatementType.IS)
    assert len(found) == 2
    assert all(p.months == 12 for p in found.values())


def test_a_mostly_prose_row_is_not_a_header():
    rows = grid(["interest income", "January - December 2023"])
    assert period_columns(rows, None, StatementType.IS) == {}


def test_one_period_is_not_a_grid():
    rows = grid(["As of December 31, 2024"])
    assert period_columns(rows, None, StatementType.BS) == {}


def test_the_same_date_repeated_is_a_layout_not_a_history():
    rows = grid(["Dec 31, 24", "Dec 31, 24"])
    assert period_columns(rows, None, StatementType.BS) == {}


# --------------------------------------------------------------------------
# headings must sit over the figures
# --------------------------------------------------------------------------


def test_headings_offset_from_the_figures_are_refused():
    """Client five's interim balance sheet heads columns 3 and 4 while its
    figures sit in 2 and 3.

    Taking the headings at face value there files August 2025 under August 2024
    and leaves the other year empty. Guessing the offset is the kind of repair
    that quietly invents a number, so the grid is declined and the segment
    falls back to reading a single period.
    """
    rows = [
        row(1, None, texts=[(3, "Aug 31, 25"), (4, "Aug 31, 24")]),
        row(2, "Cash", values=[631535.39, 236533.33], cols=[2, 3]),
    ]
    assert period_columns(rows, None, StatementType.BS) == {}


def test_a_partly_aligned_header_keeps_only_the_columns_that_line_up():
    rows = [
        row(1, None, texts=[(2, "Dec 31, 22"), (3, "Dec 31, 23"), (9, "Dec 31, 24")]),
        row(2, "Cash", values=[1.0, 2.0], cols=[2, 3]),
    ]
    found = period_columns(rows, None, StatementType.BS)
    assert sorted(found) == [2, 3]


def test_a_header_with_no_figures_at_all_is_refused():
    rows = [row(1, None, texts=[(2, "Dec 31, 22"), (3, "Dec 31, 23")])]
    assert period_columns(rows, None, StatementType.BS) == {}


# --------------------------------------------------------------------------
# the widest header wins
# --------------------------------------------------------------------------


def test_the_richest_period_row_is_chosen():
    """A statement can mention a date above its real header row."""
    rows = [
        row(1, None, texts=[(2, "As of December 31, 2024")]),
        row(2, None, texts=[(2, "Dec 31, 22"), (3, "Dec 31, 23"), (4, "Dec 31, 24")]),
        row(3, "Cash", values=[1.0, 2.0, 3.0], cols=[2, 3, 4]),
    ]
    assert len(period_columns(rows, None, StatementType.BS)) == 3
