"""Column identity: which period a column covers, and whose figures they are.

Everything here exists because sample 3 got all of it wrong silently. A
trailing-twelve-month income statement was filed as a fiscal year, a
subsidiary's balance sheet stood in for the consolidated group, and a
two-page PDF lost its second page -- none of which raised anything.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fsa.ingest.raw import parse_period
from fsa.ingest.scope import (
    check_period_coverage,
    infer_missing_period_ends,
    check_period_basis,
    looks_like_rollup,
    merge_pages,
    select_scope,
)
from fsa.model.schema import (
    AccountRow,
    ColumnRole,
    ExtractedColumn,
    RowKind,
    StatementSet,
    StatementType,
)


def col(
    statement=StatementType.BS,
    year=2024,
    entity=None,
    end=None,
    months=None,
    sheet="Sheet1",
    file="a.xlsx",
    nrows=1,
):
    return ExtractedColumn(
        statement=statement,
        fiscal_year=year,
        role=ColumnRole.PRIMARY,
        source_file=Path(file),
        source_sheet=sheet,
        value_column="v0",
        label_column="label",
        period_end=end or date(year, 12, 31),
        period_months=months,
        entity=entity,
        rows=[
            AccountRow(
                raw_label=f"row{i}",
                norm_label=f"row{i}",
                kind=RowKind.DATA,
                value=1.0,
                section=None,
                row_index=i,
            )
            for i in range(nrows)
        ],
    )


# --------------------------------------------------------------------------
# period parsing: what it must refuse
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,end",
    [
        ("March 2025", date(2025, 3, 31)),
        ("consolidated balance sheet - March 2025", date(2025, 3, 31)),
        ("Dec 25", date(2025, 12, 31)),
        ("August 2024", date(2024, 8, 31)),
    ],
)
def test_a_named_month_resolves_to_its_own_month_end(text, end):
    """A March statement must never become a December year end."""
    p = parse_period(text)
    assert p is not None and p.end == end


@pytest.mark.parametrize("text", ["2025", "FY2023", "2024 Financials"])
def test_a_bare_year_does_not_determine_a_year_end(text):
    # The client's year end is not knowable from a year alone, and answering
    # December 31st is only right for calendar-year clients.
    assert parse_period(text) is None


@pytest.mark.parametrize(
    "text",
    ["2025 Budget", "FY25 Forecast", "Restated 2023", "Printed 11/04/2025 at 3:42 PM"],
)
def test_a_date_that_is_not_an_actual_period_is_refused(text):
    assert parse_period(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "April 1, 2024 - March 31, 2025",
        "For the period 4.1.24 - 3.31.25",
        "Comparative: 12/31/2023 and 12/31/2024",
    ],
)
def test_two_dates_are_a_span_and_are_not_read_as_the_first_one(text):
    """Taking the first date reports a period ending when it started."""
    assert parse_period(text) is None


def test_the_shapes_it_does_read_still_work():
    assert parse_period("As of December 31, 2025").end == date(2025, 12, 31)
    assert parse_period("January through August 2025").months == 8
    assert parse_period("for the period ended June 30, 2024").end == date(2024, 6, 30)


# --------------------------------------------------------------------------
# scope selection
# --------------------------------------------------------------------------


def test_rollup_naming_covers_entity_words_and_statement_names():
    assert looks_like_rollup("consolidated")
    assert looks_like_rollup("Combined Entities")
    assert looks_like_rollup("HoldCo")
    # A tab named after the statement is the statement, not a component.
    assert looks_like_rollup("Profit and Loss")
    assert looks_like_rollup("Balance Sheet")
    assert not looks_like_rollup("US")
    assert not looks_like_rollup("Bermuda")


def test_the_rollup_wins_and_the_components_are_dropped():
    ss = StatementSet()
    for name in ("consolidated", "US", "Canada", "Bermuda"):
        ss.add(col(entity=name, sheet=name))
    findings = select_scope(ss)

    assert len(ss.columns) == 1
    assert ss.columns[0].entity == "consolidated"
    assert [f.code for f in findings] == ["entity_scope_selected"]


def test_an_unrecognised_rollup_is_an_error_not_a_silent_pick():
    ss = StatementSet()
    for name in ("Alpha", "Beta"):
        ss.add(col(entity=name, sheet=name))
    findings = select_scope(ss)

    assert [f.code for f in findings] == ["entity_scope_ambiguous"]
    assert findings[0].severity.value == "error"
    # It still yields one column so the run can continue and be reviewed.
    assert len(ss.columns) == 1


def test_two_rollup_candidates_are_also_ambiguous():
    ss = StatementSet()
    ss.add(col(entity="consolidated", sheet="consolidated", file="a.xlsx"))
    ss.add(col(entity="consolidated", sheet="consolidated", file="b.xlsx"))
    findings = select_scope(ss)
    assert findings[0].code == "entity_scope_ambiguous"


def test_one_column_per_slot_is_left_alone():
    ss = StatementSet()
    ss.add(col(entity="consolidated", year=2023))
    ss.add(col(entity="consolidated", year=2024))
    assert select_scope(ss) == []
    assert len(ss.columns) == 2


# --------------------------------------------------------------------------
# pages vs entities
# --------------------------------------------------------------------------


def test_pages_of_one_pdf_are_joined_rather_than_contested():
    """`p1`/`p2` are pagination. Choosing between them drops half a statement."""
    ss = StatementSet()
    ss.add(col(entity=None, sheet="p1", file="bs.pdf", nrows=3))
    ss.add(col(entity=None, sheet="p2", file="bs.pdf", nrows=2))

    findings = merge_pages(ss)

    assert len(ss.columns) == 1
    assert len(ss.columns[0].rows) == 5
    assert [f.code for f in findings] == ["pages_joined"]
    # And nothing is left for scope selection to argue about.
    assert select_scope(ss) == []


def test_named_entities_are_never_merged_as_pages():
    ss = StatementSet()
    ss.add(col(entity="US", sheet="US", file="bs.xlsx", nrows=3))
    ss.add(col(entity="Canada", sheet="Canada", file="bs.xlsx", nrows=2))
    assert merge_pages(ss) == []
    assert len(ss.columns) == 2


# --------------------------------------------------------------------------
# period basis
# --------------------------------------------------------------------------


def test_an_odd_year_end_among_december_years_is_an_error():
    ss = StatementSet()
    for y in (2021, 2022, 2023):
        ss.add(col(year=y, end=date(y, 12, 31)))
    ss.add(col(year=2024, end=date(2024, 6, 30)))

    findings = check_period_basis(ss)
    codes = [f.code for f in findings]

    assert "period_basis_mismatch" in codes
    odd = next(f for f in findings if f.code == "period_basis_mismatch")
    assert odd.fiscal_year == 2024
    assert odd.severity.value == "error"


def test_a_weekday_year_end_is_not_a_change_of_basis():
    """TS Distributors closes on Dec 30 one year and Dec 1 another.

    A client on a 52/53-week fiscal calendar moves its close date within the
    month every year. Comparing exact days made every such client look like it
    had changed reporting basis; only the month is load-bearing.
    """
    ss = StatementSet()
    for y, day in ((2022, 31), (2023, 1), (2024, 30)):
        ss.add(col(year=y, end=date(y, 12, day)))
    assert check_period_basis(ss) == []


def test_a_consistent_year_end_raises_nothing_even_when_it_is_not_december():
    ss = StatementSet()
    for y in (2022, 2023, 2024):
        ss.add(col(year=y, end=date(y, 6, 30)))
    assert check_period_basis(ss) == []


def test_a_stub_is_flagged_by_its_length_not_only_its_date():
    ss = StatementSet()
    ss.add(col(statement=StatementType.IS, year=2024, end=date(2024, 12, 31), months=12))
    ss.add(col(statement=StatementType.IS, year=2025, end=date(2025, 12, 31), months=8))

    codes = [f.code for f in check_period_basis(ss)]
    assert "partial_period" in codes


def test_a_trailing_twelve_month_column_is_caught_by_its_year_end():
    """TTM is 12 months, so length alone cannot catch it -- the close date can."""
    ss = StatementSet()
    for y in (2022, 2023, 2024):
        ss.add(col(statement=StatementType.IS, year=y, end=date(y, 12, 31), months=12))
    ss.add(col(statement=StatementType.IS, year=2025, end=date(2025, 3, 31), months=12))

    findings = check_period_basis(ss)
    assert [f.code for f in findings] == ["period_basis_mismatch"]
    assert findings[0].fiscal_year == 2025


# --------------------------------------------------------------------------
# coverage: an unchecked column must not look like a checked one
# --------------------------------------------------------------------------


def test_an_income_statement_takes_its_date_from_its_own_balance_sheet():
    """The multi-column extractor dates balance sheets and not income statements.

    `discover.py` identifies IS columns by a `YTD <year>` header, which names a
    year and no date, so it records `period_end=None`. A company's income
    statement ends when its balance sheet does, so the date is recoverable from
    the client's own figures.
    """
    ss = StatementSet()
    ss.add(col(statement=StatementType.BS, year=2024, end=date(2024, 12, 30), file="fy24.xlsx"))
    ss.add(col(statement=StatementType.IS, year=2024, end=None, file="fy24.xlsx"))
    ss.columns[1].period_end = None  # what discover.py actually produces

    findings = infer_missing_period_ends(ss)

    assert ss.columns[1].period_end == date(2024, 12, 30)
    assert [f.code for f in findings] == ["period_end_from_balance_sheet"]
    # The date is recoverable; the length is not, and must not be invented.
    assert ss.columns[1].period_months is None


def test_the_same_workbook_is_preferred_when_several_balance_sheets_match():
    ss = StatementSet()
    ss.add(col(statement=StatementType.BS, year=2024, end=date(2024, 6, 30), file="other.xlsx"))
    ss.add(col(statement=StatementType.BS, year=2024, end=date(2024, 12, 31), file="mine.xlsx"))
    ss.add(col(statement=StatementType.IS, year=2024, file="mine.xlsx"))
    ss.columns[2].period_end = None

    infer_missing_period_ends(ss)
    assert ss.columns[2].period_end == date(2024, 12, 31)


def test_an_undated_column_is_reported_rather_than_silently_skipped():
    """Every period check needs a date, so an undated column is skipped by all
    of them. Saying nothing makes 'unverified' indistinguishable from 'clean'."""
    ss = StatementSet()
    c = col(statement=StatementType.IS, year=2023)
    c.period_end = None
    ss.add(c)

    findings = check_period_coverage(ss)
    assert [f.code for f in findings] == ["period_unverified"]
    assert "2023" in findings[0].message
    # And the basis check genuinely does skip it, which is why this matters.
    assert check_period_basis(ss) == []


def test_a_dated_income_statement_of_unknown_length_is_still_reported():
    ss = StatementSet()
    ss.add(col(statement=StatementType.IS, year=2023, end=date(2023, 12, 31), months=None))
    codes = [f.code for f in check_period_coverage(ss)]
    assert codes == ["period_length_unverified"]


def test_a_fully_established_period_reports_nothing():
    ss = StatementSet()
    ss.add(col(statement=StatementType.BS, year=2023, end=date(2023, 12, 31)))
    ss.add(col(statement=StatementType.IS, year=2023, end=date(2023, 12, 31), months=12))
    assert check_period_coverage(ss) == []
