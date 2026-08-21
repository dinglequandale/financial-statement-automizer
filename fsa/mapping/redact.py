"""Pseudonymize personal names in account labels before they leave the machine.

PLAN.md 7.1 commits to sending account *names* only -- never amounts, never
client identity. Real charts of accounts violate that commitment on their own:
TS Distributors names its owners in twelve equity accounts
(`Paid in Capital - Brad Stein`, `Earnings - Gary Stein`, ...).

A naive "Title Case after a dash" rule is worse than nothing -- it also fires on
`Goodwill - Exclusivity Contract` and `Inventory - Capitalized Sec 236A`, and
destroying those suffixes destroys the mapping signal we are paying the model to
read.

The usable signal is structural rather than lexical: **a person's name attaches
to many different accounting stems; a descriptive suffix attaches to one.**
`Brad Stein` appears under Paid in Capital, Earnings, Distributions paid, and
Distributions - Due to (from); `Exclusivity Contract` appears only under
Goodwill. So we pseudonymize a suffix only when it recurs across at least
`MIN_STEMS` distinct stems.

Pseudonyms are stable within a run (`<PERSON_1>` is always the same person), so
the grouping signal the model needs survives while the identity does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# `Paid in Capital - Brad Stein` -> stem `Paid in Capital`, suffix `Brad Stein`.
# Also matches the doubled form `Distributions - Due to (from) - Brad Stein`,
# where we take the LAST dash-separated segment as the suffix.
_SEP = re.compile(r"\s+-\s+")

# A candidate personal name: two or more capitalized words, letters only.
_NAME_SHAPED = re.compile(r"^[A-Z][a-zA-Z']+(?:\s+[A-Z][a-zA-Z']+)+$")

# Suffixes that are name-shaped but are accounting vocabulary, not people.
_NOT_PEOPLE = frozenset(
    {
        "clearing account",
        "exclusivity contract",
        "damaged product",
        "computer equipment",
        "packing supplies",
        "beg balance variance",
        "allowed write off",
        "due to",
        "due from",
        "trade receivable",
        "accounts receivable",
        "line of credit",
        "cost of sales",
        "general admin",
        "general administrative",
        "opening balance",
        "retained earnings",
        "current portion",
        "long term",
        "short term",
    }
)

# How many distinct stems a suffix must attach to before we treat it as a person.
MIN_STEMS = 3


@dataclass
class Redaction:
    """The pseudonym map for one run, plus the labels it rewrote."""

    aliases: dict[str, str] = field(default_factory=dict)  # real -> <PERSON_n>
    rewritten: dict[str, str] = field(default_factory=dict)  # original -> redacted

    @property
    def person_count(self) -> int:
        return len(self.aliases)

    def restore(self, label: str) -> str:
        """Map a redacted label back to the original, for local display."""
        for original, red in self.rewritten.items():
            if red == label:
                return original
        return label


def _split(label: str) -> tuple[str, str] | None:
    parts = _SEP.split(label.strip())
    if len(parts) < 2:
        return None
    suffix = parts[-1].strip()
    stem = _SEP.split(label.strip())[0].strip()
    return stem, suffix


def _name_shaped(suffix: str) -> bool:
    if suffix.casefold() in _NOT_PEOPLE:
        return False
    return bool(_NAME_SHAPED.match(suffix))


def detect_people(labels: list[str]) -> set[str]:
    """Suffixes that recur across >= MIN_STEMS distinct stems and look like names."""
    stems_by_suffix: dict[str, set[str]] = {}
    for label in labels:
        split = _split(label)
        if not split:
            continue
        stem, suffix = split
        if not _name_shaped(suffix):
            continue
        stems_by_suffix.setdefault(suffix, set()).add(stem.casefold())
    return {s for s, stems in stems_by_suffix.items() if len(stems) >= MIN_STEMS}


def redact(labels: list[str]) -> Redaction:
    """Replace detected personal names with stable `<PERSON_n>` pseudonyms."""
    people = sorted(detect_people(labels))
    r = Redaction()
    for i, person in enumerate(people, start=1):
        r.aliases[person] = f"<PERSON_{i}>"

    if not people:
        return r

    for label in labels:
        out = label
        for person, alias in r.aliases.items():
            out = re.sub(rf"(?<![A-Za-z]){re.escape(person)}(?![A-Za-z])", alias, out)
        if out != label:
            r.rewritten[label] = out
    return r


def apply(label: str, r: Redaction) -> str:
    return r.rewritten.get(label, label)
