"""Rules vs LLM on period-caption parsing, scored on silent failures.

Three arms over `eval/period_captions.yaml`:

    A  rules      -- fsa.ingest.raw.parse_period, exactly as it ships
    B  guarded    -- the same rules, plus a refusal to infer a year from a bare
                     number when the caption carries any other temporal signal
                     (a month name, TTM/YTD/quarter wording) or is not an actual
                     reporting period at all (budget, forecast, print timestamp)
    C  llm        -- claude-haiku-4-5, structured output, one call per caption

Both A/B and C are given the same task specification (SPEC below); neither is
shown the expected answers.

The headline metric is **not** accuracy. A caption the parser declines is a
question to the analyst -- cheap and safe. A caption it answers *wrongly* is a
whole column of numbers filed under the wrong period, which is the failure that
reaches a delivered model. So the arms are ranked on `silent_wrong` first and
`correct` second.

Usage:
    python tools/period_bakeoff.py            # arms A and B only, free
    python tools/period_bakeoff.py --llm      # all three arms
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from fsa.env import load_dotenv
from fsa.ingest.raw import _MONTH_ALT, Period, parse_period

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / "eval" / "period_captions.yaml"

MODEL = "claude-haiku-4-5"

#: The task, stated once and given to every arm. Describes the contract, never
#: the corpus -- the arms are not told which captions are undetermined.
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

# --------------------------------------------------------------------------
# Arm B: the guard
# --------------------------------------------------------------------------

_MONTH_RE = re.compile(rf"\b({_MONTH_ALT})\b", re.I)

#: Wording that implies a period the bare-year fallback cannot represent.
_TEMPORAL_SIGNAL = re.compile(
    r"\b(ttm|ltm|ytd|trailing|last\s+twelve|year\s+to\s+date|"
    r"q[1-4]|quarter|month|months|period|stub|interim|"
    r"through|thru|ended|ending)\b",
    re.I,
)

#: Not an actual historical period, whatever date it contains.
_NOT_ACTUAL = re.compile(
    r"\b(budget|forecast|project(ed|ion|ions)?|plan|proforma|pro\s*forma|"
    r"restated|printed|as\s+of\s+\d{1,2}[:/]\d{2}\s*(am|pm)|comparative)\b",
    re.I,
)

_BARE_YEAR_ONLY = re.compile(r"^\s*(?:fy\s*)?((?:19|20)\d{2})\s*$", re.I)


def parse_period_guarded(text: str) -> Period | None:
    """`parse_period`, minus the guesses it is not entitled to make.

    Two refusals, both about the bare-year fallback in `raw.py`. That fallback
    turns any four-digit number into December 31st of that year, which is right
    for `FY2023` on a calendar-year client and catastrophic for `March 2025`
    (a March balance sheet filed as a December year end) or `TTM 2025`.
    """
    if not text:
        return None
    if _NOT_ACTUAL.search(text):
        return None

    got = parse_period(text)
    if got is None:
        return None

    # Did the underlying parser reach the bare-year fallback? It is the only
    # branch that yields Dec-31 with no day and no range in the source text.
    reached_fallback = (
        got.months is None and got.end.month == 12 and got.end.day == 31
        and not re.search(r"\b31\b", text)
    )
    if not reached_fallback:
        return got

    # It did. Refuse when the caption plainly describes something else.
    if _MONTH_RE.search(text) or _TEMPORAL_SIGNAL.search(text):
        return None
    # A bare year on its own does not determine a year end either.
    if _BARE_YEAR_ONLY.match(text.strip()):
        return None
    return got


# --------------------------------------------------------------------------
# Arm C: the model
# --------------------------------------------------------------------------

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


def parse_period_llm(client, text: str) -> tuple[Period | None, int, int]:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=256,
        system=SPEC,
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": f"Caption: {text!r}"}],
    )
    body = next(b.text for b in resp.content if b.type == "text")
    d = json.loads(body)
    usage = (resp.usage.input_tokens, resp.usage.output_tokens)
    if not d.get("readable") or not d.get("end_date"):
        return None, *usage
    y, m, dd = (int(x) for x in d["end_date"].split("-"))
    return Period(end=date(y, m, dd), months=d.get("months"), raw=text), *usage


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


@dataclass
class Score:
    correct: int = 0
    abstained: int = 0
    wrong_date: int = 0
    wrong_length: int = 0
    detail: list = field(default_factory=list)

    @property
    def n(self) -> int:
        return self.correct + self.abstained + self.wrong_date + self.wrong_length

    @property
    def silent_wrong(self) -> int:
        return self.wrong_date + self.wrong_length


def judge(item: dict, got: Period | None) -> str:
    """Four outcomes, deliberately not collapsed into pass/fail.

    The two failures differ in blast radius. A wrong *end date* files a whole
    column of figures under a period the client never reported. A wrong period
    *length* keeps the column in the right year but lets an 8-month stub or a
    TTM span masquerade as a full year -- the §2.6 error, quieter but still a
    misstated valuation.
    """
    want_end = item.get("end")
    must_abstain = bool(item.get("must_abstain"))

    if got is None:
        # Declining is correct when there was nothing to find; otherwise it is
        # a safe miss, never a silent failure.
        return "correct" if must_abstain else "abstained"
    if must_abstain or want_end is None:
        return "wrong_date"
    if got.end != want_end:
        return "wrong_date"
    want_months = item.get("months")
    if want_months is not None and got.months != want_months:
        return "wrong_length"
    return "correct"


def run(name: str, fn, items: list[dict]) -> Score:
    s = Score()
    for it in items:
        try:
            got = fn(it["text"])
        except Exception as exc:  # a crash is not a silent failure, but note it
            got = None
            it = {**it, "_error": str(exc)}
        verdict = judge(it, got)
        setattr(s, verdict, getattr(s, verdict) + 1)
        s.detail.append((it, got, verdict))
    return s


def report(name: str, s: Score, items: list[dict]) -> None:
    print(f"\n{'=' * 78}\n  ARM {name}   n={s.n}\n{'=' * 78}")
    print(f"    correct        {s.correct:>3}   ({100*s.correct/s.n:.0f}%)")
    print(f"    abstained      {s.abstained:>3}   ({100*s.abstained/s.n:.0f}%)  safe -- costs a question")
    print(f"    wrong DATE     {s.wrong_date:>3}   ({100*s.wrong_date/s.n:.0f}%)  <- column filed under the wrong period")
    print(f"    wrong LENGTH   {s.wrong_length:>3}   ({100*s.wrong_length/s.n:.0f}%)  <- stub/TTM passes as a full year")

    for label, want in (("in-sample", True), ("out-of-sample", False)):
        sub = [d for d in s.detail if bool(d[0].get("seen")) is want]
        if not sub:
            continue
        c = sum(1 for d in sub if d[2] == "correct")
        w = sum(1 for d in sub if d[2] in ("wrong_date", "wrong_length"))
        print(f"      {label:<14} {c}/{len(sub)} correct, {w} silent wrong")

    for kind, title in (("wrong_date", "wrong date"), ("wrong_length", "wrong length")):
        bad = [d for d in s.detail if d[2] == kind]
        if not bad:
            continue
        print(f"\n    {title}:")
        for it, got, _ in bad:
            exp = it.get("end") or "(decline)"
            gm = "-" if got is None or got.months is None else got.months
            wm = it.get("months")
            wm = "-" if wm is None else wm
            print(f"      {it['text'][:42]:44} -> {str(got.end) if got else None:11} {str(gm):>3}m"
                  f"   want {str(exp):11} {str(wm):>3}m")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--llm", action="store_true", help="include the model arm (costs a few cents)")
    args = ap.parse_args()

    items = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))["captions"]
    print(f"corpus: {len(items)} captions "
          f"({sum(1 for i in items if i.get('seen'))} in-sample, "
          f"{sum(1 for i in items if not i.get('seen'))} out-of-sample, "
          f"{sum(1 for i in items if i.get('must_abstain'))} must-abstain)")

    scores = {}
    scores["A rules"] = run("A", parse_period, items)
    scores["B guarded"] = run("B", parse_period_guarded, items)

    if args.llm:
        load_dotenv()
        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("\n  (no ANTHROPIC_API_KEY -- skipping arm C)")
        else:
            client = anthropic.Anthropic()
            tok = [0, 0]

            def one(text: str):
                p, i, o = parse_period_llm(client, text)
                tok[0] += i
                tok[1] += o
                return p

            with ThreadPoolExecutor(max_workers=8) as ex:
                got = list(ex.map(lambda it: one(it["text"]), items))
            s = Score()
            for it, g in zip(items, got):
                v = judge(it, g)
                setattr(s, v, getattr(s, v) + 1)
                s.detail.append((it, g, v))
            scores["C llm"] = s
            cost = tok[0] / 1e6 * 1.00 + tok[1] / 1e6 * 5.00
            print(f"\n  arm C: {tok[0]} in / {tok[1]} out tokens  =  ${cost:.4f}")

    for name, s in scores.items():
        report(name, s, items)

    print(f"\n{'=' * 78}\n  RANKING (silent failures first, then correctness)\n{'=' * 78}")
    print(f"  {'arm':<12}{'silent':>8}{'  (date':>8}{'/len)':>7}{'correct':>10}{'abstained':>12}")
    for name, s in sorted(scores.items(), key=lambda kv: (kv[1].silent_wrong, -kv[1].correct)):
        print(f"  {name:<12}{s.silent_wrong:>8}{s.wrong_date:>8}{s.wrong_length:>7}"
              f"{s.correct:>10}{s.abstained:>12}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
