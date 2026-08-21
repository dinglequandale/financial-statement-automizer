"""Tests for the mapping-level policy and the two-pass break_out flow.

These run against a fake client, so the whole L4 control flow is covered without
spending anything. That matters more than usual here: the behaviour under test
exists specifically to tame run-to-run variance, and a test that itself called
the API would inherit that variance.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fsa.mapping.llm import MappingLevel, propose_unresolved
from fsa.mapping.matcher import UNRESOLVED_LABEL
from fsa.model.mapping import Decider, MappingRule, MappingSet, Target
from fsa.model.schema import (
    ConsolidatedRow,
    ConsolidatedTable,
    RowKind,
    StatementType,
)

IS = StatementType.IS


class _Line:
    _next_row = 10

    def __init__(self, label, block=None, placeholder=False):
        self.label = label
        self.block = block
        self.placeholder = placeholder
        self.is_target = True
        _Line._next_row += 1
        self.row = _Line._next_row
        self.frozen = False
        self.derived = False
        self.collapsible = False


class _Block:
    def __init__(self, label, statement):
        self.subtotal_label = label
        self.statement = statement
        self.first_row = 1
        self.last_row = 99

    def contains(self, _row):
        return True


class _Spec:
    def __init__(self, labels):
        self._d = {l.lower(): _Line(l) for l in labels}
        self.blocks = []
        self.lines = list(self._d.values())

    def find(self, _st, label):
        return self._d.get((label or "").lower())

    def targets(self, _st):
        return list(self._d.values())


SPEC = _Spec(["Revenue", "Cost of Revenue", "Operating Expenses", "Freight Income"])


class FakeClient:
    """Records every payload it is asked about and replays canned answers."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.payloads = []

    @property
    def messages(self):
        return self

    def stream(self, **kw):
        self.payloads.append(kw["messages"][0]["content"])
        proposals = self.answers.pop(0) if self.answers else []
        body = json.dumps({"proposals": proposals})
        resp = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=body)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=20),
        )

        class _Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: resp)

            def __exit__(self_inner, *a):
                return False

        return _Ctx()


def _table():
    """Revenue detail under a subtotal, plus one expense subtotal."""
    rows = [
        ConsolidatedRow("Job Income", "job income", RowKind.DATA, "Income", {2025: 1.0}, {}, 2),
        ConsolidatedRow("Freight Income", "freight income", RowKind.DATA, "Income", {2025: 1.0}, {}, 2),
        ConsolidatedRow("Total Income", "total income", RowKind.SUBTOTAL, None, {2025: 2.0}, {}, 1),
        ConsolidatedRow("Rent", "rent", RowKind.DATA, "Expense", {2025: 1.0}, {}, 2),
        ConsolidatedRow("Total Expense", "total expense", RowKind.SUBTOTAL, None, {2025: 1.0}, {}, 1),
        ConsolidatedRow("Bank Fees", "bank fees", RowKind.DATA, None, {2025: 1.0}, {}, 1),
    ]
    return ConsolidatedTable(statement=IS, years=[2025], rows=rows)


def _ms(table):
    ms = MappingSet(statement=IS)
    for row in table.rows:
        ms.rules.append(
            MappingRule(
                client_account=row.raw_label,
                norm_account=row.norm_label,
                target=Target(statement=IS, label=UNRESOLVED_LABEL),
                decided_by=Decider.UNRESOLVED,
                confidence=0.0,
            )
        )
    return ms


def _asked(payload):
    """Account labels the payload actually put in front of the model."""
    names = ["Job Income", "Freight Income", "Total Income", "Rent", "Total Expense", "Bank Fees"]
    return {n for n in names if n in payload}


def test_detail_level_never_shows_the_client_subtotals():
    table = _table()
    ms = _ms(table)
    fake = FakeClient([[]])
    propose_unresolved(ms, table, SPEC, client=fake, level=MappingLevel.DETAIL)
    asked = _asked(fake.payloads[0])
    assert "Total Income" not in asked and "Total Expense" not in asked
    assert {"Job Income", "Rent", "Bank Fees"} <= asked


