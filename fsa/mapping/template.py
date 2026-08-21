"""Read the standardized target taxonomy out of a blank BVAL template.

Everything here is *derived from the template file*, never hardcoded. The
template's own subtotal formulas define the blocks: `BS!E14 = SUM(E9:E13)` says
rows 9-13 are the current-asset block and row 14 is its total. That gives us,
for free:

  - the set of legal mapping targets (what the LLM may propose)
  - which targets belong to which block (what arithmetic verification checks)
  - which rows are structurally frozen (`Do Not Change >>>` in column A)

Deriving rather than hardcoding matters because Weaver's template evolves and
because the delivered TS model proves analysts insert and repurpose rows -- a
row-number constant would be wrong within one engagement.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from fsa.model.schema import StatementType

# `=SUM(E9:E13)` / `=SUM(E25:E30)` -- a contiguous single-column block total.
_SUM_RANGE = re.compile(r"^=SUM\(([A-Z]{1,2})(\d+):([A-Z]{1,2})(\d+)\)$", re.I)

# Structural markers Weaver writes in column A.
_FROZEN_MARKERS = ("do not change", "do not add row")

# `Revenue Component 2`, `Operating Expense 5`, `Other Income (Expense) 3` --
# numbered capacity slots rather than named categories.
_NUMBERED_SLOT = re.compile(r"^(?P<stem>.+?)\s+(?P<n>\d+)$")
_SLOT_WORDS = ("component",)

_LABEL_COL = "C"
_MARKER_COL = "A"
_PROBE_COL = "E"  # first historical-year column; formulas here define the blocks


class TemplateError(RuntimeError):
    pass


@dataclass(frozen=True)
class TemplateLine:
    """One line on a standardized tab.

    `derived` is the load-bearing flag: a line whose probe cell already holds a
    formula is computed by the template (`Total Assets = TCA + NFA + TOA`,
    `Operating Income = Gross Profit - Total Operating Exp.`) and must never be
    a mapping target -- writing into it would overwrite the model's own
    arithmetic. Only non-derived lines are targets.
    """

    statement: StatementType
    row: int
    label: str
    frozen: bool  # column A carries a Do-Not-Change marker
    derived: bool  # probe cell holds a formula; computed, not an input
    collapsible: bool = False  # block subtotal that may be written directly
    placeholder: bool = False  # unnamed capacity slot the analyst names
    block: str | None = None  # label of the subtotal this line rolls into

    @property
    def is_target(self) -> bool:
        """May a client account be mapped here?

        Plain input lines, plus *collapsible* block subtotals. The delivered TS
        model proves the latter is real practice: the client reported one
        `Total Cost of Goods Sold` with no breakdown, so the analyst wrote it
        straight into `Cost of Revenue` (overwriting `=SUM(E15:E17)`) and left
        the three COGS component rows empty.

        Collapsing a block subtotal is safe because it sums only its own block.
        Cross-block roll-ups (`Total Assets`) and computed results
        (`Gross Profit`, `Operating Income`, `Net Income`) are never
        collapsible -- overwriting those desynchronizes the model's arithmetic.
        """
        return not self.derived or self.collapsible

    @property
    def key(self) -> str:
        return f"{self.statement.value}::{self.label}"


@dataclass(frozen=True)
class TemplateBlock:
    """A contiguous run of input lines plus the subtotal that sums them."""

    statement: StatementType
    subtotal_label: str
    subtotal_row: int
    first_row: int
    last_row: int

    def contains(self, row: int) -> bool:
        return self.first_row <= row <= self.last_row


@dataclass
class TemplateSpec:
    source: Path
    lines: list[TemplateLine] = field(default_factory=list)
    blocks: list[TemplateBlock] = field(default_factory=list)

    def targets(self, statement: StatementType | None = None) -> list[TemplateLine]:
        """Legal mapping targets -- input lines only.

        Excludes every derived line, which covers block subtotals
        (`Total Current Assets`), cross-block roll-ups (`Total Assets`), and
        computed results (`Operating Income`, `Gross Profit`) alike.
        """
        return [
            l
            for l in self.lines
            if (statement is None or l.statement is statement) and l.is_target
        ]

    def block_for(self, statement: StatementType, row: int) -> TemplateBlock | None:
        for b in self.blocks:
            if b.statement is statement and b.contains(row):
                return b
        return None

    def find(self, statement: StatementType, label: str) -> TemplateLine | None:
        want = label.strip().casefold()
        for l in self.lines:
            if l.statement is statement and l.label.casefold() == want:
                return l
        return None


def _load(path: Path) -> openpyxl.Workbook:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return openpyxl.load_workbook(path, data_only=False)


def _is_frozen(marker: object) -> bool:
    if not isinstance(marker, str):
        return False
    m = marker.strip().casefold()
    return any(m.startswith(x) for x in _FROZEN_MARKERS)


def load_template(path: Path) -> TemplateSpec:
    """Parse a blank (or completed) BVAL model into a TemplateSpec."""
    wb = _load(path)
    spec = TemplateSpec(source=path)

    for statement in (StatementType.BS, StatementType.IS):
        name = statement.value
        if name not in wb.sheetnames:
            raise TemplateError(
                f"{path.name}: no '{name}' sheet -- not a BVAL template? "
                f"(sheets: {', '.join(wb.sheetnames[:8])}...)"
            )
        ws = wb[name]

        labelled: dict[int, str] = {}
        for r in range(1, ws.max_row + 1):
            v = ws[f"{_LABEL_COL}{r}"].value
            if isinstance(v, str) and v.strip():
                labelled[r] = v.strip()

        # Blocks come from the template's own SUM formulas.
        blocks: list[TemplateBlock] = []
        for r, label in labelled.items():
            f = ws[f"{_PROBE_COL}{r}"].value
            if not isinstance(f, str):
                continue
            m = _SUM_RANGE.match(f.strip())
            if not m:
                continue
            first, last = int(m.group(2)), int(m.group(4))
            if last < first or first > r:
                continue
            blocks.append(
                TemplateBlock(
                    statement=statement,
                    subtotal_label=label,
                    subtotal_row=r,
                    first_row=first,
                    last_row=last,
                )
            )

        if not blocks:
            raise TemplateError(
                f"{path.name}!{name}: found no block subtotals (expected formulas like "
                f"'=SUM({_PROBE_COL}9:{_PROBE_COL}13)' in column {_PROBE_COL})"
            )

        # A numbered slot is a placeholder when it has numbered siblings sharing
        # its stem (`Revenue Component 1/2/3`), or when its stem says so
        # outright (`... Component`). The *unnumbered* base of such a family is
        # a placeholder too -- `Other Income (Expense)` heads
        # `Other Income (Expense) 2..5` -- so long as it isn't independently
        # meaningful, which is why we require numbered siblings to exist.
        stems: dict[str, int] = {}
        for label in labelled.values():
            m = _NUMBERED_SLOT.match(label)
            if m:
                stems[m.group("stem").casefold()] = stems.get(m.group("stem").casefold(), 0) + 1

        def _is_placeholder(label: str) -> bool:
            low = label.casefold()
            if any(w in low for w in _SLOT_WORDS):
                return True
            m = _NUMBERED_SLOT.match(label)
            if m and stems.get(m.group("stem").casefold(), 0) >= 1:
                return True
            return stems.get(low, 0) >= 1  # unnumbered head of a numbered family

        subtotal_rows = {b.subtotal_row for b in blocks}
        for r, label in labelled.items():
            probe = ws[f"{_PROBE_COL}{r}"].value
            derived = isinstance(probe, str) and probe.startswith("=")
            blk = next((b for b in blocks if b.contains(r) and b.subtotal_row != r), None)
            spec.lines.append(
                TemplateLine(
                    statement=statement,
                    row=r,
                    label=label,
                    frozen=_is_frozen(ws[f"{_MARKER_COL}{r}"].value),
                    derived=derived,
                    collapsible=r in subtotal_rows,
                    placeholder=_is_placeholder(label),
                    block=blk.subtotal_label if blk else None,
                )
            )
        spec.blocks.extend(blocks)

    return spec


def taxonomy_prompt(spec: TemplateSpec, statement: StatementType) -> str:
    """The target list, grouped by block, for the LLM proposer.

    Grouping is not cosmetic: block membership is what arithmetic verification
    checks, so showing the model which block each line rolls into is the single
    most useful piece of context for avoiding cross-block errors.
    """
    targets = spec.targets(statement)
    out: list[str] = []
    placed: set[int] = set()

    for b in [x for x in spec.blocks if x.statement is statement]:
        members = [l for l in targets if b.contains(l.row)]
        if not members:
            continue
        out.append(f"{b.subtotal_label}:")
        out.extend(f"  - {l.label}" for l in members)
        placed.update(l.row for l in members)

    loose = [l for l in targets if l.row not in placed]
    if loose:
        out.append("(standalone -- not part of a block subtotal):")
        out.extend(f"  - {l.label}" for l in loose)
    return "\n".join(out)
