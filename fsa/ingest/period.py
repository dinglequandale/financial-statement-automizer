"""Resolve a period caption: structure first, a model only for the residue.

`raw.parse_period` reads the shapes a date can be written in unambiguously --
`As of December 31, 2024`, `January through August 2025`, `March 2025`. It
deliberately stops there. The rest of how statements say when they cover is
*phrasing*, not structure: `For the Year Ended June 30, 2024`, `Eight Months
Ended August 31, 2025`, `Trailing Twelve Months Ended March 31, 2025`, `LTM
3/31/25`, `Q3 2024`. Every client writes those differently, and adding a regex
family per client is how a parser ends up fitted to whoever happened to send
files first.

That is not a hypothesis. `tools/period_bakeoff.py` scores three arms over 53
captions, ~2/3 of which appear in no sample client:

    rules as shipped   16 wrong dates, 13 missing lengths, 22/53 correct
    rules + guard       2 wrong dates, 13 missing lengths, 29/53 correct
    claude-haiku-4-5    0 wrong dates,  1 missing length,  47/53 correct

Three model runs produced 1, 1 and 0 silent failures and **zero** wrong dates;
the variance sat entirely in how often it declined, which is the safe
direction. So the split here is: structure is deterministic and permanent, and
the open-ended vocabulary goes to a model that is measurably better at it.

Three properties keep that safe:

1. **The model is never asked first.** It sees only what the deterministic
   layer could not resolve, so a client whose captions are ordinary costs
   nothing at all.
2. **Answers are cached by caption text.** `prepare` and `build` re-ingest the
   same files and must produce identical numbers (`fsa/job.py`); a cached
   decision makes the model layer reproducible rather than merely repeatable.
3. **It may decline.** An unresolved period is a loud `period_unreadable`
   error the analyst answers in seconds -- never a guess that files a column of
   figures under a year the client never reported.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

from fsa.ingest.raw import Period, parse_period
from fsa.model.schema import StatementType

#: Classification of a short caption. Not a reasoning task, and priced like it
#: ($1/$5 per MTok -- a full client costs a fraction of a cent).
MODEL = "claude-haiku-4-5"

MAX_WORKERS = 8

SPEC = """\
You read one caption line from a financial statement and report the reporting
period it identifies.

A reporting period is a *historical actual* period covered by the statement.

- A balance sheet caption names a point in time: report its date, months = null.
- An income statement caption names a span: report the END date and the number
  of months it covers (a full year is 12, a trailing-twelve-month period is
  also 12, a quarter is 3).

Decline (readable = false) when the caption does not determine an actual
reporting period. That includes:
- a bare year or fiscal-year label with no month or day, because the client's
  year end is not knowable from the caption alone
- budgets, forecasts, or projections, which are not actual periods
- print timestamps, page furniture, account names, statement titles, and any
  other text that is not a period at all
- a caption naming more than one period

Declining is safe and expected. Guessing a date the caption does not support is
the one outcome to avoid: it files a column of figures under the wrong period.

When a month is named but no day, use the last day of that month."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "readable": {"type": "boolean"},
        "end_date": {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]},
        "months": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    },
    "required": ["readable", "end_date", "months"],
    "additionalProperties": False,
}


def _key(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


@dataclass
class PeriodResolver:
    """Deterministic parse, then a model, then a cache over both.

    `cache` maps a normalized caption to a serialized answer and is round
    tripped through the job directory, so a second run of the same engagement
    makes no calls at all.
    """

    use_llm: bool = True
    model: str = MODEL
    cache: dict[str, dict] = field(default_factory=dict)
    #: Captions the model was asked about and declined, so the caller can
    #: report them once rather than per call site.
    declined: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    calls: int = 0
    _client: object | None = None
    _disabled: bool = False

    # ---------------------------------------------------------------- rules

    def _needs_model(self, text: str, got: Period | None, statement) -> bool:
        """Escalate only where the deterministic layer is knowably short.

        Two cases, both structural rather than vocabulary-based:

        * it resolved nothing at all, and there is something to resolve;
        * it resolved a date but no length, on a statement that must have one.
          An income statement always covers a span, so `months is None` there
          is a gap by definition -- this is what catches `Year Ended ...` and
          every stub phrased as `N Months Ended ...` without naming any of them.
        """
        if not (text or "").strip():
            return False
        if got is None:
            return any(ch.isdigit() for ch in text)
        return statement is StatementType.IS and got.months is None

    # ---------------------------------------------------------------- model

    def _ask(self, text: str) -> Period | None:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic()
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=256,
            system=SPEC,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": f"Caption: {text!r}"}],
        )
        self.calls += 1
        body = next(b.text for b in resp.content if b.type == "text")
        d = json.loads(body)
        if not d.get("readable") or not d.get("end_date"):
            return None
        y, m, dd = (int(x) for x in str(d["end_date"]).split("-"))
        return Period(end=date(y, m, dd), months=d.get("months"), raw=text)

    def _available(self) -> bool:
        if self._disabled or not self.use_llm:
            return False
        if not os.environ.get("ANTHROPIC_API_KEY"):
            self._disabled = True
            self.notes.append(
                "no ANTHROPIC_API_KEY: period captions the structural parser "
                "cannot read were left unresolved rather than guessed"
            )
            return False
        return True

    # ---------------------------------------------------------------- api

    def resolve(
        self, text: str, statement: StatementType | None = None
    ) -> tuple[Period | None, str]:
        """Return `(period, how)` where `how` is rules | cache | llm | none."""
        got = parse_period(text)
        if not self._needs_model(text, got, statement):
            return got, "rules" if got else "none"

        k = _key(text)
        if k in self.cache:
            return self._from_cache(k, text), "cache"

        if not self._available():
            return got, "rules" if got else "none"

        try:
            answer = self._ask(text)
        except Exception as exc:  # noqa: BLE001 -- ingest must never die here
            self._disabled = True
            self.notes.append(f"period model unavailable ({exc}); using structure only")
            return got, "rules" if got else "none"

        self.cache[k] = (
            {"end": answer.end.isoformat(), "months": answer.months}
            if answer is not None
            else {"end": None, "months": None}
        )
        if answer is None:
            self.declined.append(text)
        return answer, "llm"

    def _from_cache(self, k: str, text: str) -> Period | None:
        d = self.cache[k]
        if not d.get("end"):
            return None
        y, m, dd = (int(x) for x in str(d["end"]).split("-"))
        return Period(end=date(y, m, dd), months=d.get("months"), raw=text)

    def warm(self, texts: list[tuple[str, StatementType | None]]) -> None:
        """Resolve many captions concurrently, filling the cache.

        Call before a sequential ingest so the model round trips overlap
        instead of adding a second each to the wall clock.
        """
        pending = [
            (t, st)
            for t, st in texts
            if t and _key(t) not in self.cache
            and self._needs_model(t, parse_period(t), st)
        ]
        if not pending or not self._available():
            return
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            list(ex.map(lambda p: self.resolve(*p), pending))
