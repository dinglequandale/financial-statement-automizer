"""Plan the write: which row each target lands on, and what formula goes in it.

Deliberately pure. Everything here is decided and unit-tested *before* Excel is
opened, because the COM step is the one place a mistake silently corrupts a
delivered model rather than raising.

Two jobs:

1. **Row allocation.** A target may already exist, may take over an unused
   placeholder slot, may repurpose an unused named line, or may need a new row
   inserted. The delivered TS model shows all four in one engagement -- the
   analyst renamed `Land and Buildings` to `Vehicles` (repurpose) and inserted
   `Software` outright.

2. **Formula generation.** House style, matching the delivered model:
   `=SUM('Historical BS'!G18:G21,'Historical BS'!G27)`. Contiguous source rows
   collapse into ranges so a reviewer sees what they expect.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fsa.mapping.template import TemplateBlock, TemplateSpec
from fsa.model.mapping import Decider, MappingSet, TargetKind
from fsa.model.schema import StatementType


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class RowAction:
    """One row's fate on a standardized tab."""

    target_label: str
    kind: str  # existing | rename_slot | repurpose | insert
    row: int  # planned final row (pre-insert coordinates for `insert`)
    block: str | None = None
    old_label: str | None = None  # what we are renaming away from


@dataclass
class WritePlan:
    statement: StatementType
    actions: list[RowAction] = field(default_factory=list)
    # target label -> [(historical row, sign)] sorted by row
    sources: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def inserts(self) -> list[RowAction]:
        return [a for a in self.actions if a.kind == "insert"]

    def action_for(self, label: str) -> RowAction | None:
        for a in self.actions:
            if a.target_label == label:
                return a
        return None


def _block_of(spec: TemplateSpec, statement: StatementType, name: str) -> TemplateBlock | None:
    for b in spec.blocks:
        if b.statement is statement and b.subtotal_label == name:
            return b
    return None


