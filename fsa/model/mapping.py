"""The mapping data model.

Shaped by three findings measured against the delivered TS model (PLAN.md 7.0):

  1. A mapping is `(account, target, sign)` -- not a pair. Three of the sixty
     ground-truth rules are contra items.
  2. A target may not exist yet. 15 of the 33 lines the analyst used are absent
     from the blank template, so the engine must be able to name a placeholder
     slot or insert a line, not only pick an existing one.
  3. Decisions are year-scoped. `Goodwill (Net)` included the exclusivity
     contract through FY2024 and excluded it in FY2025.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from fsa.model.schema import StatementType


class TargetKind(str, Enum):
    """How a proposal reaches its target line."""

    ASSIGN = "assign"  # an existing, named template line
    NAME_SLOT = "name_slot"  # rename an unnamed placeholder slot
    INSERT = "insert"  # add a new line into a named block


class Decider(str, Enum):
    """Provenance of a rule. Ordered loosely by trust."""

    HUMAN = "human"
    PROFILE = "profile"  # a prior human/confirmed decision, replayed
    ALIAS = "alias"  # curated cross-client dictionary
    STRUCTURE = "structure"  # section/sign inference
    FUZZY = "fuzzy"
    LLM = "llm"
    UNRESOLVED = "unresolved"

    @property
    def is_confirmed(self) -> bool:
        """Has a human signed off on this, directly or by prior confirmation?"""
        return self in (Decider.HUMAN, Decider.PROFILE)


@dataclass(frozen=True)
class Target:
    """Where a client account's value lands on a standardized tab.

    For ASSIGN, `label` names an existing template line. For NAME_SLOT it is
    the new name for `slot_label`. For INSERT it is the new line's name and
    `block` says which subtotal it must roll into -- without a block an
    inserted line has no defined position and cannot be written safely.
    """

    statement: StatementType
    label: str
    kind: TargetKind = TargetKind.ASSIGN
    block: str | None = None
    slot_label: str | None = None  # NAME_SLOT: the placeholder being named

    def __post_init__(self) -> None:
        if self.kind is TargetKind.NAME_SLOT and not self.slot_label:
            raise ValueError(f"NAME_SLOT target {self.label!r} needs slot_label")
        if self.kind is TargetKind.INSERT and not self.block:
            raise ValueError(f"INSERT target {self.label!r} needs a block")

    @property
    def key(self) -> str:
        return f"{self.statement.value}::{self.label}"


@dataclass
class MappingRule:
    """One client account -> one template line, with sign and provenance.

    `sign` is explicit and never inferred at write time: the delivered model
    subtracts `Taxes - State Income/Franchise` and `Penalties/Fines` inside
    Other Income, and negates `Interest Expense`.
    """

    client_account: str  # raw label, as the client wrote it
    norm_account: str  # normalized matching key
    target: Target
    sign: int = 1
    confidence: float = 1.0
    decided_by: Decider = Decider.UNRESOLVED
    rationale: str | None = None
    decided_at: datetime | None = None
    decided_for_year: int | None = None  # see finding 3
    verified: bool | None = None  # set by arithmetic verification

    def __post_init__(self) -> None:
        if self.sign not in (1, -1):
            raise ValueError(f"sign must be +1 or -1, got {self.sign!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence out of range: {self.confidence!r}")

    @property
    def needs_review(self) -> bool:
        """Anything not human-confirmed, or arithmetically contradicted."""
        return self.verified is False or not self.decided_by.is_confirmed


@dataclass
class Exclusion:
    """An account deliberately not mapped, with a reason.

    Every non-subtotal source account must end as a rule or an exclusion --
    silence is never allowed, because a silently dropped account is exactly the
    failure mode that makes totals stop tying.
    """

    client_account: str
    norm_account: str
    reason: str
    decided_by: Decider = Decider.HUMAN
    decided_at: datetime | None = None


@dataclass
class MappingSet:
    """Every decision for one client, on one statement."""

    statement: StatementType
    rules: list[MappingRule] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)

    def by_target(self) -> dict[str, list[MappingRule]]:
        out: dict[str, list[MappingRule]] = {}
        for r in self.rules:
            out.setdefault(r.target.key, []).append(r)
        return out

    def for_account(self, norm_account: str) -> MappingRule | None:
        for r in self.rules:
            if r.norm_account == norm_account:
                return r
        return None

    def covered(self) -> set[str]:
        return {r.norm_account for r in self.rules} | {
            e.norm_account for e in self.exclusions
        }

    @property
    def unresolved(self) -> list[MappingRule]:
        return [r for r in self.rules if r.decided_by is Decider.UNRESOLVED]

    @property
    def review_queue(self) -> list[MappingRule]:
        """Lowest confidence first -- the analyst's worklist."""
        return sorted(
            (r for r in self.rules if r.needs_review),
            key=lambda r: (r.verified is False, r.confidence),
        )


@dataclass
class ClientProfile:
    """Persisted, reusable mapping decisions for one client.

    This is the cache that makes year 2 free. Git-tracked YAML so decisions are
    diffable and attributable across analysts.
    """

    client_name: str
    template_source: str | None = None
    created_at: date | None = None
    updated_at: date | None = None
    sets: dict[str, MappingSet] = field(default_factory=dict)  # statement -> set
    notes: list[str] = field(default_factory=list)

    def get(self, statement: StatementType) -> MappingSet:
        return self.sets.setdefault(statement.value, MappingSet(statement=statement))

    def lookup(self, statement: StatementType, norm_account: str) -> MappingRule | None:
        return self.get(statement).for_account(norm_account)
