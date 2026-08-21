"""Deterministic mapping layers L0-L3 (PLAN.md 7.1).

`propose()` runs, per client account, in this order -- first confident hit
wins:

    L0  profile lookup   -- an exact prior decision for this norm_account,
                             replayed verbatim (target, sign preserved).
    L1  alias dictionary -- curated, cross-client (fsa.mapping.aliases).
    L2  structure         -- narrows L3's candidates to the template lines
                              whose block matches the client's own section.
                              Decides nothing by itself.
    L3  fuzzy match       -- similarity() against named target labels and
                              against L1's own keys. Accepts only >= 0.86.

Anything none of L0-L3 resolve comes back as a `MappingRule` with
`decided_by=Decider.UNRESOLVED` -- never dropped, never guessed. This module
deliberately never proposes `TargetKind.NAME_SLOT` or `TargetKind.INSERT`:
naming a placeholder slot or inventing a new template line requires either a
human or the LLM layer looking at the *set* of unmatched accounts together,
which a per-account deterministic pass structurally cannot do (PLAN.md 7.0).
Proposing one here would silently corrupt the eval this project is measured
against, so placeholder template lines (`TemplateLine.placeholder`) are never
offered as a candidate by any layer in this module.

On a brand-new client (empty profile) this is a cache with nothing cached
yet: expect most accounts to come back UNRESOLVED. That is the intended,
honest cold-start result -- see PLAN.md's "cold start vs steady state" table.
Only `RowKind.DATA` rows are ever mapped; `RowKind.SUBTOTAL` rows are skipped
entirely (never a rule, never an exclusion) per `fsa/model/schema.py`'s
`RowKind` docstring: mapping a subtotal would double-count its components.
"""

from __future__ import annotations

import re

from fsa.ingest.normalize import normalize, similar, similarity
from fsa.mapping.aliases import alias_keys, lookup_alias
from fsa.mapping.template import TemplateLine, TemplateSpec
from fsa.model.mapping import (
    ClientProfile,
    Decider,
    Exclusion,
    MappingRule,
    MappingSet,
    Target,
    TargetKind,
)
from fsa.model.schema import ConsolidatedTable, RowKind, StatementType

# L3 accepts only at or above this similarity. Below it, an account escalates
# to UNRESOLVED rather than being auto-accepted on a weak guess.
FUZZY_THRESHOLD = 0.86

PROFILE_CONFIDENCE = 1.0
ALIAS_CONFIDENCE = 0.95

# Sentinel label for an UNRESOLVED rule's Target. It is not a real template
# line -- MappingRule.target is a required field, so an unresolved account
# still needs *some* Target to carry, but `decided_by is Decider.UNRESOLVED`
# is the signal that this value must never be trusted or written anywhere.
UNRESOLVED_LABEL = "(unresolved)"

_ROLLUP_PREFIX_RE = re.compile(r"^(total|net)\s+")


def propose(
    table: ConsolidatedTable,
    spec: TemplateSpec,
    profile: ClientProfile | None = None,
) -> MappingSet:
    """Run L0->L3 over every mappable row of `table`, for one statement."""
    statement = table.statement
    out = MappingSet(statement=statement)

    # Legal ASSIGN candidates: real input/collapsible lines, never a
    # placeholder capacity slot (PLAN.md 7.0 -- those need a name, and naming
    # one is out of scope for every layer in this module).
    named_targets = [l for l in spec.targets(statement) if not l.placeholder]

    profile_set = profile.get(statement) if profile is not None else None

    for row in table.rows:
        norm = row.norm_label
        if not norm:
            continue

        # Client subtotals ARE legitimate mapping sources (PLAN.md 7.0b): four of
        # the sixty ground-truth labels map from one, and on the income statement
        # four rollups absorb ~90 detail accounts. But no deterministic layer may
        # decide that -- choosing between "map the rollup" and "map the detail" is
        # exactly the judgement the LLM and the analyst make, and an alias hit on
        # `Total Operating Expenses` would silently double-count against its own
        # components. So surface them as candidates and resolve them nowhere else.
        if row.kind is RowKind.SUBTOTAL:
            out.rules.append(
                MappingRule(
                    client_account=row.raw_label,
                    norm_account=norm,
                    target=Target(statement=statement, label=UNRESOLVED_LABEL),
                    decided_by=Decider.UNRESOLVED,
                    confidence=0.0,
                    rationale="client subtotal -- rollup vs detail is a judgement call",
                )
            )
            continue
        if row.kind is not RowKind.DATA:
            continue

        if profile_set is not None:
            rule = _from_profile(row.raw_label, norm, profile_set)
            if isinstance(rule, Exclusion):
                out.exclusions.append(rule)
                continue
            if rule is not None:
                out.rules.append(rule)
                continue

        rule = _from_alias(row.raw_label, norm, statement, spec)
        if rule is not None:
            out.rules.append(rule)
            continue

        candidates = _restrict_by_section(named_targets, row.section) or named_targets
        restricted = candidates is not named_targets
        rule = _from_fuzzy(row.raw_label, norm, statement, candidates, restricted)
        out.rules.append(rule)

    return out


