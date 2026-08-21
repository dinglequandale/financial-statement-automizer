"""Finishing a mapping from the client's own grouping (`fsa/mapping/complete.py`).

The rules exist to turn N unanswerable rows into one answerable one, so the
tests are mostly about when they must *decline*: a rule that fires on a group
the client deliberately split, or invents a name nobody chose, costs more trust
than the rows it saves.
"""

from __future__ import annotations

import pytest

from fsa.mapping.complete import (
    _rollup_name,
    complete_by_rollup,
    complete_by_siblings,
)
from fsa.model.mapping import Decider, Exclusion, MappingRule, MappingSet, Target
from fsa.model.schema import (
    ConsolidatedRow,
    ConsolidatedTable,
    RowKind,
    StatementType,
)
from fsa.review.sheet import LOW_CONFIDENCE

YEAR = 2025
IS = StatementType.IS

_TEMPLATE = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "Konrad Project"
    / "sample_1_TS"
    / "BVAL Model (Weaver Template).xlsx"
)


@pytest.fixture(scope="module")
def spec():
    if not _TEMPLATE.is_file():
        pytest.skip(f"template not available at {_TEMPLATE}")
    from fsa.mapping.template import load_template

    return load_template(_TEMPLATE)


def _row(label, value, section, kind=RowKind.DATA, depth=2):
    return ConsolidatedRow(
        raw_label=label,
        norm_label=label.lower().strip(),
        kind=kind,
        section=section,
        values={YEAR: value},
        depth=depth,
    )


def _table(*rows):
    t = ConsolidatedTable(statement=IS, years=[YEAR])
    t.rows = list(rows)
    return t


def _rule(account, label=None, block=None, *, by=Decider.LLM, conf=0.9):
    return MappingRule(
        client_account=account,
        norm_account=account.lower().strip(),
        target=Target(statement=IS, label=label or "", block=block),
        confidence=conf if label else 0.0,
        decided_by=by if label else Decider.UNRESOLVED,
    )


def _set(*rules, exclusions=()):
    ms = MappingSet(statement=IS)
    ms.rules.extend(rules)
    ms.exclusions.extend(exclusions)
    return ms


# ---------------------------------------------------------------------------
# sibling completion
# ---------------------------------------------------------------------------


def test_extends_a_bucket_to_its_unresolved_siblings(spec):
    """TS's operating expenses: 28 of 37 already on one bucket line."""
    t = _table(
        _row("Postage", 100.0, "Operating Expenses"),
        _row("Printing", 100.0, "Operating Expenses"),
        _row("Dues", 100.0, "Operating Expenses"),
        _row("Janitorial", 10.0, "Operating Expenses"),
    )
    ms = _set(
        _rule("Postage", "Other Operating Expenses", "Operating Expenses"),
        _rule("Printing", "Other Operating Expenses", "Operating Expenses"),
        _rule("Dues", "Other Operating Expenses", "Operating Expenses"),
        _rule("Janitorial"),
    )
    rep = complete_by_siblings(ms, t, spec)
    assert rep.completed == 1
    done = ms.rules[-1]
    assert done.target.label == "Other Operating Expenses"
    assert done.decided_by is Decider.STRUCTURE
    assert done.confidence < LOW_CONFIDENCE, "must be reviewed, never skimmed"


def test_declines_when_every_sibling_has_its_own_line(spec):
    """TS's cost of goods sold: six resolved members, six distinct targets.

    There is no bucket, so extending whichever name wins a tiebreak would have
    produced `Freight - Outbound -> Supplier Discounts`.
    """
    t = _table(
        _row("Supplier Discounts", 100.0, "COGS"),
        _row("Product Repair", 100.0, "COGS"),
        _row("Inventory Adj", 100.0, "COGS"),
        _row("Freight - Outbound", 10.0, "COGS"),
    )
    ms = _set(
        _rule("Supplier Discounts", "Supplier Discounts", "Cost of Revenue"),
        _rule("Product Repair", "Product Repair Expense", "Cost of Revenue"),
        _rule("Inventory Adj", "Inventory Adjustments", "Cost of Revenue"),
        _rule("Freight - Outbound"),
    )
    rep = complete_by_siblings(ms, t, spec)
    assert rep.completed == 0
    assert rep.groups_skipped_specific == 1
    assert ms.rules[-1].decided_by is Decider.UNRESOLVED


def test_declines_when_the_group_splits_across_blocks(spec):
    """Commercial Flooring's fixed assets go to three different lines on
    purpose. A group the analyst split is not a group we may extend."""
    t = _table(
        _row("Machinery and Equipment", 100.0, "Fixed Assets"),
        _row("Autos and Trucks", 100.0, "Fixed Assets"),
        _row("Land", 100.0, "Fixed Assets"),
    )
    ms = _set(
        _rule("Machinery and Equipment", "Equipment", "Total Fixed Assets (Gross)"),
        _rule("Autos and Trucks", "Autos & Trucks", "Net Fixed Assets"),
        _rule("Land"),
    )
    rep = complete_by_siblings(ms, t, spec)
    assert rep.completed == 0
    assert rep.groups_skipped_split == 1


def test_declines_when_the_known_part_is_a_minority_of_the_money(spec):
    """Inferring a $3M account from two $100 ones is backwards."""
    t = _table(
        _row("Postage", 100.0, "Operating Expenses"),
        _row("Printing", 100.0, "Operating Expenses"),
        _row("Salaries - Warehouse", 3_000_000.0, "Operating Expenses"),
    )
    ms = _set(
        _rule("Postage", "Other Operating Expenses", "Operating Expenses"),
        _rule("Printing", "Other Operating Expenses", "Operating Expenses"),
        _rule("Salaries - Warehouse"),
    )
    rep = complete_by_siblings(ms, t, spec)
    assert rep.completed == 0


