"""Finish the mapping from the client's own grouping, without another API call.

The measured problem this solves. On TS Distributors' income statement the
model resolved 64 accounts and declined on 42, and the declines are not spread
evenly -- they cluster inside the client's own sections:

    Salaries & Wage Expense    17 unresolved,  4 resolved
    Operating Expenses         16 unresolved, 37 resolved
    COST OF GOODS SOLD          6 unresolved,  6 resolved

Those three sections are 86 rows and, in the delivered model, exactly three
decisions: `Total Salaries & Wages -> Salaries & Wages`, `Total Operating
Expenses -> SG&A Expenses`, `Total Cost of Goods Sold -> Cost of Revenue`. The
engine asked about each account independently and answered an arbitrary subset
of each group -- PLAN 7.4's coverage variance, which is a property of asking N
independent questions about one decision.

**The principle, which is the client's arithmetic and not any client's
vocabulary:** a client group whose resolved members all land in the same
template block is a homogeneous group. An unresolved sibling of a homogeneous
group belongs in that block too, because the client already asserted these
accounts belong together by summing them into one subtotal that ties.

Two guards keep the inference honest, and both are structural:

1. **Unanimity.** If the resolved members disagree about the block, the group
   is heterogeneous and nothing is inferred. This is what stops the rule
   touching Commercial Flooring's fixed assets, where `Machinery and
   Equipment`, `Furniture and Equipment` and `Autos and Trucks` deliberately
   split across three template lines.
2. **The known part must be the majority of the money.** Inferring the large
   from the small is backwards; if the resolved siblings are a rounding error
   next to the unresolved ones, the group is not understood well enough to
   extrapolate from.

**Why this is safe where it is wrong.** The inference can only place a row in a
block its own siblings already occupy, so its failure mode is a line that is
too coarse -- `Officer Compensation` bucketed with operating expenses rather
than given the dedicated line a valuation would want -- never money in the
wrong subtotal. That is the cosmetic axis, not the axis that moves a valuation
(PLAN 7.2b), and arithmetic verification still checks the result. Completed
rules carry the weakest confidence of the siblings they were inferred from and
are marked `STRUCTURE`, so they land in review as a check, never as a skim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from fsa.model.mapping import Decider, MappingRule, MappingSet, Target, TargetKind
from fsa.model.schema import ConsolidatedTable, RowKind

#: Completed rules must always be reviewed, never skimmed. Expressed against
#: the review sheet's own threshold rather than as a second magic number.
from fsa.review.sheet import LOW_CONFIDENCE

_CEILING = LOW_CONFIDENCE - 0.01

#: The resolved part of a group must carry more than this share of the group's
#: money before its answer is extended to the rest. A bare majority is the
#: weakest defensible reading of "we understand this group".
MAJORITY = 0.5

#: A label may only be extended to new accounts if the group's own resolved
#: members already share it -- i.e. it is functioning as a bucket rather than
#: as one account's dedicated line.
#:
#: This is the condition that separates the two shapes a homogeneous group
#: takes, and without it the rule fabricates. TS's operating expenses put 28 of
#: 37 resolved members on `Other Operating Expenses`: a bucket, and the
#: unresolved siblings plainly belong in it. TS's cost of goods sold resolved
#: six members onto *six different* lines -- `Freight Costs`, `Inventory
#: Adjustments`, `Supplier Discounts`, one account each. There is no bucket
#: there, and extending whichever name happened to win a tiebreak produced
#: `Freight - Outbound -> Supplier Discounts`: correctly placed, and nonsense
#: to read. A group mapped that specifically is telling us it needs an analyst,
#: not an inference.
MODAL_SHARE = 0.5


@dataclass
class CompletionReport:
    completed: int = 0
    groups_used: int = 0
    groups_skipped_split: int = 0
    groups_skipped_thin: int = 0
    groups_skipped_specific: int = 0
    detail: list[tuple[str, str, int]] = field(default_factory=list)


def _block_of(spec, statement, rule) -> str | None:
    """Where a rule's money lands. From the proposal, never from its label."""
    if rule.target.block:
        return rule.target.block
    line = spec.find(statement, rule.target.label)
    if line is not None:
        if line.block:
            return line.block
        if line.derived and line.collapsible:
            return line.label
    if rule.target.slot_label:
        slot = spec.find(statement, rule.target.slot_label)
        if slot is not None and slot.block:
            return slot.block
    return None