def _from_profile(
    raw_label: str, norm: str, profile_set: MappingSet
) -> MappingRule | Exclusion | None:
    """L0: replay a prior, already-confirmed decision for this account."""
    for e in profile_set.exclusions:
        if e.norm_account == norm:
            return Exclusion(
                client_account=raw_label,
                norm_account=norm,
                reason=e.reason,
                decided_by=e.decided_by,
                decided_at=e.decided_at,
            )

    prior = profile_set.for_account(norm)
    if prior is None:
        return None
    return MappingRule(
        client_account=raw_label,
        norm_account=norm,
        target=prior.target,
        sign=prior.sign,
        confidence=PROFILE_CONFIDENCE,
        decided_by=Decider.PROFILE,
        rationale=prior.rationale or "replayed from client profile",
        decided_for_year=prior.decided_for_year,
    )


def _from_alias(
    raw_label: str, norm: str, statement: StatementType, spec: TemplateSpec
) -> MappingRule | None:
    """L1: curated cross-client dictionary, gated on the target actually existing."""
    hit = lookup_alias(norm, statement)
    if hit is None:
        return None
    tline = spec.find(statement, hit.target_label)
    if tline is None or not tline.is_target or tline.placeholder:
        # The dictionary named a line this template doesn't have (or that is
        # a placeholder here) -- fall through to structure/fuzzy instead of
        # inventing it.
        return None
    return MappingRule(
        client_account=raw_label,
        norm_account=norm,
        target=Target(
            statement=statement,
            label=tline.label,
            kind=TargetKind.ASSIGN,
            block=tline.block,
        ),
        sign=hit.sign,
        confidence=ALIAS_CONFIDENCE,
        decided_by=Decider.ALIAS,
        rationale=hit.rationale,
    )


def _section_matches_block(section: str, block_label: str) -> bool:
    """Is the client's own section the same block as `block_label`?

    Client sections and Weaver block-subtotal labels describe the same idea
    with different words ("Current Assets" vs "Total Current Assets"), so
    strip the roll-up prefix before comparing.
    """
    ns = normalize(section)
    nb = _ROLLUP_PREFIX_RE.sub("", normalize(block_label))
    if not ns or not nb:
        return False
    if ns == nb or ns in nb or nb in ns:
        return True
    return similar(ns, nb, 0.80)


def _restrict_by_section(
    target_lines: list[TemplateLine], section: str | None
) -> list[TemplateLine] | None:
    """L2: narrow candidates to the block matching the client's section.

    Returns None (meaning "no restriction available") when there is no
    section, or when nothing matches it -- the caller falls back to the full
    candidate set rather than starving L3 of candidates entirely.
    """
    if not section:
        return None
    matched = [l for l in target_lines if l.block and _section_matches_block(section, l.block)]
    return matched or None


def _fuzzy_pool(
    target_lines: list[TemplateLine], statement: StatementType
) -> list[tuple[str, TemplateLine, int]]:
    """Similarity anchors for L3: each target's own label, plus L1's keys.

    A key from the alias dictionary only counts if its target is among
    `target_lines` -- so a typo'd account can fuzzy-match an alias key
    ("Accrued Payrol" ~ "Accrued") and land on the same target the exact/
    prefix/contains check in L1 would have used had the spelling been right.
    """
    by_label = {l.label.casefold(): l for l in target_lines}
    pool: list[tuple[str, TemplateLine, int]] = [(l.label, l, 1) for l in target_lines]
    for key, target_label, sign in alias_keys(statement):
        tline = by_label.get(target_label.casefold())
        if tline is not None:
            pool.append((key, tline, sign))
    return pool


def _best_fuzzy(
    norm: str, pool: list[tuple[str, TemplateLine, int]]
) -> tuple[float, TemplateLine, int, str] | None:
    best: tuple[float, TemplateLine, int, str] | None = None
    for key, tline, sign in pool:
        score = similarity(norm, key)
        if best is None or score > best[0]:
            best = (score, tline, sign, key)
    return best


def _from_fuzzy(
    raw_label: str,
    norm: str,
    statement: StatementType,
    candidates: list[TemplateLine],
    restricted: bool,
) -> MappingRule:
    """L2 (restriction, already applied by the caller) + L3 (the decision)."""
    pool = _fuzzy_pool(candidates, statement)
    best = _best_fuzzy(norm, pool)

    if best is not None and best[0] >= FUZZY_THRESHOLD:
        score, tline, sign, key = best
        prefix = "structure+fuzzy" if restricted else "fuzzy"
        return MappingRule(
            client_account=raw_label,
            norm_account=norm,
            target=Target(
                statement=statement,
                label=tline.label,
                kind=TargetKind.ASSIGN,
                block=tline.block,
            ),
            sign=sign,
            confidence=score,
            decided_by=Decider.FUZZY,
            rationale=f"{prefix}: {score:.2f} similarity vs {key!r}",
        )

    if best is not None:
        note = f"no match >= {FUZZY_THRESHOLD:.2f} (best: {best[3]!r} at {best[0]:.2f})"
    else:
        note = "no candidate targets in scope"
    return MappingRule(
        client_account=raw_label,
        norm_account=norm,
        target=Target(statement=statement, label=UNRESOLVED_LABEL, kind=TargetKind.ASSIGN),
        sign=1,
        confidence=0.0,
        decided_by=Decider.UNRESOLVED,
        rationale=note,
    )
