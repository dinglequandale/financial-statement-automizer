"""PDF reader: recover the account hierarchy the team's manual conversion loses.

Sample 2 arrived as QuickBooks print-to-PDF, which the Weaver team converts to
Excel by hand before working on it. Reading the PDF directly is strictly better
on two counts (PLAN 2.6):

* the conversion is a manual step we delete outright, and
* the conversion is *lossy in the field that matters*. QuickBooks encodes
  account nesting as indentation. The converted `.ods` carries none of it --
  not as a column offset, not as leading whitespace -- so the group header
  `Accounts Receivable` and the detail line `Accounts Receivable` collapse into
  byte-identical strings that nothing downstream can separate. In the PDF they
  sit at different x-offsets and are never confused.

The indent ladder is measured, not assumed: body labels land on a regular step
(~6.85pt for this producer), so depth is `round((x - base) / step)` and comes
out integral with a wide margin. We derive `step` per document rather than
hard-coding it, because it is a function of the producing application's font
and point size, not of PDF in general.

Two traps worth naming:

1. **Word order in the content stream is not reading order.** A line's number
   frequently precedes its label. Everything is re-sorted by x before use.
2. **A scanned PDF yields no text at all.** That must fail loudly -- a silent
   empty statement is the one error that reaches a delivered model unnoticed.
"""

from __future__ import annotations

import re
import statistics
from pathlib import Path

from fsa.ingest.raw import RawDoc, RawPart, RawRow, UnreadableSource

#: A number as QuickBooks prints it: "1,234.56", "-61,700.00", "(1,234.56)", "0.00".
_NUMBER = re.compile(r"^\(?-?[\d,]*\d(?:\.\d+)?\)?$")

#: Rows whose label starts left of the body are page furniture (timestamp,
#: print date). Measured per page rather than hard-coded.
_FURNITURE_GAP = 20.0

#: Two words belong to the same visual line if their tops differ by less.
_LINE_TOL = 3.0

#: Below this many extractable words per page we assume an image-only scan.
_MIN_WORDS_PER_PAGE = 15


def _to_number(tok: str) -> float | None:
    t = tok.strip()
    if not _NUMBER.match(t):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace(",", "")
    if t in ("", "-", "."):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _lines(page) -> list[tuple[float, list]]:
    """Group words into visual lines, each sorted left to right."""
    words = sorted(page.get_text("words"), key=lambda w: (round(w[1], 1), w[0]))
    out: list[tuple[float, list]] = []
    cur: list = []
    top: float | None = None
    for w in words:
        if top is None or abs(w[1] - top) > _LINE_TOL:
            if cur:
                out.append((top, sorted(cur, key=lambda w: w[0])))
            cur, top = [], w[1]
        cur.append(w)
    if cur:
        out.append((top, sorted(cur, key=lambda w: w[0])))
    return out


#: Words inside a label sit a few points apart; the gutter before a
#: right-aligned value column is far wider. Anything between is not a real
#: layout in this class of report.
_GUTTER = 10.0


def _split(words: list) -> tuple[list, list]:
    """Separate label words from value words on one line, by position.

    Splitting on "does this token look like a number?" is wrong, and wrongly in
    a way that corrupts figures silently. Real charts of accounts put digits in
    account *names* -- this client has `Visa AE - 6777`, a card's last four --
    and a naive regex reads 6777 as the row's balance. Measured: that made
    FY2022 `Total Credit Cards` disagree with its own child by $3,761.54.

    Values live in a right-aligned column, so the label/value boundary is the
    line's widest horizontal gap. A split only counts when everything to its
    right is numeric; otherwise the line is all label (which is what keeps
    `Accrual Basis  As of December 31, 2025` from donating 2025 as a value).
    """
    if not words:
        return [], []
    ordered = sorted(words, key=lambda w: w[0])
    gaps = [(ordered[i + 1][0] - ordered[i][2], i + 1) for i in range(len(ordered) - 1)]
    if gaps:
        width, at = max(gaps)
        if width >= _GUTTER and all(_to_number(w[4]) is not None for w in ordered[at:]):
            return ordered[:at], ordered[at:]
    if all(_to_number(w[4]) is not None for w in ordered):
        return [], ordered
    return ordered, []


def _indent_step(offsets: list[float]) -> float | None:
    """Infer the per-level indent from the gaps between distinct x-offsets.

    Uses the smallest recurring gap: nesting is dense, so the one-level step is
    the gap that shows up most often among small gaps. Returns None when the
    document has no usable ladder (e.g. a single flat level).
    """
    uniq = sorted(set(round(x, 1) for x in offsets))
    gaps = [round(b - a, 1) for a, b in zip(uniq, uniq[1:]) if 2.0 < (b - a) < 40.0]
    if not gaps:
        return None
    try:
        return statistics.mode(gaps)
    except statistics.StatisticsError:
        return min(gaps)