def _groups(table: ConsolidatedTable) -> dict[str, list]:
    """The client's own groups: section for flat sources, depth for nested.

    Deliberately uses the *section* rather than subtotal descendants. A section
    is present on every source we read -- Excel via header inference, PDF via
    the indent ladder -- whereas a tying subtotal is not always there. The
    group is the unit the client themselves chose to add up.
    """
    out: dict[str, list] = {}
    for row in table.rows:
        if row.kind is not RowKind.DATA or not row.section:
            continue
        out.setdefault(row.section, []).append(row)
    return out


def complete_by_siblings(
    ms: MappingSet, table: ConsolidatedTable, spec
) -> CompletionReport:
    """Extend a homogeneous group's answer to its unresolved members."""
    rep = CompletionReport()
    if not table.years:
        return rep
    year = max(table.years)
    now = datetime.now(timezone.utc)

    by_norm = {r.norm_account: r for r in ms.rules}
    excluded = {e.norm_account for e in ms.exclusions}

    def _val(row) -> float:
        v = row.values.get(year)
        return abs(v) if isinstance(v, (int, float)) else 0.0

    for section, rows in _groups(table).items():
        resolved, pending = [], []
        for row in rows:
            if row.norm_label in excluded:
                continue
            rule = by_norm.get(row.norm_label)
            if rule is None:
                continue
            if rule.decided_by is Decider.UNRESOLVED:
                pending.append((row, rule))
            else:
                resolved.append((row, rule))

        if not pending or len(resolved) < 2:
            if pending:
                rep.groups_skipped_thin += 1
            continue

        blocks = {_block_of(spec, table.statement, r) for _, r in resolved}
        blocks.discard(None)
        if len(blocks) != 1:
            rep.groups_skipped_split += 1
            continue

        known = sum(_val(row) for row, _ in resolved)
        unknown = sum(_val(row) for row, _ in pending)
        if known + unknown <= 0 or known / (known + unknown) <= MAJORITY:
            rep.groups_skipped_thin += 1
            continue

        # Inherit only a label the group is already using as a bucket. If the
        # resolved members each have their own dedicated line, there is nothing
        # to join and the group goes to a human intact.
        counts: dict[str, int] = {}
        for _, r in resolved:
            counts[r.target.label] = counts.get(r.target.label, 0) + 1
        label = max(counts, key=lambda k: (counts[k], k))
        if counts[label] / len(resolved) <= MODAL_SHARE:
            rep.groups_skipped_specific += 1
            continue
        donor = next(r for _, r in resolved if r.target.label == label)
        conf = min(_CEILING, min(r.confidence for _, r in resolved))

        for row, rule in pending:
            rule.target = Target(
                statement=table.statement,
                label=donor.target.label,
                kind=(
                    TargetKind.ASSIGN
                    if donor.target.kind is TargetKind.NAME_SLOT
                    else donor.target.kind
                ),
                block=donor.target.block,
                slot_label=None,
            )
            rule.sign = donor.sign
            rule.confidence = conf
            rule.decided_by = Decider.STRUCTURE
            rule.decided_at = now
            rule.rationale = (
                f"grouped with its siblings under {section!r}, all of which "
                f"map to {donor.target.label!r} -- inferred, not proposed"
            )
            rep.completed += 1

        rep.groups_used += 1
        rep.detail.append((section, donor.target.label, len(pending)))

    return rep