def build_plan(
    ms: MappingSet,
    spec: TemplateSpec,
    hist_rows: dict[str, int],
    *,
    require_confirmed: bool = True,
) -> WritePlan:
    """Decide the final row layout and the source rows behind each target.

    `hist_rows` maps a normalized client account to its row on the Historical
    tab. `require_confirmed` refuses to write anything a human has not signed
    off on -- an LLM proposal must never reach a delivered model unreviewed.
    """
    plan = WritePlan(statement=ms.statement)

    usable = []
    for r in ms.rules:
        if r.decided_by is Decider.UNRESOLVED:
            plan.warnings.append(f"unresolved, not written: {r.client_account}")
            continue
        if require_confirmed and not r.decided_by.is_confirmed:
            plan.warnings.append(
                f"unconfirmed ({r.decided_by.value}), not written: {r.client_account}"
            )
            continue
        if r.norm_account not in hist_rows:
            plan.warnings.append(f"no Historical row for: {r.client_account}")
            continue
        usable.append(r)

    if not usable:
        raise PlanError(
            f"{ms.statement.value}: nothing confirmed to write "
            f"({len(plan.warnings)} skipped). Confirm the mapping first."
        )

    # Collect source rows per target, preserving sign.
    for r in usable:
        plan.sources.setdefault(r.target.label, []).append(
            (hist_rows[r.norm_account], r.sign)
        )
    for label in plan.sources:
        plan.sources[label].sort()

    # Which template rows are already spoken for by an exact-name target.
    claimed: set[int] = set()
    targets = {r.target.label: r.target for r in usable}

    for label, target in targets.items():
        line = spec.find(ms.statement, label)
        if line is not None and line.is_target:
            plan.actions.append(
                RowAction(label, "existing", line.row, line.block)
            )
            claimed.add(line.row)

    # Everything else needs a home. Order matters only for determinism.
    for label, target in sorted(targets.items()):
        if plan.action_for(label) is not None:
            continue

        block_name = target.block or (
            spec.find(ms.statement, target.slot_label or "").block
            if target.slot_label
            else None
        )
        blk = _block_of(spec, ms.statement, block_name) if block_name else None
        if blk is None:
            plan.warnings.append(
                f"no block for target {label!r}; skipped (an inserted line "
                f"without a block has no defined position)"
            )
            plan.sources.pop(label, None)
            continue

        # 1) The named slot the model asked for, if it is free.
        slot = (
            spec.find(ms.statement, target.slot_label)
            if target.kind is TargetKind.NAME_SLOT and target.slot_label
            else None
        )
        if slot is not None and slot.row not in claimed and not slot.frozen:
            plan.actions.append(
                RowAction(label, "rename_slot", slot.row, blk.subtotal_label, slot.label)
            )
            claimed.add(slot.row)
            continue

        # 2) Any free placeholder in the block.
        free_slot = next(
            (
                l
                for l in spec.targets(ms.statement)
                if l.placeholder
                and blk.contains(l.row)
                and l.row not in claimed
                and not l.frozen
            ),
            None,
        )
        if free_slot is not None:
            plan.actions.append(
                RowAction(label, "rename_slot", free_slot.row, blk.subtotal_label, free_slot.label)
            )
            claimed.add(free_slot.row)
            continue

        # 3) Repurpose an unused, unfrozen named line -- what the analyst did
        #    turning `Land and Buildings` into `Vehicles`.
        spare = next(
            (
                l
                for l in spec.targets(ms.statement)
                if blk.contains(l.row)
                and l.row not in claimed
                and not l.frozen
                and not l.collapsible
            ),
            None,
        )
        if spare is not None:
            plan.actions.append(
                RowAction(label, "repurpose", spare.row, blk.subtotal_label, spare.label)
            )
            claimed.add(spare.row)
            continue

        # 4) Insert -- at `last_row`, NOT `last_row + 1`.
        #
        # Excel only widens a range when the insertion point falls strictly
        # inside it. For a block summed by `=SUM(E9:E13)`, inserting at row 14
        # leaves the formula as `SUM(E9:E13)`, pushes the subtotal to row 15,
        # and silently orphans the new row outside its own subtotal -- the
        # value is written, looks right on screen, and never reaches any total.
        # Inserting at 13 shifts the old row 13 down and the formula becomes
        # `SUM(E9:E14)`, which includes the new row.
        #
        # Caught by end-to-end verification: an inserted `Employee Advances`
        # row was excluded from Total Current Assets in all five years, and the
        # balance check was off by exactly that account's value each year.
        # ...and at the highest NON-frozen row in the block. Inserting onto a
        # frozen row's position displaces it, which the template's
        # `Do not add row before/after >>` markers exist to forbid.
        frozen_rows = {
            l.row
            for l in spec.lines
            if l.statement is ms.statement and l.frozen and blk.contains(l.row)
        }
        at = next(
            (r for r in range(blk.last_row, blk.first_row - 1, -1) if r not in frozen_rows),
            None,
        )
        if at is None:
            plan.warnings.append(
                f"block {blk.subtotal_label!r} is entirely frozen; cannot insert {label!r}"
            )
            plan.sources.pop(label, None)
            continue
        plan.actions.append(RowAction(label, "insert", at, blk.subtotal_label))

    return plan


def build_formula(hist_sheet: str, hist_col: str, sources: list[tuple[int, int]]) -> str:
    """Render source rows as one Excel formula, in the delivered model's style.

    Contiguous same-sign rows collapse into ranges. Negative-sign sources are
    subtracted, mirroring `=...+Z16-SUM(Z136:Z137)` in the delivered model.
    """
    if not sources:
        return ""

    def runs(rows: list[int]) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for r in sorted(rows):
            if out and r == out[-1][1] + 1:
                out[-1] = (out[-1][0], r)
            else:
                out.append((r, r))
        return out

    def render(rows: list[int]) -> str:
        parts = [
            f"'{hist_sheet}'!{hist_col}{a}"
            if a == b
            else f"'{hist_sheet}'!{hist_col}{a}:{hist_col}{b}"
            for a, b in runs(rows)
        ]
        return f"SUM({','.join(parts)})"

    pos = [r for r, s in sources if s > 0]
    neg = [r for r, s in sources if s < 0]

    if pos and neg:
        return f"={render(pos)}-{render(neg)}"
    if neg:
        return f"=-{render(neg)}"
    return f"={render(pos)}"