def test_a_single_resolved_sibling_is_not_evidence(spec):
    t = _table(
        _row("Postage", 100.0, "Operating Expenses"),
        _row("Printing", 10.0, "Operating Expenses"),
    )
    ms = _set(
        _rule("Postage", "Other Operating Expenses", "Operating Expenses"),
        _rule("Printing"),
    )
    assert complete_by_siblings(ms, t, spec).completed == 0


def test_never_touches_an_excluded_account(spec):
    t = _table(
        _row("Postage", 100.0, "Operating Expenses"),
        _row("Printing", 100.0, "Operating Expenses"),
        _row("Dues", 100.0, "Operating Expenses"),
        _row("Janitorial", 10.0, "Operating Expenses"),
    )
    ms = _set(
        _rule("Postage", "Other Operating Expenses", "Operating Expenses"),
        _rule("Printing", "Other Operating Expenses", "Operating Expenses"),
        _rule("Dues", "Other Operating Expenses", "Operating Expenses"),
        _rule("Janitorial"),
        exclusions=[
            Exclusion(client_account="Janitorial", norm_account="janitorial",
                      reason="covered", decided_by=Decider.STRUCTURE)
        ],
    )
    assert complete_by_siblings(ms, t, spec).completed == 0


# ---------------------------------------------------------------------------
# rollup completion
# ---------------------------------------------------------------------------


def _salaries_table():
    """A group we mostly failed to answer, with a rollup that ties."""
    return _table(
        _row("Salaries - Sales", 200.0, "Salaries & Wage Expense"),
        _row("Salaries - Warehouse", 700.0, "Salaries & Wage Expense"),
        _row("PR Tax - Admin", 100.0, "Salaries & Wage Expense"),
        _row("Total Salaries & Wages", 1000.0, "Salaries & Wage Expense",
             kind=RowKind.SUBTOTAL, depth=1),
    )


def test_answers_a_not_understood_group_at_its_rollup(spec):
    t = _salaries_table()
    ms = _set(
        _rule("Salaries - Sales", "Salaries & Wages", "Operating Expenses"),
        _rule("Salaries - Warehouse"),
        _rule("PR Tax - Admin"),
        _rule("Total Salaries & Wages"),
    )
    rep = complete_by_rollup(ms, t, spec)
    assert rep.completed == 1
    rollup = ms.for_account("total salaries & wages")
    assert rollup.decided_by is Decider.STRUCTURE
    assert rollup.target.block == "Operating Expenses"
    assert rollup.target.label == "Salaries & Wages"  # the client's own name


def test_leaves_a_group_alone_when_most_of_it_is_understood(spec):
    """TS's cost of goods sold: 97% of the money resolved, six small strays.
    Mapping the rollup there would discard six good answers."""
    t = _table(
        _row("Purchases", 9700.0, "COGS"),
        _row("Freight - Outbound", 200.0, "COGS"),
        _row("Freight - Transfer", 100.0, "COGS"),
        _row("Total COGS", 10000.0, "COGS", kind=RowKind.SUBTOTAL, depth=1),
    )
    ms = _set(
        _rule("Purchases", "Cost of Revenue", "Cost of Revenue"),
        _rule("Freight - Outbound"),
        _rule("Freight - Transfer"),
        _rule("Total COGS"),
    )
    assert complete_by_rollup(ms, t, spec).completed == 0


def test_does_not_overrule_an_answered_rollup(spec):
    t = _salaries_table()
    ms = _set(
        _rule("Salaries - Sales", "Salaries & Wages", "Operating Expenses"),
        _rule("Salaries - Warehouse"),
        _rule("PR Tax - Admin"),
        _rule("Total Salaries & Wages", "SG&A Expenses", "Operating Expenses",
              by=Decider.HUMAN, conf=1.0),
    )
    assert complete_by_rollup(ms, t, spec).completed == 0


def test_declines_when_the_rollup_does_not_tie(spec):
    """A row that is not the sum of its group is a computed result, and
    mapping it would carry a number the client never grouped."""
    t = _table(
        _row("Salaries - Sales", 200.0, "Salaries & Wage Expense"),
        _row("Salaries - Warehouse", 700.0, "Salaries & Wage Expense"),
        _row("PR Tax - Admin", 100.0, "Salaries & Wage Expense"),
        _row("Total Salaries & Wages", 9999.0, "Salaries & Wage Expense",
             kind=RowKind.SUBTOTAL, depth=1),
    )
    ms = _set(
        _rule("Salaries - Sales", "Salaries & Wages", "Operating Expenses"),
        _rule("Salaries - Warehouse"),
        _rule("PR Tax - Admin"),
        _rule("Total Salaries & Wages"),
    )
    assert complete_by_rollup(ms, t, spec).completed == 0


# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,want",
    [
        ("Total Salaries & Wages", "Salaries & Wages"),
        ("TOTAL REVENUE", "REVENUE"),
        ("Net Other Income", "Other Income"),
        ("Total Cost of Goods Sold", "Cost of Goods Sold"),
        ("Subtotal", "Subtotal"),        # not a prefix word; left alone
        ("Totals", "Totals"),            # prefix must be a whole word
        ("Total", "Total"),              # nothing left to strip to
    ],
)
def test_rollup_name_uses_the_clients_own_vocabulary(raw, want):
    assert _rollup_name(raw) == want