def complete_by_rollup(
    ms: MappingSet, table: ConsolidatedTable, spec
) -> CompletionReport:
    """Answer a not-understood group once, at its rollup, instead of N times.

    `complete_by_siblings` handles the group we *do* understand. This handles
    its opposite: a group where most of the money is unresolved and the few
    resolved members each took a dedicated line, so there is no bucket to
    extend. TS's `COST OF GOODS SOLD` (6 unresolved of 12, six distinct target
    lines) and `Salaries & Wage Expense` (17 unresolved of 21) are both this
    shape, and between them they are 24 red rows standing for two decisions.
    Both delivered models answer exactly this shape at the rollup:
    `Total Cost of Goods Sold -> Cost of Revenue`,
    `Total Salaries & Wages -> Salaries & Wages`.

    The rule is PLAN 7.2b's completeness rule, applied before review rather
    than after: **when not every member is mapped, only the rollup is
    guaranteed to cover every member.** Mapping it keeps the block total right
    whatever we did or did not understand underneath, and `reconcile` then
    turns the members into pre-filled exclusions -- so N unanswerable rows
    become one answerable one with its contents listed beneath it.

    Naming comes from the client's own label with the rollup word dropped
    (`Total Salaries & Wages` -> `Salaries & Wages`), which is both the least
    inventive option available and, on TS, exactly what the analyst chose.

    Deliberately conservative: it fires only when the group's own arithmetic
    holds, when the resolved members agree on one block, and when the majority
    of the group's money is unresolved -- i.e. only where we would otherwise be
    handing the analyst a pile of rows with no answer on any of them.
    """
    from fsa.validate.mapping_checks import GROUP, classify_client_rows

    rep = CompletionReport()
    if not table.years:
        return rep
    year = max(table.years)
    now = datetime.now(timezone.utc)
    kinds = classify_client_rows(table)
    by_norm = {r.norm_account: r for r in ms.rules}
    excluded = {e.norm_account for e in ms.exclusions}

    def _val(row) -> float:
        v = row.values.get(year)
        return abs(v) if isinstance(v, (int, float)) else 0.0

    # The client's rollup for a section is the subtotal that carries it.
    rollup_for: dict[str, object] = {}
    for row in table.rows:
        if row.kind is RowKind.SUBTOTAL and kinds.get(row.norm_label) == GROUP:
            if row.section:
                rollup_for.setdefault(row.section, row)

    for section, rows in _groups(table).items():
        rollup = rollup_for.get(section)
        if rollup is None or rollup.norm_label in excluded:
            continue
        rrule = by_norm.get(rollup.norm_label)
        if rrule is None or rrule.decided_by is not Decider.UNRESOLVED:
            continue  # already answered, or not ours to answer

        resolved, pending = [], []
        for row in rows:
            if row.norm_label in excluded:
                continue
            rule = by_norm.get(row.norm_label)
            if rule is None:
                continue
            (pending if rule.decided_by is Decider.UNRESOLVED else resolved).append(row)

        if not pending:
            continue
        known = sum(_val(r) for r in resolved)
        unknown = sum(_val(r) for r in pending)
        if known + unknown <= 0 or unknown / (known + unknown) < MAJORITY:
            rep.groups_skipped_thin += 1
            continue  # we understand most of this group; leave it alone

        blocks = {
            _block_of(spec, table.statement, by_norm[r.norm_label]) for r in resolved
        }
        blocks.discard(None)
        if len(blocks) != 1:
            rep.groups_skipped_split += 1
            continue
        block = blocks.pop()

        label = _rollup_name(rollup.raw_label)
        line = spec.find(table.statement, label)
        kind = TargetKind.ASSIGN if (line and line.is_target) else TargetKind.INSERT
        rrule.target = Target(
            statement=table.statement, label=label, kind=kind, block=block
        )
        rrule.sign = 1
        rrule.confidence = _CEILING
        rrule.decided_by = Decider.STRUCTURE
        rrule.decided_at = now
        rrule.rationale = (
            f"{len(pending)} of this group's {len(rows)} accounts had no "
            f"proposal, so the group is mapped at its own total -- the only "
            f"level guaranteed to cover all of them. Its members are listed "
            f"below as exclusions; overtype any to map it separately instead."
        )
        rep.completed += 1
        rep.groups_used += 1
        rep.detail.append((section, f"{rollup.raw_label} -> {label}", len(pending)))

    return rep


#: Words a client puts in front of a subtotal's name. Dropping them recovers
#: the group's own name, which is the least inventive label available and needs
#: no vocabulary of our own.
_ROLLUP_WORDS = ("total", "net", "sum of")


def _rollup_name(raw: str) -> str:
    out = raw.strip().strip(":").strip()
    low = out.lower()
    for w in _ROLLUP_WORDS:
        if low.startswith(w + " "):
            out = out[len(w) + 1 :].strip()
            break
    return out or raw.strip()
