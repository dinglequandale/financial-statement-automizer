"""Arithmetic verification of a mapping: the LLM proposes, arithmetic disposes.

PLAN 7.3 describes this check and the rest of the design leans on it -- 7.0b
calls the double-count rule "mechanically checkable", 7.1 tiers the review by
"whether 7.3 verification passed", and every score report prints "cross-block
(arithmetic verification CATCHES these)". It was never built. `MappingRule.
verified` was declared, persisted, read by the review sheet's risk sort, and
left `None` on every rule of every run.

This module builds it. The whole thing rests on one asset we already have and
have already validated: **the client's own statement carries subtotals that
tie** (141/141 on Commercial Flooring, every balance sheet balancing). That
gives a free, judgement-free answer key for placement -- not for naming, and
not for the analyst's deliberate reclassifications, but for the question that
actually moves a valuation: did the money land in the right group?

Three checks, in increasing order of how much they catch:

1. `classify_client_rows` -- which client subtotals are even mappable. A row
   that does not equal the sum of its own constituents is a *computed result*
   (`Gross Margin`, `EBITDA`, `Net Ordinary Income`); a row whose constituents
   are all themselves subtotals is a *grand total* (`Total Assets`). Neither is
   ever a mapping source, and both were being shown to the analyst as decisions
   to make. This is arithmetic, not a list of names, so it transfers.

2. `verify_placement` -- reconstruct each template block from the mapping and
   compare it against the client's own equivalent subtotal. A mismatch means an
   account crossed a boundary the client and the template agree on. This is the
   check that catches `Inventory Receipts - Clearing Account`: the client files
   it under Current Liabilities, we mapped it to Inventory, and the
   reconstructed Total Current Liabilities comes up short by exactly its value.

3. `audit_exclusions` -- the largest unmeasured surface in the pipeline. On
   Commercial Flooring 43 accounts carrying 58% of the statement are excluded
   on the grounds that a mapped rollup already covers them. Nothing checked
   that claim. If the rollup is not mapped, or does not actually contain the
   excluded account, the money silently disappears.

**What this deliberately does not do.** It never *fixes* a mapping. An analyst
reclassifying an account against the client's own presentation is legitimate
and common (PLAN 2.5), so a mismatch is a question, not a verdict: it sets
`verified = False`, which floats the row to the top of the review sheet in red.
Silence is the only thing it rules out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fsa.mapping.reconcile import descendants
from fsa.model.mapping import Decider, Exclusion, MappingSet
from fsa.model.schema import (
    ConsolidatedRow,
    ConsolidatedTable,
    Finding,
    RowKind,
    Severity,
    StatementType,
)

#: Dollars. The client's own subtotals tie exactly, so anything above rounding
#: noise is a real discrepancy.
TOL = 1.0

#: A row kind, as far as *mapping* is concerned.
GROUP = "group"  # a real sum-rollup over accounts: a legitimate mapping source
COMPUTED = "computed"  # a derived result (Gross Margin, EBITDA): never a source
GRAND_TOTAL = "grand_total"  # a sum over other subtotals: never a source


@dataclass
class VerifyReport:
    findings: list[Finding] = field(default_factory=list)
    checked_blocks: int = 0
    failed_blocks: int = 0
    flagged_rules: int = 0
    verified_rules: int = 0
    suppressed_rows: int = 0
    unsafe_exclusions: int = 0

    @property
    def ok(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.findings)


# ---------------------------------------------------------------------------
# 1. which client rows are mappable at all
# ---------------------------------------------------------------------------


def _constituents(table: ConsolidatedTable, idx: int) -> list[ConsolidatedRow]:
    """Immediate constituents of the subtotal at `idx`.

    Hierarchical: when a descendant is itself a subtotal we take its value and
    skip the rows it already covers, which is what stops a nested group being
    counted twice. Flooring's `Total Equity` only ties this way -- three data
    rows plus `Net Income`, itself a computed row.
    """
    kids = descendants(table, idx)
    if not kids:
        return []
    covered: set[int] = set()
    order = {id(r): i for i, r in enumerate(table.rows)}
    out: list[ConsolidatedRow] = []
    for kid in sorted(kids, key=lambda r: order[id(r)], reverse=True):
        i = order[id(kid)]
        if i in covered:
            continue
        out.append(kid)
        if kid.kind is RowKind.SUBTOTAL:
            for sub in descendants(table, i):
                covered.add(order[id(sub)])
    return out


def _year_for(table: ConsolidatedTable) -> int | None:
    return max(table.years) if table.years else None


def _names(table: ConsolidatedTable, norms: list[str], limit: int = 6) -> str:
    """Display labels for a set of normalized keys.

    A finding that reports only a dollar gap makes the analyst hunt for the
    account; naming it is the difference between a warning and an instruction.
    """
    out = []
    for n in norms[:limit]:
        row = table.find(n)
        out.append(row.raw_label if row is not None else n)
    if len(norms) > limit:
        out.append(f"+{len(norms) - limit} more")
    return ", ".join(repr(x) for x in out)


def classify_client_rows(table: ConsolidatedTable) -> dict[str, str]:
    """`norm_label -> GROUP | COMPUTED | GRAND_TOTAL` for every subtotal row.

    Purely arithmetic, checked on every year the client sent rather than one,
    so a single odd year cannot flip a classification.
    """
    out: dict[str, str] = {}
    for i, row in enumerate(table.rows):
        if row.kind is not RowKind.SUBTOTAL:
            continue
        kids = _constituents(table, i)
        ties_any = False
        checked = False
        for yr in table.years:
            v = row.values.get(yr)
            if not isinstance(v, (int, float)):
                continue
            s = sum(
                k.values.get(yr) or 0.0
                for k in kids
                if isinstance(k.values.get(yr), (int, float))
            )
            checked = True
            if abs(s - v) <= TOL:
                ties_any = True
                break
        if not checked or not ties_any:
            out[row.norm_label] = COMPUTED
        elif kids and all(k.kind is RowKind.SUBTOTAL for k in kids):
            out[row.norm_label] = GRAND_TOTAL
        else:
            out[row.norm_label] = GROUP
    return out


def suppress_underivable(
    ms: MappingSet, table: ConsolidatedTable, spec, rep: VerifyReport
) -> None:
    """Take computed results and grand totals off the analyst's decision list.

    They are not accounts. The template derives `Gross Profit` and `Total
    Assets` itself, so mapping one is at best redundant and at worst an
    overwrite of a formula the model depends on (PLAN 2.3). They were showing
    up as RED rows -- 11 of TS's 43 -- each costing a from-scratch decision
    that has no right answer.

    They are marked, not deleted: the row still appears in review, pre-filled
    with its reason, so an analyst who disagrees can overtype it. A decision
    the analyst cannot see is a decision they cannot reverse (PLAN 7.3).
    """
    kinds = classify_client_rows(table)
    already = {e.norm_account for e in ms.exclusions}
    drop: set[str] = set()
    for rule in ms.rules:
        k = kinds.get(rule.norm_account)
        if k not in (COMPUTED, GRAND_TOTAL) or rule.norm_account in already:
            continue
        if rule.decided_by.is_confirmed:
            continue  # a human already said otherwise; never overrule that
        why = (
            "a computed result, not an account -- the template derives this"
            if k is COMPUTED
            else "a grand total over other subtotals -- the template derives this"
        )
        if rule.decided_by is Decider.UNRESOLVED:
            # Becomes a pre-filled exclusion rather than staying unresolved.
            # Left unresolved it is still a red row costing a from-scratch
            # decision that has no right answer -- which is the whole reason
            # these were worth detecting. As an exclusion it is visible,
            # explained, and reversible by overtyping (PLAN 7.3).
            ms.exclusions.append(
                Exclusion(
                    client_account=rule.client_account,
                    norm_account=rule.norm_account,
                    reason=why,
                    decided_by=Decider.STRUCTURE,
                )
            )
            drop.add(rule.norm_account)
            rep.suppressed_rows += 1
        else:
            # It was mapped. Collapsing a grand total onto the template's own
            # collapsible block subtotal is legal and in production use (PLAN
            # 7.0) -- Commercial Flooring's `Total Current Liabilities` is a
            # sum of three nested subtotals and maps straight onto the block.
            # Only a genuinely meaningless source, or a target that is not a
            # collapse point, is an error.
            line = spec.find(table.statement, rule.target.label)
            legal_collapse = (
                k is GRAND_TOTAL
                and line is not None
                and line.derived
                and line.collapsible
            )
            if legal_collapse:
                continue
            # Writing into a derived row desynchronises the model.
            rule.verified = False
            rule.rationale = f"{why} (proposed as {rule.target.label!r})"
            rep.flagged_rules += 1
            rep.findings.append(
                Finding(
                    code="mapped_derived_row",
                    severity=Severity.ERROR,
                    message=(
                        f"{table.statement.value}: {rule.client_account!r} is {why}, "
                        f"but was mapped to {rule.target.label!r}"
                    ),
                )
            )
    if drop:
        ms.rules[:] = [r for r in ms.rules if r.norm_account not in drop]


# ---------------------------------------------------------------------------
# 2. placement: reconstruct each block and compare against the client
# ---------------------------------------------------------------------------

#: Client subtotal -> template block. Only pairs whose meaning is fixed by
#: accounting rather than by house style, so this stays a structural fact and
#: not a per-client tuning knob. Anything unlisted is skipped, never guessed.
_BLOCK_SYNONYMS: dict[str, tuple[str, ...]] = {
    "Total Current Assets": ("total current assets",),
    "Total Current Liabilities": ("total current liabilities",),
    "Total Non-Current Liabilities": (
        "total long term liabilities",
        "total long-term liabilities",
        "total non current liabilities",
    ),
    "Revenue": ("total revenue", "total income", "total sales"),
    "Cost of Revenue": (
        "total cost of goods sold",
        "total cogs",
        "total cost of sales",
        "total cost of revenue",
    ),
}


def _align_blocks(
    spec, table: ConsolidatedTable, kinds: dict[str, str]
) -> list[tuple[str, ConsolidatedRow, int]]:
    """(template block label, client subtotal row, its index) for alignable pairs."""
    out = []
    by_norm = {}
    for i, row in enumerate(table.rows):
        if row.kind is RowKind.SUBTOTAL and kinds.get(row.norm_label) == GROUP:
            by_norm.setdefault(row.norm_label, (row, i))
    for block in spec.blocks:
        if block.statement is not table.statement:
            continue
        for syn in _BLOCK_SYNONYMS.get(block.subtotal_label, ()):
            hit = by_norm.get(syn)
            if hit is not None:
                out.append((block.subtotal_label, hit[0], hit[1]))
                break
    return out


def _target_block(spec, statement: StatementType, rule) -> str | None:
    """Where a rule's money lands. Read from the proposal, never from its label.

    For NAME_SLOT and INSERT the label is a name we invented and resolves to
    nothing; `Target.block` is authoritative.
    """
    if rule.target.block:
        return rule.target.block
    line = spec.find(statement, rule.target.label)
    if line is not None:
        if line.block:
            return line.block
        if line.derived and line.collapsible:
            return line.label  # collapsing onto a block subtotal is legal
    if rule.target.slot_label:
        slot = spec.find(statement, rule.target.slot_label)
        if slot is not None and slot.block:
            return slot.block
    return None


def verify_placement(
    ms: MappingSet, table: ConsolidatedTable, spec, rep: VerifyReport
) -> None:
    """Does each reconstructed template block match the client's own subtotal?

    The client's subtotals tie by construction, so any gap is ours. Attribution
    is by set difference, which points at the specific account rather than
    reporting a number the analyst then has to hunt down: accounts inside the
    client's group that landed outside the block, and accounts from outside
    that landed inside.
    """
    kinds = classify_client_rows(table)
    year = _year_for(table)
    if year is None:
        return
    rules = {r.norm_account: r for r in ms.rules if r.decided_by is not Decider.UNRESOLVED}
    excluded = {e.norm_account for e in ms.exclusions}

    for block_label, sub_row, idx in _align_blocks(spec, table, kinds):
        expected = sub_row.values.get(year)
        if not isinstance(expected, (int, float)):
            continue
        members = {
            k.norm_label for k in descendants(table, idx) if k.kind is RowKind.DATA
        }
        rep.checked_blocks += 1

        landed, actual = set(), 0.0
        for norm, rule in rules.items():
            if _target_block(spec, table.statement, rule) != block_label:
                continue
            row = table.find(norm)
            if row is None:
                continue
            v = row.values.get(year)
            if isinstance(v, (int, float)):
                actual += rule.sign * v
                landed.add(norm)

        if abs(actual - expected) <= max(TOL, abs(expected) * 1e-6):
            # Positive evidence, recorded for display -- and deliberately NOT
            # used to lower a row's review priority.
            #
            # That was tried and measured, and it failed. Demoting `verified is
            # True` rows out of the amber tier saved ~3 minutes on TS and moved
            # a real misplacement from flagged to unflagged: `Other Revenues`,
            # which the client files under revenue and the analyst deliberately
            # reclassifies into Other Income (PLAN 2.5). The block reconciled
            # precisely *because* we had reproduced the client's own
            # presentation.
            #
            # The general lesson, which limits this whole module: **agreeing
            # with the client's arithmetic is not the same as agreeing with the
            # analyst.** Every deliberate reclassification is a case where this
            # check endorses the answer the analyst will overrule, so it can
            # raise an alarm but must never lower one.
            #
            # `verified` stays tri-state: None means nobody checked, which is
            # not the same as passing.
            for norm in landed:
                rule = rules[norm]
                if rule.verified is None and not rule.decided_by.is_confirmed:
                    rule.verified = True
            rep.verified_rules += len(landed)
            continue

        rep.failed_blocks += 1

        def _moves(norm: str) -> bool:
            """Only name accounts that could actually explain the gap.

            Accounts the client dropped years ago (`Certificate of Deposit`,
            `Accounts Receivable - Lawler`) are still in the consolidated table
            with no value in the latest year, so they show up as strays while
            contributing nothing. Naming them buries the one row that matters.
            """
            row = table.find(norm)
            v = row.values.get(year) if row else None
            return isinstance(v, (int, float)) and abs(v) > TOL

        strayed = sorted(n for n in members - landed - excluded if _moves(n))
        intruded = sorted(n for n in landed - members if _moves(n))
        rep.findings.append(
            Finding(
                code="block_does_not_reconcile",
                severity=Severity.ERROR,
                message=(
                    f"{table.statement.value} {block_label}: rebuilt "
                    f"{actual:,.0f} against the client's {expected:,.0f} "
                    f"(off by {actual - expected:+,.0f})."
                    + (f" Left the group: {_names(table, strayed)}." if strayed else "")
                    + (f" Arrived from elsewhere: {_names(table, intruded)}."
                       if intruded else "")
                ),
            )
        )
        for norm in strayed + intruded:
            rule = rules.get(norm)
            if rule is None or rule.decided_by.is_confirmed:
                continue
            rule.verified = False
            rep.flagged_rules += 1


# ---------------------------------------------------------------------------
# 3. exclusions: the surface nobody was checking
# ---------------------------------------------------------------------------


def audit_exclusions(
    ms: MappingSet, table: ConsolidatedTable, spec, rep: VerifyReport
) -> None:
    """Every exclusion asserts the money is already in the model. Check it.

    `reconcile` excludes in two opposite directions and they need opposite
    proofs, which an earlier version of this check got wrong -- it tested only
    the first and reported all eight of TS's legitimate rollup exclusions as
    dropped money:

      * **a detail row under a mapped rollup** -- prove some *mapped rollup*
        arithmetically contains it;
      * **a rollup whose details were mapped instead** -- prove its own
        *constituents* are all accounted for, summing back to its value.

    Computed results and grand totals are always safe to exclude: the template
    derives them rather than reading them.

    On Commercial Flooring this covers 43 accounts and 58% of the statement,
    and until now nothing verified any of it.
    """
    year = _year_for(table)
    if year is None or not ms.exclusions:
        return
    kinds = classify_client_rows(table)
    mapped = {
        r.norm_account for r in ms.rules if r.decided_by is not Decider.UNRESOLVED
    }
    idx = {row.norm_label: i for i, row in enumerate(table.rows)}

    def _val(norm: str) -> float | None:
        row = table.find(norm)
        v = row.values.get(year) if row else None
        return v if isinstance(v, (int, float)) else None

    # Accounts genuinely reached through some mapped rollup that ties.
    #
    # Summed over `_constituents`, not raw `descendants`: a nested hierarchy
    # returns children *and* the subtotals already containing them, so the raw
    # sum double-counts and no rollup ever appeared to tie. That made this
    # check report Commercial Flooring's entire current-liabilities block --
    # $1.1M of Accounts Payable included -- as dropped money, when it is
    # correctly carried by a collapsed `Total Current Liabilities`.
    #
    # Coverage is transitive: once a rollup is vouched for, everything beneath
    # it at any depth is covered, not just its immediate constituents.
    covered: set[str] = set()
    for i, row in enumerate(table.rows):
        if row.kind is not RowKind.SUBTOTAL or row.norm_label not in mapped:
            continue
        v = _val(row.norm_label)
        s = sum(
            k.values.get(year) or 0.0
            for k in _constituents(table, i)
            if isinstance(k.values.get(year), (int, float))
        )
        if v is None or abs(s - v) > max(TOL, abs(v) * 1e-6):
            continue  # the rollup does not contain its members; cannot vouch
        covered.update(k.norm_label for k in descendants(table, i))

    for e in ms.exclusions:
        if e.decided_by is Decider.HUMAN:
            continue  # an analyst's own call is not ours to second-guess
        v = _val(e.norm_account)
        if v is None or abs(v) <= TOL:
            continue  # nothing to lose
        if kinds.get(e.norm_account) in (COMPUTED, GRAND_TOTAL):
            continue  # the template derives it; there is nothing to carry

        i = idx.get(e.norm_account)
        row = table.rows[i] if i is not None else None
        if row is not None and row.kind is RowKind.SUBTOTAL:
            # Excluded rollup: its own constituents must add back up to it.
            reached = sum(
                k.values.get(year) or 0.0
                for k in _constituents(table, i)
                if k.norm_label in mapped or k.norm_label in covered
                if isinstance(k.values.get(year), (int, float))
            )
            if abs(reached - v) <= max(TOL, abs(v) * 1e-6):
                continue
            gap, why = reached - v, "its components do not add back up to it"
        else:
            if e.norm_account in covered:
                continue
            gap, why = -v, "no mapped rollup arithmetically contains it"

        rep.unsafe_exclusions += 1
        rep.findings.append(
            Finding(
                code="exclusion_drops_money",
                severity=Severity.ERROR,
                message=(
                    f"{table.statement.value}: {e.client_account!r} ({v:,.0f}) "
                    f"was excluded, but {why} -- {abs(gap):,.0f} is currently "
                    f"dropped"
                ),
            )
        )


# ---------------------------------------------------------------------------


def verify_mapping(
    ms: MappingSet, table: ConsolidatedTable, spec
) -> VerifyReport:
    """Run every arithmetic check over one statement's mapping.

    Sets `MappingRule.verified = False` on anything contradicted, which the
    review sheet already reads: `_risk` sorts those into tier 0, red, above
    even the unresolved rows.
    """
    rep = VerifyReport()
    suppress_underivable(ms, table, spec, rep)
    verify_placement(ms, table, spec, rep)
    audit_exclusions(ms, table, spec, rep)
    return rep
