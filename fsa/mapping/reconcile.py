"""Enforce one invariant: every client account reaches the model exactly once.

No individual mapping layer can enforce this, because it is a property of the
*set* of decisions. The alias layer resolves a detail row; the LLM independently
resolves the rollup that contains it; each is defensible alone and together they
double-count. Sample 2 produced exactly that -- `Accounts Receivable` matched by
alias at 0.95 and `Total Accounts Receivable` proposed by the LLM at 0.90, both
landing on the template's `Accounts Receivable` line.

This runs after every layer and before review. It does three things:

1. **Covers descendants of a mapped rollup.** Choosing `Total Expense ->
   Operating Expenses` accounts for the thirty expense rows beneath it. They are
   excluded with a reason naming the rollup, not left for a human to wade
   through. This is a deterministic consequence of the hierarchy, not judgment.

2. **Reports double-counting it cannot safely resolve.** When a rollup *and* one
   of its descendants are both mapped, silently dropping either is a valuation
   error. The delivered TS model shows the legitimate third option -- map the
   rollup and subtract the descendant (`=...+Z16-SUM(Z136:Z137)`) -- which is an
   analyst's call. So: flag, never guess.

3. **Turns ASSIGN on a placeholder into NAME_SLOT.** A capacity slot named
   `Other Income (Expense) 2` must be *renamed* to the client's account, or the
   delivered model ships with the template's filler text as a line item.

Hierarchy comes from `depth` where the source had it. Where it did not, we fall
back to the client's own section headers, which is weaker but still correct for
the common one-level case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from fsa.mapping.template import TemplateSpec
from fsa.model.mapping import Decider, Exclusion, MappingSet, TargetKind
from fsa.model.schema import (
    ConsolidatedRow,
    ConsolidatedTable,
    Finding,
    RowKind,
    Severity,
)


@dataclass
class ReconcileReport:
    covered: int = 0  # descendants excluded because a rollup covers them
    conflicts: int = 0  # rollup and descendant both mapped
    renamed_slots: int = 0  # ASSIGN on a placeholder -> NAME_SLOT
    uncovered: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def descendants(table: ConsolidatedTable, idx: int) -> list[ConsolidatedRow]:
    """Rows contained by the row at `idx`.

    With depth, a subtotal owns the contiguous run of deeper rows immediately
    above it -- QuickBooks prints the total *after* its members. Without depth,
    fall back to the client's section header.
    """
    row = table.rows[idx]
    if row.depth is not None:
        out: list[ConsolidatedRow] = []
        for prev in reversed(table.rows[:idx]):
            if prev.depth is None or prev.depth <= row.depth:
                break
            out.append(prev)
        return out

    if not row.section:
        return []
    return [
        r
        for i, r in enumerate(table.rows)
        if i != idx and r.section == row.section and r.kind is RowKind.DATA
    ]


def rows_under_subtotals(table: ConsolidatedTable) -> set[str]:
    """Normalized labels of every DATA row contained by some client subtotal.

    A row *not* in this set has no rollup that could stand in for it, so it must
    be mapped on its own account whatever level policy is in force.
    """
    out: set[str] = set()
    for i, row in enumerate(table.rows):
        if row.kind is RowKind.SUBTOTAL:
            out |= {
                k.norm_label for k in descendants(table, i) if k.kind is RowKind.DATA
            }
    return out


def reconcile(
    ms: MappingSet,
    table: ConsolidatedTable,
    spec: TemplateSpec,
    *,
    cover_complete_rollups: bool = True,
) -> ReconcileReport:
    """Resolve coverage and report what cannot be resolved. Mutates `ms`.

    `cover_complete_rollups` exists to be turned off in measurement, not in
    production: it isolates the rule below so its effect on the delivered
    models can be scored on its own, without LLM run-to-run variance sitting on
    top of the comparison. Leave it alone in the pipeline.
    """
    rep = ReconcileReport()
    now = datetime.now(timezone.utc)

    mapped = {
        r.norm_account: r
        for r in ms.rules
        if r.decided_by is not Decider.UNRESOLVED
    }
    excluded = {e.norm_account for e in ms.exclusions}

    # 3) A placeholder must be named, not merely written into.
    for rule in ms.rules:
        if rule.decided_by is Decider.UNRESOLVED:
            continue
        line = spec.find(ms.statement, rule.target.label)
        if line is not None and line.placeholder and rule.target.kind is TargetKind.ASSIGN:
            rule.target = type(rule.target)(
                statement=rule.target.statement,
                label=rule.client_account,
                kind=TargetKind.NAME_SLOT,
                slot_label=line.label,
                block=line.block or rule.target.block,
            )
            rep.renamed_slots += 1

    # 1) and 2): walk every mapped rollup and settle what it contains.
    #
    # Detecting a double-count is not enough -- an earlier version reported it
    # and left both rules in place, so `build_plan` wrote both and the money
    # landed twice. Measured on sample 2: Total Current Assets came out
    # $736,592 high because the alias layer matched the detail `Accounts
    # Receivable` while the LLM matched `Total Accounts Receivable`.
    #
    # Which side wins is not a preference, it is determined by completeness:
    # keeping the total correct outranks keeping the split correct.
    #   * every member mapped  -> the detail accounts for the whole group, so
    #     drop the rollup and keep the finer breakdown.
    #   * otherwise            -> only the rollup is guaranteed to cover every
    #     member, so drop the mapped members. Their placement is lost; the
    #     statement's arithmetic is not.
    # Either way the ERROR still fires, so a human sees and can reverse it.
    #
    # Outermost first: if a group and a nested sub-group are both mapped, the
    # outer one covers strictly more, so it settles the inner conflict too.
    covered_by: dict[str, str] = {}
    dropped: set[str] = set()
    order = sorted(
        (i for i, r in enumerate(table.rows) if r.kind is RowKind.SUBTOTAL),
        key=lambda i: (table.rows[i].depth if table.rows[i].depth is not None else 0),
    )

    for i in order:
        row = table.rows[i]
        if row.norm_label not in mapped or row.norm_label in dropped:
            continue
        rollup = mapped[row.norm_label]
        kids = descendants(table, i)
        data_kids = [k for k in kids if k.kind is RowKind.DATA]

        clashing = [
            k
            for k in kids
            if k.norm_label in mapped
            and k.norm_label not in dropped
            # Opposite signs are the analyst's deliberate "rollup minus this
            # component" construction, which is not a double-count.
            and mapped[k.norm_label].sign == rollup.sign
        ]

        for kid in clashing:
            other = mapped[kid.norm_label]
            rep.conflicts += 1
            rep.findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="double_count",
                    message=(
                        f"{row.raw_label!r} -> {rollup.target.label!r} already "
                        f"includes {kid.raw_label!r}, which is separately "
                        f"mapped to {other.target.label!r}. Its value would "
                        f"reach the model twice. Map the rollup or the "
                        f"detail, or subtract the detail from the rollup."
                    ),
                    statement=ms.statement,
                    account=kid.raw_label,
                    detail={
                        "rollup": row.raw_label,
                        "rollup_target": rollup.target.label,
                        "detail_target": other.target.label,
                    },
                )
            )

        if clashing:
            complete = all(
                k.norm_label in mapped or k.norm_label in excluded for k in data_kids
            )
            if complete:
                dropped.add(row.norm_label)  # detail covers the group; drop the rollup
                continue
            for kid in clashing:
                dropped.add(kid.norm_label)  # rollup is the only complete cover

        for kid in data_kids:
            if kid.norm_label in mapped and kid.norm_label not in dropped:
                continue
            if kid.norm_label not in excluded:
                covered_by[kid.norm_label] = (
                    f"already included in '{row.raw_label}', which is mapped"
                )

    # Retire the losing side of every conflict. Leaving the rule in place is
    # what let the money be written twice; converting it to an exclusion keeps
    # it visible and reversible in the review sheet instead.
    if dropped:
        for rule in [r for r in ms.rules if r.norm_account in dropped]:
            crow = table.find(rule.norm_account)
            via = (
                "its own components"
                if crow is not None and crow.kind is RowKind.SUBTOTAL
                else "a mapped rollup that contains it"
            )
            ms.rules.remove(rule)
            ms.exclusions.append(
                Exclusion(
                    client_account=rule.client_account,
                    norm_account=rule.norm_account,
                    reason=(
                        f"would double-count -- already reached through {via}. "
                        f"Was mapped to {rule.target.label!r}."
                    ),
                    decided_by=Decider.STRUCTURE,
                    decided_at=now,
                )
            )
        mapped = {
            r.norm_account: r
            for r in ms.rules
            if r.decided_by is not Decider.UNRESOLVED
        }
        excluded = {e.norm_account for e in ms.exclusions}

    # The mirror image of case 1, and the one that was missing: a client
    # subtotal whose members are *all* accounted for is itself already in the
    # model, through them. Leaving it merely UNRESOLVED made the review sheet
    # ask the analyst to map `Total Current Assets` -- and doing as asked would
    # produce exactly the double-count this module exists to prevent. It has to
    # be an explicit, visible exclusion, not an open question.
    #
    # Same completeness test as above, so the two directions cannot disagree.
    # A subtotal with no mapped members at all is left alone: nothing covers it,
    # and `uncovered_accounts` below is the right report for that.
    for i, row in enumerate(table.rows) if cover_complete_rollups else []:
        if row.kind is not RowKind.SUBTOTAL:
            continue
        if row.norm_label in mapped or row.norm_label in excluded:
            continue
        data_kids = [k for k in descendants(table, i) if k.kind is RowKind.DATA]
        if not data_kids:
            continue
        accounted_kids = [
            k
            for k in data_kids
            if k.norm_label in mapped
            or k.norm_label in excluded
            or k.norm_label in covered_by
        ]
        if len(accounted_kids) != len(data_kids):
            continue
        if not any(k.norm_label in mapped for k in data_kids):
            continue  # nothing actually reaches the model; not covered, just empty
        covered_by[row.norm_label] = (
            "already in the model through its own components, which are mapped "
            "individually -- mapping this total as well would double-count them"
        )

    # An excluded account must not also appear as an open question: the review
    # sheet lists rules and exclusions side by side, so leaving the rule behind
    # would show the same account twice, once answered and once not.
    for rule in [
        r
        for r in ms.rules
        if r.norm_account in covered_by and r.decided_by is Decider.UNRESOLVED
    ]:
        ms.rules.remove(rule)

    # One exclusion per account, whatever route reached it. Commercial
    # Flooring prints `Accounts Payable`, `Retainage` and `Visa` at two nesting
    # depths, so an account can be covered by a nested rollup *and* by the
    # grand total above it and was being excluded once per route -- three
    # entries for `Accounts Payable`. Duplicates do not change the arithmetic
    # but they pad the review sheet with rows the analyst must read twice.
    already = {e.norm_account for e in ms.exclusions}
    for norm, reason in covered_by.items():
        if norm in already:
            continue
        already.add(norm)
        crow = table.find(norm)
        ms.exclusions.append(
            Exclusion(
                client_account=crow.raw_label if crow else norm,
                norm_account=norm,
                reason=reason,
                # A structural deduction from the client's own hierarchy, not a
                # name match and not a judgement call.
                decided_by=Decider.STRUCTURE,
                decided_at=now,
            )
        )
        rep.covered += 1

    # Anything left: a real account whose value reaches nothing.
    accounted = set(mapped) | set(covered_by) | excluded
    for row in table.rows:
        if row.kind is RowKind.DATA and row.norm_label not in accounted:
            rep.uncovered.append(row.raw_label)

    if rep.uncovered:
        rep.findings.append(
            Finding(
                severity=Severity.WARNING,
                code="uncovered_accounts",
                message=(
                    f"{len(rep.uncovered)} account(s) are neither mapped, covered by "
                    f"a mapped rollup, nor excluded: "
                    f"{', '.join(rep.uncovered[:6])}"
                    f"{'...' if len(rep.uncovered) > 6 else ''}"
                ),
                statement=ms.statement,
                detail={"accounts": rep.uncovered},
            )
        )
    return rep


def coverage(ms: MappingSet, table: ConsolidatedTable) -> tuple[int, int]:
    """(accounted, total) over DATA rows -- the metric that actually means something.

    Counting "rows with a rule" understates the income statement badly: mapping
    `Total Expense -> Operating Expenses` accounts for thirty expense rows
    without giving any of them a rule of its own, and that is the *correct*
    treatment (PLAN 7.0b), not a gap.
    """
    mapped = {r.norm_account for r in ms.rules if r.decided_by is not Decider.UNRESOLVED}
    excluded = {e.norm_account for e in ms.exclusions}
    data = [r for r in table.rows if r.kind is RowKind.DATA]
    done = sum(1 for r in data if r.norm_label in mapped or r.norm_label in excluded)
    return done, len(data)