def read(path: Path) -> RawDoc:
    """Read a text PDF into the universal intermediate. One `RawPart` per page."""
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - environment issue
        raise UnreadableSource(
            "PyMuPDF is required to read PDF statements (pip install pymupdf)"
        ) from exc

    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise UnreadableSource(f"cannot open {path.name}: {exc}") from exc

    pages = [(_lines(p), p) for p in doc]
    total_words = sum(len(ws) for lines, _ in pages for _, ws in lines)
    if total_words < _MIN_WORDS_PER_PAGE * max(1, len(pages)):
        raise UnreadableSource(
            f"{path.name}: only {total_words} extractable words across "
            f"{len(pages)} page(s) -- this looks like a scanned/image PDF. "
            f"Text-based statements are required; OCR is not supported."
        )

    # The indent ladder must be measured **per page**, not pooled across the
    # document. Different report types start at different left margins (this
    # producer indents the balance sheet from x=191.0 and the P&L from x=184.6),
    # so pooling their offsets invents gaps that were never a nesting level and
    # halves the inferred step -- which then throws most rows off the ladder and
    # silently discards them. Measured: pooling gave depths 0,2,4 and dropped 43
    # of 51 P&L rows.
    per_page: list[list[tuple[float, float, str, list[float], bool, str]]] = []
    page_steps: list[float] = []
    page_bases: list[float] = []

    for lines, _page in pages:
        parsed = []
        for top, words in lines:
            labels, values = _split(words)
            if not labels:
                continue
            x0 = min(w[0] for w in labels)
            text = " ".join(w[4] for w in labels).strip()
            nums = [_to_number(w[4]) for w in values]
            trunc = text.endswith("...") or text.endswith("…")
            # Caption text keeps its numbers inline: "As of December 31, 2025"
            # only parses as a period if the year survives, and the year is a
            # numeric word that `_split` would otherwise strip out.
            full = " ".join(w[4] for w in words).strip()
            parsed.append((top, x0, text, nums, trunc, full))
        per_page.append(parsed)
        if parsed:
            # Furniture sits far left of the body; the body is the dense cluster.
            xs = [x for _, x, _, _, _, _ in parsed]
            median = statistics.median(xs)
            body = [x for x in xs if x > median - _FURNITURE_GAP * 3]
            page_bases.append(min(body) if body else min(xs))
            s = _indent_step(body)
            if s:
                page_steps.append(s)
        else:
            page_bases.append(0.0)

    # A page with too few distinct levels to measure borrows the document's
    # typical step -- same producer, same font, same ladder.
    fallback = statistics.median(page_steps) if page_steps else None

    out = RawDoc(source=path, kind="pdf")
    for pageno, parsed in enumerate(per_page, start=1):
        part = RawPart(name=f"p{pageno}")
        body_min = page_bases[pageno - 1]
        if parsed:
            xs = [x for _, x, _, _, _, _ in parsed]
            median = statistics.median(xs)
            body = [x for x in xs if x > median - _FURNITURE_GAP * 3]
            step = _indent_step(body) or fallback
        else:
            step = fallback

        depths: list[int | None] = []
        for _top, x0, _text, _nums, _trunc, _full in parsed:
            furniture = x0 < body_min - _FURNITURE_GAP
            d = None
            if step and not furniture:
                q = (x0 - body_min) / step
                if abs(q - round(q)) < 0.25 and -0.5 < q < 12:
                    d = int(round(q))
            depths.append(d)

        # Masthead lines (entity name, report title) are centred, so they can
        # land on the indent ladder by coincidence -- the company name scored a
        # clean depth 6 on the balance sheet. What separates them is that they
        # are *alone* at their offset: a real nesting level is shared by
        # several rows. So a row is body if its level recurs, or if it carries
        # a number. The latter clause matters because the P&L's `Net Income` is
        # the only row at its outdented level, and it must not be dropped.
        #
        # Keying off depth 0 instead would fail outright: the P&L's outermost
        # account row is depth 1, and depth 0 appears only on the last line.
        level_counts: dict[int, int] = {}
        for d in depths:
            if d is not None:
                level_counts[d] = level_counts.get(d, 0) + 1

        for i, (top, _x0, text, nums, trunc, full) in enumerate(parsed):
            depth = depths[i]
            if depth is not None and level_counts[depth] < 2 and not any(
                v is not None for v in nums
            ):
                depth = None
            if depth is None:
                if full:
                    part.captions.append(full)
                continue
            part.rows.append(
                RawRow(
                    index=i + 1,
                    label=text or None,
                    values=nums,
                    depth=depth,
                    locator=f"y={top:.1f}",
                    truncated=trunc,
                )
            )
        out.parts.append(part)

    doc.close()
    return out
