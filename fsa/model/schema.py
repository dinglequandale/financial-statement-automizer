"""Core data model.

This module is the contract between the ingest layer and everything downstream.
It holds data and trivial helpers only -- no parsing, no validation, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path


class StatementType(str, Enum):
    BS = "BS"
    IS = "IS"


class RowKind(str, Enum):
    """What a row in a client statement actually is.

    Only DATA rows are eligible for mapping to the standardized template.
    SUBTOTAL rows are extracted so we can validate against them, never mapped
    (mapping them would double-count).
    """

    DATA = "data"
    SUBTOTAL = "subtotal"
    SECTION_HEADER = "section_header"
    TITLE = "title"
    BLANK = "blank"


class ColumnRole(str, Enum):
    """Where a year's numbers came from.

    PRIMARY     -- the file's own current-year column. Always preferred.
    COMPARATIVE -- the prior-year column inside a later year's file. Used for
                   cross-checking, and as a data source only as a last resort,
                   because it is laid out against the *prior* year's row list.
    """

    PRIMARY = "primary"
    COMPARATIVE = "comparative"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class CellRef:
    """Full provenance for a single extracted number."""

    file: Path
    sheet: str
    cell: str  # e.g. "C18"

    def __str__(self) -> str:
        return f"{self.file.name}::{self.sheet}!{self.cell}"


@dataclass(frozen=True)
class AccountRow:
    """One row of a client statement, as extracted."""

    raw_label: str
    norm_label: str
    kind: RowKind
    value: float | None
    section: str | None  # the client's own header, e.g. "Current Assets"
    row_index: int  # 1-based worksheet row
    ref: CellRef | None = None
    #: Nesting level, when the source encodes one (QuickBooks-style indented
    #: reports do; flat spreadsheets do not). Where present it makes subtotal
    #: scope exact rather than inferred -- see `checks.check_hierarchy`.
    depth: int | None = None

    @property
    def is_mappable(self) -> bool:
        return self.kind is RowKind.DATA


@dataclass
class ExtractedColumn:
    """One statement, one fiscal year, one column of numbers.

    `period_end` alone does not identify a column, and treating it as though it
    did produced two separate silent failures on the third sample client: a
    trailing-twelve-month income statement filed as a fiscal year, and a
    subsidiary's balance sheet standing in for the consolidated group. A column
    is identified by *when* it covers, *how long* it covers, and *who* it
    covers -- so all three are fields, and anything we cannot determine stays
    None and gets reported rather than assumed.
    """

    statement: StatementType
    fiscal_year: int
    role: ColumnRole
    source_file: Path
    source_sheet: str
    value_column: str  # Excel column letter the numbers came from
    label_column: str
    period_end: date | None = None
    #: Length of the period in months; None on a balance sheet (a point in
    #: time has no length) and None when it could not be determined. A value
    #: below 12 is a stub and must never be mixed with full years.
    period_months: int | None = None
    #: Which reporting entity these figures belong to -- a subsidiary, or the
    #: consolidated group. Sheet-level provenance, filled in wherever the
    #: source separates entities; None when the source does not.
    entity: str | None = None
    #: What the figures are denominated in. `scale` is the multiplier the
    #: client declared (1 = as reported, 1000 = stated in thousands) and
    #: `currency` an ISO code where one is stated. Both are part of a column's
    #: identity for the same reason its period is: figures denominated
    #: differently cannot be compared or summed, and nothing in the arithmetic
    #: gives it away -- a statement in thousands ties every subtotal perfectly.
    scale: int = 1
    scale_label: str | None = None
    currency: str | None = None
    rows: list[AccountRow] = field(default_factory=list)

    @property
    def is_partial(self) -> bool:
        """A period shorter than a full year. Never mix with annual columns."""
        return self.period_months is not None and self.period_months < 12

    @property
    def data_rows(self) -> list[AccountRow]:
        return [r for r in self.rows if r.kind is RowKind.DATA]

    @property
    def subtotal_rows(self) -> list[AccountRow]:
        return [r for r in self.rows if r.kind is RowKind.SUBTOTAL]

    def by_norm_label(self) -> dict[str, list[AccountRow]]:
        out: dict[str, list[AccountRow]] = {}
        for r in self.rows:
            if r.kind in (RowKind.DATA, RowKind.SUBTOTAL):
                out.setdefault(r.norm_label, []).append(r)
        return out

    def find(self, norm_label: str) -> AccountRow | None:
        hits = self.by_norm_label().get(norm_label)
        return hits[0] if hits else None


@dataclass
class StatementSet:
    """Everything extracted for one engagement."""

    entity_name: str | None = None
    columns: list[ExtractedColumn] = field(default_factory=list)

    def add(self, col: ExtractedColumn) -> None:
        self.columns.append(col)

    def get(
        self,
        statement: StatementType,
        fiscal_year: int,
        role: ColumnRole = ColumnRole.PRIMARY,
    ) -> ExtractedColumn | None:
        for c in self.columns:
            if (
                c.statement is statement
                and c.fiscal_year == fiscal_year
                and c.role is role
            ):
                return c
        return None

    def years(self, statement: StatementType) -> list[int]:
        return sorted(
            {
                c.fiscal_year
                for c in self.columns
                if c.statement is statement and c.role is ColumnRole.PRIMARY
            }
        )

    def contested(self) -> dict[tuple[StatementType, int], list[ExtractedColumn]]:
        """Slots where more than one PRIMARY column claims the same year.

        `get()` returns the first match, which is fine when a slot has one
        claimant and silently arbitrary when it has four. Sample 3 ships a
        workbook per year with a `consolidated` tab beside `US`, `Canada` and
        `Bermuda` tabs -- four columns for one slot, and whichever happened to
        be read first became the client's balance sheet. Surfacing the clash is
        the caller's cue to choose deliberately (or to make the analyst choose)
        rather than to inherit sheet ordering as a valuation decision.
        """
        slots: dict[tuple[StatementType, int], list[ExtractedColumn]] = {}
        for c in self.columns:
            if c.role is ColumnRole.PRIMARY:
                slots.setdefault((c.statement, c.fiscal_year), []).append(c)
        return {k: v for k, v in slots.items() if len(v) > 1}


@dataclass(frozen=True)
class Finding:
    """One validation result. Findings are data; rendering lives in report.py."""

    severity: Severity
    code: str  # stable machine-readable slug, e.g. "row_alignment_suspected"
    message: str
    statement: StatementType | None = None
    fiscal_year: int | None = None
    account: str | None = None
    ref: CellRef | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    def extend(self, fs: list[Finding]) -> None:
        self.findings.extend(fs)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def by_code(self, code: str) -> list[Finding]:
        return [f for f in self.findings if f.code == code]


@dataclass
class ConsolidatedRow:
    """One account in the merged multi-year view."""

    raw_label: str  # canonical display label (most recent year's spelling)
    norm_label: str
    kind: RowKind
    section: str | None
    values: dict[int, float | None] = field(default_factory=dict)  # year -> value
    provenance: dict[int, CellRef] = field(default_factory=dict)
    #: Nesting level carried through from the source, when it had one. Lets the
    #: reconcile pass resolve which accounts a rollup actually contains.
    depth: int | None = None


@dataclass
class ConsolidatedTable:
    """The Phase 0 deliverable: N client files merged into one aligned grid.

    Row order follows the most recent year's statement, with accounts that only
    appear in earlier years inserted at their nearest stable neighbour.
    """

    statement: StatementType
    years: list[int] = field(default_factory=list)
    rows: list[ConsolidatedRow] = field(default_factory=list)

    def find(self, norm_label: str) -> ConsolidatedRow | None:
        for r in self.rows:
            if r.norm_label == norm_label:
                return r
        return None
