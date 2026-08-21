"""The one check that needs no foresight.

Every other check asks a question someone thought to ask: is this period a
stub, is this tab the group, is this a cash flow statement. Each was added
after a client broke it, and each required knowing the dimension in advance.

This one asks whether the client's own totals hold. A statement is a closed
arithmetic system and the client already did the arithmetic, so a reading that
cannot reproduce their subtotals is a wrong reading -- whatever the cause, at
any client, in any format, without anyone predicting it.

Measured across four engagements: TS Distributors, Commercial Flooring and AOK
Holdings reproduce every client subtotal exactly; Ram Rod Utilities misses nine
of them by margins like `Total Fixed Assets = 60,595` against components summing
to `2,076`. The gate has to leave the first three alone and stop the fourth.
"""

from __future__ import annotations

import pytest

from fsa.model.schema import Finding, Severity, StatementType
from fsa.validate.checks import (
    MATERIAL_FLOOR,
    MATERIAL_SHARE,
    _is_material,
    extraction_unreliable,
)


def mismatch(expected, actual, statement=StatementType.BS, year=2024, account="Total Assets"):
    return Finding(
        severity=Severity.WARNING,
        code="subtotal_mismatch",
        message=f"subtotal {account!r} = {actual} but components sum to {expected}",
        statement=statement,
        fiscal_year=year,
        account=account,
        detail={"expected": expected, "actual": actual},
    )


# --------------------------------------------------------------------------
# materiality: rounding is not misreading
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected,actual,material",
    [
        (2075.63, 60595.40, True),    # Ram Rod: the block was not read at all
        (69695.56, 2534.16, True),    # Ram Rod: components and total swapped scale
        (1000000.0, 1000000.40, False),  # AOK: 40 cents across a balance sheet
        (1000000.0, 999999.0, False),    # a dollar on a million is rounding
        (100.0, 100.0, False),           # exact
        (0.0, 0.0, False),               # an empty block cannot be material
        (100.0, 150.0, True),            # 50% off a small block still matters
    ],
)
def test_materiality_separates_rounding_from_misreading(expected, actual, material):
    assert _is_material(expected, actual) is material


def test_a_missing_figure_is_not_graded_as_material():
    """None means we read nothing there, which other checks report."""
    assert _is_material(None, 100.0) is False
    assert _is_material(100.0, None) is False


def test_the_absolute_floor_protects_near_zero_blocks():
    # Proportionally enormous, absolutely trivial.
    assert _is_material(0.10, 0.60) is False
    assert MATERIAL_FLOOR >= 1.0 and 0 < MATERIAL_SHARE < 0.1


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


def test_a_clean_statement_passes():
    assert extraction_unreliable([]) == []
    assert extraction_unreliable([mismatch(1000000.0, 1000000.40)]) == []


def test_a_material_mismatch_stops_the_year():
    out = extraction_unreliable([mismatch(2075.63, 60595.40)])
    assert len(out) == 1
    assert out[0].code == "extraction_unreliable"
    assert out[0].severity is Severity.ERROR
    assert out[0].fiscal_year == 2024


def test_each_affected_statement_year_is_reported_once():
    """One error per year, not one per broken subtotal -- the analyst needs to
    know which years cannot be trusted, not to read nine variations of it."""
    findings = [
        mismatch(1.0, 500.0, StatementType.BS, 2022, "Total Fixed Assets"),
        mismatch(2.0, 900.0, StatementType.BS, 2022, "Total Current Assets"),
        mismatch(3.0, 700.0, StatementType.IS, 2022, "Total Expenses"),
        mismatch(4.0, 800.0, StatementType.BS, 2023, "Total Assets"),
    ]
    out = extraction_unreliable(findings)
    slots = {(f.statement, f.fiscal_year) for f in out}
    assert slots == {
        (StatementType.BS, 2022),
        (StatementType.IS, 2022),
        (StatementType.BS, 2023),
    }
    bs22 = next(f for f in out if f.statement is StatementType.BS and f.fiscal_year == 2022)
    assert bs22.detail["failed_subtotals"] == 2


def test_the_worst_offender_is_named_so_the_analyst_knows_where_to_look():
    out = extraction_unreliable([
        mismatch(100.0, 200.0, account="Small Block"),
        mismatch(2075.63, 60595.40, account="Total Fixed Assets"),
    ])
    assert "Total Fixed Assets" in out[0].message


def test_other_findings_are_ignored():
    other = Finding(
        severity=Severity.WARNING, code="account_renamed", message="x",
        statement=StatementType.BS, fiscal_year=2024,
    )
    assert extraction_unreliable([other]) == []