def test_rollup_level_shows_subtotals_and_orphans_only():
    table = _table()
    ms = _ms(table)
    fake = FakeClient([[]])
    propose_unresolved(ms, table, SPEC, client=fake, level=MappingLevel.ROLLUP)
    asked = _asked(fake.payloads[0])
    assert {"Total Income", "Total Expense"} <= asked
    # `Bank Fees` sits under no subtotal, so nothing can roll it up.
    assert "Bank Fees" in asked
    assert "Job Income" not in asked and "Rent" not in asked


def test_all_level_is_the_unfiltered_control():
    table = _table()
    ms = _ms(table)
    fake = FakeClient([[]])
    propose_unresolved(ms, table, SPEC, client=fake, level=MappingLevel.ALL)
    assert _asked(fake.payloads[0]) == {
        "Job Income", "Freight Income", "Total Income", "Rent", "Total Expense", "Bank Fees"
    }
    assert len(fake.payloads) == 1  # no second pass


def _p(account, kind, target, **kw):
    return {
        "account": account,
        "kind": kind,
        "target": target,
        "block": kw.get("block", ""),
        "slot": kw.get("slot", ""),
        "sign": kw.get("sign", 1),
        "confidence": kw.get("confidence", 0.9),
        "rationale": "because",
    }


def test_break_out_triggers_a_second_pass_for_that_group_only():
    """The mixed-level case: revenue broken out, expenses rolled up."""
    table = _table()
    ms = _ms(table)
    fake = FakeClient(
        [
            [
                _p("Total Income", "break_out", "components differ"),
                _p("Total Expense", "assign", "Operating Expenses"),
                _p("Bank Fees", "assign", "Operating Expenses"),
            ],
            [
                _p("Job Income", "assign", "Revenue"),
                _p("Freight Income", "assign", "Freight Income"),
            ],
        ]
    )
    propose_unresolved(ms, table, SPEC, client=fake, level=MappingLevel.AUTO)

    assert len(fake.payloads) == 2
    second = _asked(fake.payloads[1])
    assert {"Job Income", "Freight Income"} <= second
    # `Rent` belongs to the rolled-up group and must NOT be asked again.
    assert "Rent" not in second

    by = {r.client_account: r for r in ms.rules}
    assert by["Job Income"].target.label == "Revenue"
    assert by["Freight Income"].target.label == "Freight Income"
    assert by["Total Expense"].target.label == "Operating Expenses"


def test_a_broken_out_subtotal_is_left_unmapped():
    """Mapping it as well as its children would double-count."""
    table = _table()
    ms = _ms(table)
    fake = FakeClient(
        [[_p("Total Income", "break_out", "components differ")],
         [_p("Job Income", "assign", "Revenue")]]
    )
    propose_unresolved(ms, table, SPEC, client=fake, level=MappingLevel.AUTO)
    total = next(r for r in ms.rules if r.client_account == "Total Income")
    assert total.decided_by is Decider.UNRESOLVED
    assert "broken out" in (total.rationale or "")


def test_no_break_out_means_no_second_call():
    table = _table()
    ms = _ms(table)
    fake = FakeClient([[_p("Total Income", "assign", "Revenue")]])
    res = propose_unresolved(ms, table, SPEC, client=fake, level=MappingLevel.AUTO)
    assert len(fake.payloads) == 1
    assert res.proposed == 1


def test_second_pass_tokens_are_added_to_the_reported_cost():
    table = _table()
    ms = _ms(table)
    fake = FakeClient(
        [[_p("Total Income", "break_out", "x")], [_p("Job Income", "assign", "Revenue")]]
    )
    res = propose_unresolved(ms, table, SPEC, client=fake, level=MappingLevel.AUTO)
    assert res.input_tokens == 20 and res.output_tokens == 40  # both calls counted


def test_nothing_unresolved_makes_no_call_at_all():
    table = _table()
    ms = MappingSet(statement=IS)
    fake = FakeClient([])
    res = propose_unresolved(ms, table, SPEC, client=fake, level=MappingLevel.AUTO)
    assert fake.payloads == []
    assert res.proposed == 0
