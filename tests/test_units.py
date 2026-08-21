"""Scale and currency: the two dimensions added before a client forced them.

Every other field in the column-identity tuple arrived after an engagement
broke on its absence. These two were named from accounting practice instead, on
the argument that the pattern of learning them one client at a time is the
problem rather than the method.

Both are invisible to arithmetic. A statement stated in thousands ties every
subtotal and balances every year; Canadian dollars added to US dollars do too.
The only evidence is the line of small type above the columns.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fsa.ingest.scope import check_denomination
from fsa.ingest.units import detect_currency, detect_scale, masthead
from fsa.model.schema import ColumnRole, ExtractedColumn, StatementSet, StatementType


def col(statement=StatementType.BS, year=2024, scale=1, label=None, currency=None):
    return ExtractedColumn(
        statement=statement, fiscal_year=year, role=ColumnRole.PRIMARY,
        source_file=Path("a.xlsx"), source_sheet="Sheet1",
        value_column="v0", label_column="label",
        period_end=date(year, 12, 31), scale=scale, scale_label=label,
        currency=currency,
    )


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,multiplier,label",
    [
        ("(In Thousands)", 1000, "thousands"),
        ("Consolidated Balance Sheets (in thousands)", 1000, "thousands"),
        ("Amounts in thousands of dollars", 1000, "thousands"),
        ("Dollars in thousands", 1000, "thousands"),
        ("($000's)", 1000, "thousands"),
        ("(in millions)", 1000000, "millions"),
        ("Dollars in millions", 1000000, "millions"),
    ],
)
def test_a_declared_scale_is_read(text, multiplier, label):
    assert detect_scale([text]) == (multiplier, label)


@pytest.mark.parametrize(
    "text",
    [
        "Balance Sheet",
        "As of December 31, 2024",
        "Thousand Oaks Property LLC",
        "Millions Air Hangar Lease",
        "Total Current Assets",
    ],
)
def test_ordinary_text_declares_no_scale(text):
    """A row *called* Thousand Oaks is not a scale declaration."""
    assert detect_scale([text]) == (1, None)


@pytest.mark.parametrize(
    "text,code",
    [
        ("(CAD)", "CAD"),
        ("Expressed in Canadian dollars", "CAD"),
        ("YTD (USD)", "USD"),
        ("In U.S. dollars", "USD"),
        ("Amounts in EUR", "EUR"),
    ],
)
def test_a_declared_currency_is_read(text, code):
    assert detect_currency([text]) == code


def test_an_unstated_currency_stays_unstated():
    """None means 'the client did not say', not 'dollars'.

    Inventing a code creates a disagreement the client never declared.
    """
    assert detect_currency(["Balance Sheet", "As of December 31, 2024"]) is None
    assert detect_currency(["Cadence Software Subscription"]) is None


def test_only_the_masthead_is_scanned():
    """Scanning the body would read every account label as a candidate."""
    class Row:
        def __init__(self, label): self.label = label
    rows = [Row(f"row {i}") for i in range(40)]
    head = masthead(["Balance Sheet"], rows)
    assert "Balance Sheet" in head
    assert "row 0" in head
    assert "row 39" not in head


# --------------------------------------------------------------------------
# refusal
# --------------------------------------------------------------------------


def test_figures_as_reported_pass_silently():
    ss = StatementSet()
    ss.add(col(year=2023))
    ss.add(col(year=2024))
    assert check_denomination(ss) == []


def test_a_thousands_statement_is_refused_not_rescaled():
    ss = StatementSet()
    ss.add(col(year=2024, scale=1000, label="thousands"))
    codes = [f.code for f in check_denomination(ss)]
    assert "scale_not_as_reported" in codes
    err = next(f for f in check_denomination(ss) if f.code == "scale_not_as_reported")
    assert err.severity.value == "error"
    assert "1,000" in err.message


def test_mixed_scale_across_years_is_reported_separately():
    """The dangerous case: each year ties perfectly on its own."""
    ss = StatementSet()
    ss.add(col(year=2023, scale=1))
    ss.add(col(year=2024, scale=1000, label="thousands"))
    codes = [f.code for f in check_denomination(ss)]
    assert "scale_mixed" in codes


def test_mixed_currency_is_refused():
    ss = StatementSet()
    ss.add(col(year=2024, currency="USD"))
    ss.add(col(year=2023, currency="CAD"))
    err = next(f for f in check_denomination(ss) if f.code == "currency_mixed")
    assert err.severity.value == "error"
    assert "CAD" in err.message and "USD" in err.message


def test_one_stated_currency_is_not_a_conflict():
    ss = StatementSet()
    ss.add(col(year=2024, currency="USD"))
    ss.add(col(year=2023, currency=None))
    assert [f.code for f in check_denomination(ss)] == []


def test_the_two_statements_are_judged_independently():
    ss = StatementSet()
    ss.add(col(StatementType.BS, 2024, currency="USD"))
    ss.add(col(StatementType.IS, 2024, currency="CAD"))
    # One currency each; no statement mixes, so nothing to refuse.
    assert [f.code for f in check_denomination(ss)] == []
