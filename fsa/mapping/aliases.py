"""L1: a curated, cross-client alias dictionary.

This is deliberately small and conservative. Every entry here is a piece of
*generic* accounting vocabulary that means the same thing at (almost) any
client -- "Petty Cash" is cash everywhere, "Accrued Payroll" is an accrued
expense everywhere. It is NOT a place to encode one client's chart of
accounts: entries specific to TS Distributors (a customer named "Indital", a
line called "Goodwill - Exclusivity Contract") do not belong here even though
they appear in the TS ground truth -- putting them here would make the L1
layer memorize one engagement instead of generalizing across clients, which
defeats the entire point of a "curated cross-client" dictionary and would
quietly inflate the cold-start numbers on the one client we happen to have
data for.

Matching is on `normalize()`d labels only -- never on raw text -- so this
dictionary stays independent of capitalization, punctuation and the
"&" vs "and" style choices `fsa.ingest.normalize` already absorbs.

Three match modes, tried in this priority order across the *whole*
dictionary (an exact hit anywhere outranks a prefix hit anywhere, which
outranks a contains hit anywhere):

    exact    -- the normalized account label equals the key exactly.
    prefix   -- the normalized account label starts with the key
                ("Accrued " catches "Accrued Payroll Expense").
    contains -- the key appears anywhere in the normalized label.

`target_label` names a line on the standardized template by its *current*
label -- e.g. the blank BVAL template calls the deferred-revenue line
"Unearned Revenue", not "Deferred Revenue", so that is the label recorded
here. The matcher looks this label up against the template actually in use
and only fires the alias if a live, non-placeholder target line exists with
that name -- so an alias entry naming a line a particular client's template
lacks simply does not fire; it does not invent the line.

`sign` defaults to +1 (value flows into the target as reported). The only
two entries that override it encode findings verified against the delivered
TS model (PLAN.md 7.2/7.3): `Accumulated Depreciation` already arrives
negative, so it is summed as-is (sign +1); `Interest Expense` arrives
positive but the target line expects it negative, so it is negated
(sign -1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fsa.ingest.normalize import normalize
from fsa.model.schema import StatementType


@dataclass(frozen=True)
class AliasHit:
    """One L1 resolution: what target and sign the dictionary proposes."""

    target_label: str
    sign: int
    rationale: str


@dataclass(frozen=True)
class AliasRule:
    """One dictionary entry: a family of normalized keys -> one target."""

    statement: StatementType
    target_label: str
    sign: int = 1
    exact: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    contains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.sign not in (1, -1):
            raise ValueError(f"sign must be +1 or -1, got {self.sign!r}")
        if not (self.exact or self.prefixes or self.contains):
            raise ValueError(
                f"alias rule for {self.target_label!r} has no match keys at all"
            )


def _rule(
    statement: StatementType,
    target_label: str,
    *,
    sign: int = 1,
    exact: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
    contains: tuple[str, ...] = (),
) -> AliasRule:
    """Build a rule, normalizing every raw human-written key up front."""
    return AliasRule(
        statement=statement,
        target_label=target_label,
        sign=sign,
        exact=tuple(normalize(x) for x in exact),
        prefixes=tuple(normalize(x) for x in prefixes),
        contains=tuple(normalize(x) for x in contains),
    )


_BS = StatementType.BS
_IS = StatementType.IS

# Genuinely cross-client accounting vocabulary. Grouped by target for
# readability; order between groups does not matter -- lookup_alias() scans
# exact keys everywhere, then prefixes everywhere, then contains everywhere.
ALIASES: tuple[AliasRule, ...] = (
    # --- Balance sheet -----------------------------------------------
    _rule(
        _BS,
        "Cash and Cash Equivalents",
        exact=(
            "Cash",
            "Cash on Hand",
            "Cash in Bank",
            "Petty Cash",
            "Operating Account",
            "Checking Account",
            "Savings Account",
            "Cash and Cash Equivalents",
            "Cash Equivalents",
            "Cash - Operating",
        ),
    ),
    _rule(
        _BS,
        "Accounts Receivable",
        prefixes=("Accounts Receivable",),
        exact=(
            "Allowance for Doubtful Accounts",
            "NSF Clearing Account",
            "Trade Receivables",
            "Other Accounts Receivable",
            "Accounts Receivable, Net",
        ),
    ),
    _rule(
        _BS,
        "Inventory",
        prefixes=("Inventory",),
        exact=(
            "Merchandise Inventory",
            "Finished Goods Inventory",
            "Raw Materials Inventory",
            "Work in Process Inventory",
        ),
    ),
    _rule(_BS, "Prepaid Expenses", prefixes=("Prepaid",)),
    _rule(
        _BS,
        "Accounts Payable",
        prefixes=("Accounts Payable",),
        exact=("Trade Payables", "Vendor Payables", "A/P Trade"),
    ),
    _rule(
        _BS,
        "Other Payables",
        exact=(
            "Sales Tax Payable",
            "Property Tax Payable",
            "Corp Tax Payable",
            "Corporate Tax Payable",
            "Franchise Tax Payable",
            "Payroll Tax Payable",
            "Use Tax Payable",
        ),
    ),
    _rule(_BS, "Accrued Expenses", prefixes=("Accrued",)),
    _rule(
        _BS,
        "Long-Term Debt",
        prefixes=("Notes Payable", "Note Payable"),
        exact=("Long Term Debt",),
    ),
    _rule(_BS, "Unearned Revenue", exact=("Deferred Revenue", "Unearned Revenue")),
    _rule(
        _BS,
        "Accumulated Depreciation",
        sign=1,  # arrives negative already -- see module docstring.
        prefixes=("Accumulated Depreciation",),
    ),
    _rule(_BS, "Goodwill", exact=("Goodwill",)),
    _rule(
        _BS,
        "Total Equity",
        exact=(
            "Total Owners Equity",
            "Total Stockholders Equity",
            "Total Shareholders Equity",
            "Owners Equity",
            "Stockholders Equity",
            "Total Equity",
        ),
    ),
    _rule(
        _BS,
        "Furniture and Fixtures",
        exact=("Furniture and Fixtures", "Furniture & Fixtures"),
    ),
    _rule(
        _BS,
        "Land and Buildings",
        exact=("Land and Buildings", "Land & Buildings", "Real Estate"),
    ),
    _rule(
        _BS,
        "Right of Use Assets",
        exact=(
            "Right of Use Assets",
            "ROU Assets",
            "Operating Lease Right of Use Asset",
        ),
    ),
    _rule(_BS, "Investments", exact=("Investments", "Marketable Securities")),
    # --- Income statement ----------------------------------------------
    _rule(_IS, "Interest (Expense)", sign=-1, exact=("Interest Expense",)),
    _rule(_IS, "Interest Income", exact=("Interest Income",)),
    _rule(_IS, "Depreciation", prefixes=("Depreciation",)),
    _rule(_IS, "Amortization", prefixes=("Amortization",)),
    _rule(
        _IS,
        "Federal Income Taxes",
        exact=(
            "Income Tax Expense",
            "Provision for Income Taxes",
            "Federal Income Tax",
            "Federal Income Taxes",
            "Income Taxes",
        ),
    ),
    _rule(_IS, "Capital Expenditures", exact=("Capital Expenditures", "Capex")),
    _rule(
        _IS,
        "Distributions",
        exact=(
            "Distributions",
            "Owner Distributions",
            "Shareholder Distributions",
            "Member Distributions",
        ),
    ),
    _rule(
        _IS,
        "Revenue",
        exact=("Sales", "Sales Revenue", "Gross Sales", "Net Sales", "Revenue"),
    ),
    _rule(
        _IS,
        "Cost of Revenue",
        exact=(
            "Cost of Goods Sold",
            "Total Cost of Goods Sold",
            "COGS",
            "Cost of Sales",
            "Cost of Revenue",
        ),
    ),
    _rule(
        _IS,
        "Other Operating Expenses",
        exact=("Other Operating Expenses", "Miscellaneous Operating Expenses"),
    ),
)


def lookup_alias(norm_account: str, statement: StatementType) -> AliasHit | None:
    """L1: resolve one already-normalized account label via the dictionary.

    Priority is exact > prefix > contains, checked across the *whole*
    dictionary at each stage -- so an exact match on one target always beats
    a prefix or contains match on a different one, regardless of the order
    entries happen to be declared in above.
    """
    if not norm_account:
        return None

    rules = [r for r in ALIASES if r.statement is statement]

    for r in rules:
        if norm_account in r.exact:
            return AliasHit(
                target_label=r.target_label,
                sign=r.sign,
                rationale=f"alias: exact match -> {r.target_label!r}",
            )

    for r in rules:
        for p in r.prefixes:
            if norm_account.startswith(p):
                return AliasHit(
                    target_label=r.target_label,
                    sign=r.sign,
                    rationale=f"alias: prefix {p!r} -> {r.target_label!r}",
                )

    for r in rules:
        for c in r.contains:
            if c in norm_account:
                return AliasHit(
                    target_label=r.target_label,
                    sign=r.sign,
                    rationale=f"alias: contains {c!r} -> {r.target_label!r}",
                )

    return None


def alias_keys(statement: StatementType) -> list[tuple[str, str, int]]:
    """Every (normalized key, target_label, sign) triple, for L3 fuzzy.

    L3 matches a client account against target *labels* and against these
    alias keys, so a typo'd variant of an alias key ("Accrued Payrol") can
    still resolve via fuzzy similarity even though it misses the dictionary's
    own exact/prefix/contains checks.
    """
    out: list[tuple[str, str, int]] = []
    for r in ALIASES:
        if r.statement is not statement:
            continue
        for key in (*r.exact, *r.prefixes, *r.contains):
            out.append((key, r.target_label, r.sign))
    return out
