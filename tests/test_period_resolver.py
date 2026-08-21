"""The model layer's contract: escalate rarely, cache always, never guess.

A model in the ingest path is only defensible if it cannot make the pipeline
irreproducible and cannot invent an answer where the deterministic layer
declined. These tests pin both, and none of them touch the network.
"""

from __future__ import annotations

from datetime import date

import pytest

from fsa.ingest.period import PeriodResolver
from fsa.ingest.raw import Period
from fsa.model.schema import StatementType


class Recorder(PeriodResolver):
    """A resolver whose model answers are scripted, so calls are countable."""

    def __init__(self, answers=None, **kw):
        super().__init__(**kw)
        self.answers = answers or {}
        self.asked: list[str] = []

    def _available(self):
        # Mirrors production minus the API-key probe: a resolver that has
        # already failed once must stay switched off.
        return self.use_llm and not self._disabled

    def _ask(self, text):
        self.asked.append(text)
        self.calls += 1
        return self.answers.get(text)


# --------------------------------------------------------------------------
# escalation: only where structure is knowably short
# --------------------------------------------------------------------------


def test_a_caption_structure_fully_reads_never_reaches_the_model():
    r = Recorder()
    got, how = r.resolve("January through December 2024", StatementType.IS)
    assert how == "rules"
    assert got.months == 12
    assert r.asked == []


def test_a_balance_sheet_date_without_a_length_is_not_escalated():
    """A point in time has no length; `months=None` there is correct, not a gap."""
    r = Recorder()
    got, how = r.resolve("As of December 31, 2024", StatementType.BS)
    assert how == "rules"
    assert got.end == date(2024, 12, 31)
    assert r.asked == []


def test_an_income_statement_without_a_length_is_escalated():
    """This is what catches `Year Ended ...` without enumerating the phrasing."""
    r = Recorder({"For the Year Ended June 30, 2024": Period(date(2024, 6, 30), 12)})
    got, how = r.resolve("For the Year Ended June 30, 2024", StatementType.IS)
    assert how == "llm"
    assert (got.end, got.months) == (date(2024, 6, 30), 12)


def test_text_structure_cannot_read_at_all_is_escalated():
    r = Recorder({"TTM Ended March 31, 2025": Period(date(2025, 3, 31), 12)})
    got, how = r.resolve("TTM Ended March 31, 2025", StatementType.IS)
    assert how == "llm"
    assert got.months == 12


def test_text_with_no_digits_is_not_worth_a_call():
    r = Recorder()
    got, how = r.resolve("Accrual Basis", StatementType.IS)
    assert (got, how) == (None, "none")
    assert r.asked == []


# --------------------------------------------------------------------------
# declining
# --------------------------------------------------------------------------


def test_a_declined_caption_stays_unresolved_rather_than_guessed():
    r = Recorder({"Mary TTM 2025": None})
    got, how = r.resolve("Mary TTM 2025", StatementType.IS)
    assert got is None
    assert "Mary TTM 2025" in r.declined


def test_a_decline_is_cached_so_it_is_not_asked_again():
    r = Recorder({"Mary TTM 2025": None})
    r.resolve("Mary TTM 2025", StatementType.IS)
    r.resolve("Mary TTM 2025", StatementType.IS)
    assert r.calls == 1


# --------------------------------------------------------------------------
# caching and reproducibility -- the reason a model here is safe at all
# --------------------------------------------------------------------------


def test_the_same_caption_is_asked_once_however_often_it_appears():
    r = Recorder({"For the Year Ended June 30, 2024": Period(date(2024, 6, 30), 12)})
    for _ in range(5):
        r.resolve("For the Year Ended June 30, 2024", StatementType.IS)
    assert r.calls == 1


def test_caching_is_insensitive_to_whitespace_and_case():
    r = Recorder({"For the Year Ended June 30, 2024": Period(date(2024, 6, 30), 12)})
    r.resolve("For the Year Ended June 30, 2024", StatementType.IS)
    got, how = r.resolve("  FOR THE  year ended June 30, 2024 ", StatementType.IS)
    assert how == "cache"
    assert got.months == 12
    assert r.calls == 1


def test_a_replayed_cache_reproduces_the_answer_with_the_model_switched_off():
    """This is what `build` does: same figures as the run the analyst reviewed."""
    first = Recorder({"For the Year Ended June 30, 2024": Period(date(2024, 6, 30), 12)})
    first.resolve("For the Year Ended June 30, 2024", StatementType.IS)

    replay = PeriodResolver(use_llm=False, cache=dict(first.cache))
    got, how = replay.resolve("For the Year Ended June 30, 2024", StatementType.IS)

    assert how == "cache"
    assert (got.end, got.months) == (date(2024, 6, 30), 12)
    assert replay.calls == 0


def test_without_a_model_the_structural_answer_survives_unchanged():
    r = PeriodResolver(use_llm=False)
    got, how = r.resolve("For the Year Ended June 30, 2024", StatementType.IS)
    # Structure reads the date but not the length; nothing is invented.
    assert how == "rules"
    assert got.end == date(2024, 6, 30)
    assert got.months is None
    assert r.calls == 0


def test_a_model_failure_degrades_to_structure_instead_of_killing_ingest():
    class Broken(Recorder):
        def _ask(self, text):
            raise RuntimeError("no network")

    r = Broken()
    got, how = r.resolve("For the Year Ended June 30, 2024", StatementType.IS)
    assert got.end == date(2024, 6, 30)
    assert how == "rules"
    assert any("no network" in n for n in r.notes)


def test_one_failure_disables_further_calls_rather_than_retrying_per_caption():
    class Broken(Recorder):
        def _ask(self, text):
            self.calls += 1
            raise RuntimeError("no network")

    r = Broken()
    for text in ("For the Year Ended June 30, 2024", "TTM Ended March 31, 2025"):
        r.resolve(text, StatementType.IS)
    assert r.calls == 1


def test_warm_fills_the_cache_so_later_lookups_make_no_calls():
    r = Recorder({"For the Year Ended June 30, 2024": Period(date(2024, 6, 30), 12)})
    r.warm([("For the Year Ended June 30, 2024", StatementType.IS)])
    assert r.calls == 1
    _, how = r.resolve("For the Year Ended June 30, 2024", StatementType.IS)
    assert how == "cache"
    assert r.calls == 1
