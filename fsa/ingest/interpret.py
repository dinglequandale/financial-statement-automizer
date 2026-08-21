"""RawDoc -> StatementSet. All the accounting judgment, none of the file formats.

This is the layer sample 2 forced into existence (PLAN 2.7). Readers hand us a
positional grid; everything that requires knowing what a financial statement
*is* happens here, once, for every format.

Scope note -- why this does not replace `extract.py`:

    `extract.py` handles **multi-column** sources: one sheet carrying several
    fiscal years side by side, where a year is a column and the hard problem is
    aligning rows across columns. That is TS Distributors.

    `interpret.py` handles **single-period documents**: one page or segment
    carrying one period, where the hard problems are finding the statement
    boundaries, reading the period caption, and recovering hierarchy. That is
    QuickBooks output, and it is a genuinely different layout, not merely a
    different file extension.

Three things here are not obvious:

1. **One part can hold two statements.** Sample 2's converted sheet stacks a
   balance sheet and a P&L vertically, so segmentation runs on row content, not
   on sheet boundaries.

2. **Depth replaces heuristics when present.** With an indent ladder we know a
   row's section exactly -- it is the nearest enclosing header at a lower depth
   -- rather than inferring it from blank lines and bold text.

3. **Duplicate labels are normal and must survive.** QuickBooks names a group
   and its only child identically (`Accounts Receivable` at depth 2 and 3).
   Depth separates them: the parent becomes a section header, the child stays
   data. Where two *data* rows still collide, we qualify the later one with its
   section rather than let one silently overwrite the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from fsa.ingest.normalize import is_derived_line, is_total_label, normalize
from fsa.ingest.raw import Period, RawDoc, RawPart, RawRow, parse_period
from fsa.model.schema import (
    AccountRow,
    CellRef,
    ColumnRole,
    ExtractedColumn,
    Finding,
    RowKind,
    Severity,
    StatementType,
)

_TITLES: list[tuple[re.Pattern, StatementType]] = [
    (re.compile(r"\bbalance\s+sheet\b", re.I), StatementType.BS),
    (re.compile(r"\bstatement\s+of\s+financial\s+position\b", re.I), StatementType.BS),
    (re.compile(r"\bprofit\s*(?:&|and)\s*loss\b", re.I), StatementType.IS),
    (re.compile(r"\bincome\s+statement\b", re.I), StatementType.IS),
    (re.compile(r"\bstatement\s+of\s+(?:operations|income|earnings)\b", re.I), StatementType.IS),
]

#: Statements this tool deliberately does not model. Recognising them is not
#: the same as supporting them: a cash flow statement opens with `Net Income`,
#: which is also the fallback marker for an income statement, so without this a
#: client's FY2022 cash flows are read *as* their income statement -- and on a
#: client who sends one file per statement it can win the year outright.
#:
#: There are three primary financial statements and this list closes the set;
#: it is domain vocabulary, not any one client's wording.
_NOT_MODELLED = re.compile(
    r"\b(statements?\s+of\s+cash\s*flows?|cash\s*flows?\s+statements?|"
    r"statements?\s+of\s+(changes\s+in\s+)?(stockholders?|shareholders?|"
    r"members?|owners?|partners?)[’']?\s*(equity|capital)|"
    r"statements?\s+of\s+retained\s+earnings|statements?\s+of\s+equity)\b",
    re.I,
)

#: Fallback when a document carries no title line: the outermost row of each
#: statement is highly stereotyped.
_BODY_MARKERS: list[tuple[re.Pattern, StatementType]] = [
    (re.compile(r"^\s*(?:total\s+)?assets\s*$", re.I), StatementType.BS),
    (re.compile(r"^\s*liabilities\s*(?:&|and)\s*(?:equity|capital)\s*$", re.I), StatementType.BS),
    (re.compile(r"^\s*ordinary\s+income", re.I), StatementType.IS),
    (re.compile(r"^\s*(?:gross\s+profit|net\s+income|total\s+revenue)\s*$", re.I), StatementType.IS),
]


def _title_of(text: str) -> StatementType | None:
    for pat, st in _TITLES:
        if pat.search(text or ""):
            return st
    return None


def _is_not_modelled(text: str) -> bool:
    return bool(_NOT_MODELLED.search(text or ""))


def _marker_of(text: str) -> StatementType | None:
    for pat, st in _BODY_MARKERS:
        if pat.match(text or ""):
            return st
    return None


def _rows_hash(rows: list[RawRow]) -> str:
    """Content identity for a statement body, ignoring print furniture."""
    import hashlib

    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r.label}|{r.depth}|{r.values}\n".encode())
    return h.hexdigest()[:16]


#: Names a tool gives a sheet when nobody named it: Excel's `Sheet1`, its
#: localised forms, and this reader's own `p1`/`p2` page labels.
_DEFAULT_NAME = re.compile(
    r"^\s*(sheet|tabelle|hoja|feuille|foglio|blad|list|p)\s*\d*\s*$", re.I
)


def _is_default_name(name: str | None) -> bool:
    return not name or bool(_DEFAULT_NAME.match(name))


@dataclass
class _Segment:
    statement: StatementType
    rows: list[RawRow]
    period: Period | None
    captions: list[str]


#: A real statement always closes with at least one total, and carries more
#: than a couple of figures.
_MIN_VALUED_ROWS = 3


def _plausible(seg: _Segment) -> bool:
    """Reject things that merely *mention* a statement without being one.

    Sample 2's P&L workbook ships a second sheet written by DataSnipper (an
    audit tool) that indexes the source documents by filename. One of those
    filenames is `2025 Balance Sheet.pdf`, which matched the title pattern and
    produced a phantom balance sheet whose "accounts" were the tool's own
    progress figures. A document that names a statement is not a statement:
    demand the arithmetic furniture a real one always has.
    """
    valued = sum(1 for r in seg.rows if any(v is not None for v in r.values))
    has_total = any(is_total_label(r.label or "") for r in seg.rows)
    return valued >= _MIN_VALUED_ROWS and has_total


def _segment(part: RawPart) -> list[_Segment]:
    """Split one part into statements.

    Titles inside `rows` win, because that is where a stacked multi-statement
    sheet puts them. A part with no inline title is a single statement and we
    fall back to its captions (the PDF case, where the masthead is separated
    out by the reader).
    """
    cuts: list[tuple[int, StatementType | None]] = []
    for i, r in enumerate(part.rows):
        label = r.label or ""
        if _is_not_modelled(label):
            # A cut with no statement: everything from here to the next title
            # belongs to a statement we do not model, and is discarded below.
            cuts.append((i, None))
            continue
        st = _title_of(label)
        if st is not None:
            cuts.append((i, st))

    if not cuts:
        st = next((s for c in part.captions if (s := _title_of(c))), None)
        if st is None:
            # Only reach for the body markers once the part has been cleared of
            # statements we do not model -- `Net Income` is the first line of a
            # cash flow statement as well as the last line of an income one.
            if any(_is_not_modelled(c) for c in part.captions):
                return []
            st = next((s for r in part.rows if (s := _marker_of(r.label or ""))), None)
        if st is None:
            return []
        period = next((p for c in part.captions if (p := parse_period(c))), None)
        return [_Segment(st, list(part.rows), period, list(part.captions))]

    out: list[_Segment] = []
    for n, (start, st) in enumerate(cuts):
        if st is None:
            continue
        end = cuts[n + 1][0] if n + 1 < len(cuts) else len(part.rows)
        rows = part.rows[start:end]
        # The period caption sits within a few lines of the title.
        head = [r.label or "" for r in rows[:8]]
        period = next((p for h in head if (p := parse_period(h))), None)
        if period is None:
            period = next((p for c in part.captions if (p := parse_period(c))), None)
        out.append(_Segment(st, rows, period, head))
    return out


def _classify(rows: list[RawRow]) -> list[tuple[RawRow, RowKind, str | None]]:
    """Assign a kind and a section to every row, using depth where available."""
    have_depth = any(r.depth is not None for r in rows)
    out: list[tuple[RawRow, RowKind, str | None]] = []
    stack: list[tuple[int, str]] = []  # (depth, header label)
    flat_section: str | None = None

    for i, r in enumerate(rows):
        label = (r.label or "").strip()
        value = next((v for v in r.values if v is not None), None)

        if not label:
            out.append((r, RowKind.BLANK, None))
            continue

        if have_depth and r.depth is not None:
            while stack and stack[-1][0] >= r.depth:
                stack.pop()
            section = stack[-1][1] if stack else None
        else:
            section = flat_section

        if is_total_label(label) or is_derived_line(label):
            kind = RowKind.SUBTOTAL
        elif value is None:
            # A header is a valueless row that opens a group. With depth we can
            # confirm it: something below is nested deeper before we pop back
            # out. Without depth, a valueless label is treated as a header,
            # which is the long-standing spreadsheet heuristic.
            opens = True
            if have_depth and r.depth is not None:
                nxt = rows[i + 1] if i + 1 < len(rows) else None
                opens = nxt is not None and (nxt.depth or 0) > r.depth
            kind = RowKind.SECTION_HEADER if opens else RowKind.BLANK
        else:
            kind = RowKind.DATA

        if kind is RowKind.SECTION_HEADER:
            if have_depth and r.depth is not None:
                stack.append((r.depth, label))
            else:
                flat_section = label

        out.append((r, kind, section))
    return out


def _needs_resolution(seg: _Segment) -> bool:
    if seg.period is None:
        return True
    return seg.statement is StatementType.IS and seg.period.months is None


def _resolve_period(resolver, seg: _Segment, part, doc, findings: list[Finding]):
    """Escalate a caption the structural parser could not fully settle.

    Adopting a *length* is safe when the resolver agrees about the end date --
    it is filling a gap, not overruling a reading. Adopting a different end
    date is not, so a disagreement is reported and the structural answer wins:
    the deterministic layer is the one we can reproduce without a network.
    """
    if not _needs_resolution(seg):
        return seg.period

    candidates = list(seg.captions) + [part.name, doc.source.stem]
    for text in candidates:
        if not (text or "").strip():
            continue
        got, _how = resolver.resolve(text, seg.statement)
        if got is None:
            continue

        if seg.period is None:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="period_inferred",
                    message=(
                        f"{doc.source.name}::{part.name}: read {got} for the "
                        f"{seg.statement.value} from {text.strip()[:60]!r}. "
                        f"Confirm the period before relying on it."
                    ),
                    statement=seg.statement,
                    fiscal_year=got.fiscal_year,
                )
            )
            return got

        if got.end == seg.period.end and got.months is not None:
            return Period(end=seg.period.end, months=got.months, raw=seg.period.raw)

        if got.end != seg.period.end:
            findings.append(
                Finding(
                    severity=Severity.INFO,
                    code="period_disagreement",
                    message=(
                        f"{doc.source.name}::{part.name}: {text.strip()[:40]!r} reads "
                        f"as {got}, but the statement's own caption says "
                        f"{seg.period}; kept the caption."
                    ),
                    statement=seg.statement,
                    fiscal_year=seg.period.fiscal_year,
                )
            )
    return seg.period


def interpret(
    doc: RawDoc, *, entity_name: str | None = None, resolver=None
) -> tuple[list[ExtractedColumn], list[Finding]]:
    """Turn one source document into extracted columns plus any findings.

    `resolver` is an optional `fsa.ingest.period.PeriodResolver`. Without one
    the structural parser is the whole story, which is safe but leaves every
    `Year Ended ...` and `N Months Ended ...` caption without a period length.
    """
    findings: list[Finding] = []
    columns: list[ExtractedColumn] = []
    seen: dict[tuple, str] = {}

    for part in doc.parts:
        for seg in _segment(part):
            if not _plausible(seg):
                continue

            # Converting to Excel routinely strips the masthead -- sample 2's
            # balance sheet workbook has no period text anywhere in the cells,
            # only a sheet tab reading "Dec 2025". Fall back to the sheet name,
            # then the file name, and say so: an inferred period is a weaker
            # fact than a printed one.
            if seg.period is None:
                for source, text in (("sheet name", part.name), ("file name", doc.source.stem)):
                    guess = parse_period(text)
                    if guess is not None:
                        seg.period = guess
                        findings.append(
                            Finding(
                                severity=Severity.WARNING,
                                code="period_inferred",
                                message=(
                                    f"{doc.source.name}::{part.name}: the "
                                    f"{seg.statement.value} carries no period caption; "
                                    f"read {guess} from the {source}. Confirm this is "
                                    f"the full year before relying on it."
                                ),
                                statement=seg.statement,
                                fiscal_year=guess.fiscal_year,
                            )
                        )
                        break

            # Whatever structure could not settle goes to the resolver: either
            # no period at all, or -- on an income statement, which by
            # definition covers a span -- a date with no length. The second
            # case is the one that matters: it is how an 8-month stub or a TTM
            # column passes for a full year (PLAN 2.6).
            if resolver is not None:
                seg.period = _resolve_period(
                    resolver, seg, part, doc, findings
                )

            if seg.period is None:
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code="period_unreadable",
                        message=(
                            f"{doc.source.name}::{part.name}: could not read a "
                            f"reporting period for the {seg.statement.value}. "
                            f"Captions seen: {seg.captions[:4]}"
                        ),
                        statement=seg.statement,
                    )
                )
                continue

            # De-duplicate on statement + period + content, not on the page as
            # a whole. Sample 2's PDF repeats FY2020 verbatim, but its two
            # copies carry different print timestamps, so hashing the page
            # furniture would miss them; conversely two different years can
            # legitimately share a row shape, so content alone would merge
            # periods that must stay separate.
            key = (seg.statement, seg.period.key, _rows_hash(seg.rows))
            if key in seen:
                findings.append(
                    Finding(
                        severity=Severity.INFO,
                        code="duplicate_page_skipped",
                        message=(
                            f"{doc.source.name}: {part.name} repeats the "
                            f"{seg.statement.value} for {seg.period} already read "
                            f"from {seen[key]}; the copy was ignored"
                        ),
                        statement=seg.statement,
                        fiscal_year=seg.period.fiscal_year,
                    )
                )
                continue
            seen[key] = part.name

            if seg.period.is_partial:
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code="partial_period",
                        message=(
                            f"{doc.source.name}::{part.name}: the "
                            f"{seg.statement.value} covers {seg.period} -- a "
                            f"{seg.period.months}-month stub, not a full year. "
                            f"Mixing it with 12-month years would misstate the "
                            f"valuation. Supply the full-year statement or "
                            f"exclude this period."
                        ),
                        statement=seg.statement,
                        fiscal_year=seg.period.fiscal_year,
                        detail={"months": seg.period.months, "end": str(seg.period.end)},
                    )
                )

            rows: list[AccountRow] = []
            used: dict[str, str | None] = {}
            for raw, kind, section in _classify(seg.rows):
                label = (raw.label or "").strip()
                if kind is RowKind.BLANK or not label:
                    continue
                value = next((v for v in raw.values if v is not None), None)
                norm = normalize(label)

                # Two *data* rows sharing a name would collide downstream.
                # Qualify with the section rather than lose one.
                if kind in (RowKind.DATA, RowKind.SUBTOTAL) and norm in used:
                    if section and normalize(section) != used[norm]:
                        norm = normalize(f"{section} {label}")
                        findings.append(
                            Finding(
                                severity=Severity.INFO,
                                code="duplicate_label_qualified",
                                message=(
                                    f"{seg.statement.value}: {label!r} appears more "
                                    f"than once; the copy under {section!r} was "
                                    f"qualified to keep them distinct"
                                ),
                                statement=seg.statement,
                                account=label,
                            )
                        )
                if kind in (RowKind.DATA, RowKind.SUBTOTAL):
                    used.setdefault(norm, normalize(section) if section else None)

                if raw.truncated:
                    findings.append(
                        Finding(
                            severity=Severity.INFO,
                            code="label_truncated_at_source",
                            message=(
                                f"{seg.statement.value}: {label!r} was cut off by "
                                f"the client's own report; matching on a prefix"
                            ),
                            statement=seg.statement,
                            account=label,
                        )
                    )

                rows.append(
                    AccountRow(
                        raw_label=label,
                        norm_label=norm,
                        kind=kind,
                        value=value,
                        section=section,
                        row_index=raw.index,
                        ref=CellRef(file=doc.source, sheet=part.name, cell=raw.locator),
                        depth=raw.depth,
                    )
                )

            columns.append(
                ExtractedColumn(
                    statement=seg.statement,
                    fiscal_year=seg.period.fiscal_year,
                    role=ColumnRole.PRIMARY,
                    source_file=doc.source,
                    source_sheet=part.name,
                    value_column="v0",
                    label_column="label",
                    period_end=seg.period.end,
                    period_months=seg.period.months,
                    # A worksheet tab is often a reporting scope -- sample 3
                    # splits `consolidated` from `US`, `Canada` and `Bermuda`.
                    # A *default* tab name is not: `Sheet1` and `p2` are
                    # provenance. Recording them as entities made two halves of
                    # one Profit and Loss look like rival scopes, so they
                    # contested the year instead of being joined and half the
                    # statement was dropped.
                    entity=None if _is_default_name(part.name) else part.name,
                    rows=rows,
                )
            )

    return columns, findings


def read_any(path: Path) -> RawDoc:
    """Dispatch on file extension. The single front door for every format."""
    from fsa.ingest.raw import UnreadableSource

    ext = path.suffix.lower()
    if ext == ".pdf":
        from fsa.ingest.readers import pdf

        return pdf.read(path)
    if ext == ".ods":
        from fsa.ingest.readers import ods

        return ods.read(path)
    if ext in (".xlsx", ".xlsm"):
        from fsa.ingest.readers import xlsx

        return xlsx.read(path)
    raise UnreadableSource(
        f"{path.name}: unsupported format {ext!r}. Supported: .pdf, .ods, .xlsx"
    )
