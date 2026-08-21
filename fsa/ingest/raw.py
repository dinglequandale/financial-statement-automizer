"""The universal intermediate: what every reader produces, whatever the format.

`RawDoc` is deliberately dumb. It knows about rows, horizontal position and
numbers; it knows nothing about accounting. All accounting judgment lives one
layer up in `interpret.py`, written once and shared by every format.

The one field that earns its keep is `RawRow.depth`. Sample 2's QuickBooks PDFs
encode account nesting as a dead-regular indent ladder (6.85pt per level), which
gives us section membership, header-vs-detail and subtotal scope directly --
signal that Excel sources make us infer from formatting. Readers that can
recover depth set it; readers that cannot leave it `None` and `interpret` falls
back to heuristics. Nothing downstream is allowed to *require* it.

Period parsing lives here too, because it is format-independent and because
getting it wrong is expensive: sample 2 ships FY2025 twice, once as a full year
and once as an 8-month stub (PLAN 2.6). Mixing those in a year-keyed table is a
valuation error much larger than any mapping mistake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_MONTHS = {
    m: i
    for i, m in enumerate(
        "january february march april may june july august september "
        "october november december".split(),
        start=1,
    )
}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

#: "As of December 31, 2025" / "December 31, 2025" / "Dec 31, 25"
_AS_OF = re.compile(
    rf"(?:as\s+of\s+)?({_MONTH_ALT})\.?\s+(\d{{1,2}}),?\s+(\d{{2,4}})", re.I
)
#: "January through December 2025" / "Jan - Dec 25" / "Jan through Aug 2025"
_RANGE = re.compile(
    rf"({_MONTH_ALT})\.?\s*(?:through|thru|-|–|to)\s*({_MONTH_ALT})\.?\s+(\d{{2,4}})",
    re.I,
)
#: "March 2025", "Dec 25" -- a month with no day. The month is real signal and
#: resolves to its own last day; what we must never do is let the *year* alone
#: decide, because that silently turns a March balance sheet into a December
#: year end (measured: `tools/period_bakeoff.py`).
_MONTH_YEAR = re.compile(rf"\b({_MONTH_ALT})\.?,?\s+((?:19|20)?\d{{2}})\b", re.I)

#: Text that carries a date but is not a historical reporting period. Reading a
#: budget or a print timestamp as a fiscal year files projected or meaningless
#: figures alongside actuals.
_NOT_ACTUAL = re.compile(
    r"\b(budget|budgeted|forecast|forecasted|projection|projections|projected|"
    r"plan|planned|proforma|pro\s*forma|restated|printed|comparative)\b",
    re.I,
)

#: Anything that reads as a specific date: "March 31, 2025", "3.31.25",
#: "4/1/2024". Used only to count them -- a caption carrying two dates is a
#: span, and picking whichever one a single-date pattern happens to match
#: first turns "April 1, 2024 - March 31, 2025" into a period ending in April.
_DATE_TOKEN = re.compile(
    rf"(?:({_MONTH_ALT})\.?,?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{2,4}})"
    rf"|(?:\b\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{2,4}}\b)",
    re.I,
)

_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _full_year(y: int) -> int:
    """Expand a 2-digit year. Statements are historical, never far future."""
    if y >= 100:
        return y
    return 2000 + y if y < 90 else 1900 + y


def _eom(year: int, month: int) -> date:
    d = _DAYS[month - 1]
    if month == 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        d = 29
    return date(year, month, d)


@dataclass(frozen=True)
class Period:
    """When a column of numbers refers to.

    `months` is the length of an income-statement period; a balance sheet is a
    point in time and leaves it `None`. `end` is what identifies the column --
    two sources agreeing on `end` and `months` are the same period.
    """

    end: date
    months: int | None = None
    raw: str = ""

    @property
    def fiscal_year(self) -> int:
        return self.end.year

    @property
    def is_partial(self) -> bool:
        """A period covering less than a full year. Never mix with full years."""
        return self.months is not None and self.months < 12

    @property
    def key(self) -> tuple:
        return (self.end, self.months)

    def __str__(self) -> str:
        if self.months is None:
            return f"as of {self.end:%b %d, %Y}"
        return f"{self.months}m ended {self.end:%b %d, %Y}"


def parse_period(text: str) -> Period | None:
    """Read a period caption structurally. Returns None rather than guessing.

    Handles the shapes a date can be written in unambiguously:
      "As of December 31, 2025"       -> point in time
      "Dec 31, 25"                    -> point in time (column header)
      "January through December 2025" -> 12-month period
      "Jan - Aug 25"                  -> 8-month period  <- the stub that matters
      "March 2025"                    -> point in time, end of that month

    Deliberately *not* handled here: "Year Ended ...", "Eight Months Ended ...",
    "TTM", "LTM", "Q3", "YTD". Those are phrasings, not structure, and every
    client writes them differently -- enumerating them is the treadmill this
    module refuses to walk. `fsa.ingest.period` sends what lands here as None
    to a model instead, which is measurably better at them
    (`tools/period_bakeoff.py`: 0 wrong dates in 53 captions vs 16 for regex).

    Two things it will no longer do, both of which were silent failures:

    * **Infer a year end from a bare year.** `FY2023` does not say when the
      client's year ends, and `2024 Financials` says even less. The old
      last-resort branch answered December 31st regardless, which is right for
      a calendar-year client and wrong for every other one.
    * **Read a date out of something that is not an actual period.** A budget,
      a forecast or a print timestamp carries a perfectly good date and must
      still be refused.
    """
    if not text:
        return None
    t = " ".join(str(text).split())

    if _NOT_ACTUAL.search(t):
        return None

    m = _RANGE.search(t)
    if m:
        a, b, y = _MONTHS[m.group(1).lower().rstrip(".")], _MONTHS[
            m.group(2).lower().rstrip(".")
        ], _full_year(int(m.group(3)))
        months = b - a + 1
        if months <= 0:  # period straddling a year end
            months += 12
        return Period(end=_eom(y, b), months=months, raw=t)

    # Two dates and no range pattern matched: this is a span written in a shape
    # we do not read structurally ("April 1, 2024 - March 31, 2025",
    # "For the period 4.1.24 - 3.31.25"). Taking the first date would report a
    # period ending on the day the period *started*, so decline and let the
    # resolver escalate instead.
    if len(_DATE_TOKEN.findall(t)) > 1:
        return None

    m = _AS_OF.search(t)
    if m:
        mo = _MONTHS[m.group(1).lower().rstrip(".")]
        day, y = int(m.group(2)), _full_year(int(m.group(3)))
        try:
            return Period(end=date(y, mo, day), months=None, raw=t)
        except ValueError:
            return Period(end=_eom(y, mo), months=None, raw=t)

    m = _MONTH_YEAR.search(t)
    if m:
        mo = _MONTHS[m.group(1).lower().rstrip(".")]
        return Period(end=_eom(_full_year(int(m.group(2))), mo), months=None, raw=t)

    return None


@dataclass(frozen=True)
class RawRow:
    """One visual line of a source document."""

    index: int  # 1-based within the part
    label: str | None
    values: list[float | None] = field(default_factory=list)
    depth: int | None = None  # nesting level, 0-based; None == unknown
    locator: str = ""  # provenance within the part: "C18", "y=249.7"
    truncated: bool = False  # label ended in "..." at source (PLAN 2.6 #4)

    @property
    def is_blank(self) -> bool:
        return not (self.label or "").strip() and not any(
            v is not None for v in self.values
        )


@dataclass
class RawPart:
    """One page (PDF) or one worksheet (spreadsheet).

    A part may contain more than one statement stacked vertically -- sample 2
    puts the balance sheet and the P&L on a single sheet.
    """

    name: str
    rows: list[RawRow] = field(default_factory=list)
    #: Caption text found above the body, in reading order. `interpret` mines
    #: this for entity name, statement type and period.
    captions: list[str] = field(default_factory=list)

    def content_hash(self) -> str:
        """Identity ignoring print timestamps -- for de-duplicating pages."""
        import hashlib

        h = hashlib.sha256()
        for r in self.rows:
            h.update(f"{r.label}|{r.depth}|{r.values}\n".encode())
        return h.hexdigest()[:16]


@dataclass
class RawDoc:
    source: Path
    kind: str  # "pdf" | "ods" | "xlsx"
    parts: list[RawPart] = field(default_factory=list)

    @property
    def has_depth(self) -> bool:
        return any(r.depth is not None for p in self.parts for r in p.rows)


class UnreadableSource(RuntimeError):
    """The file cannot be read as a statement -- e.g. a scanned, image-only PDF.

    Raised loudly and never swallowed: silently returning an empty statement is
    the one failure mode that reaches a delivered model unnoticed.
    """
