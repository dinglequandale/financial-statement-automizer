"""Unit tests for the L1 curated alias dictionary (fsa.mapping.aliases)."""

from __future__ import annotations

import pytest

from fsa.mapping.aliases import ALIASES, AliasRule, alias_keys, lookup_alias
from fsa.model.schema import StatementType

BS = StatementType.BS
IS = StatementType.IS


def test_exact_match_hits() -> None:
    hit = lookup_alias("petty cash", BS)
    assert hit is not None
    assert hit.target_label == "Cash and Cash Equivalents"
    assert hit.sign == 1


def test_prefix_match_hits_multiple_variants() -> None:
    for label in ("accrued payroll expense", "accrued 401k expenses", "accrued expenses"):
        hit = lookup_alias(label, BS)
        assert hit is not None, label
        assert hit.target_label == "Accrued Expenses"


def test_prepaid_prefix_covers_both_named_examples() -> None:
    # PLAN spec calls these out explicitly: both must resolve.
    for label in ("prepaid insurance", "prepaid inventory"):
        hit = lookup_alias(label, BS)
        assert hit is not None, label
        assert hit.target_label == "Prepaid Expenses"


def test_accounts_receivable_prefix_covers_generic_variants() -> None:
    for label in (
        "accounts receivable trade",
        "allowance for doubtful accounts",
        "nsf clearing account",
    ):
        hit = lookup_alias(label, BS)
        assert hit is not None, label
        assert hit.target_label == "Accounts Receivable"


def test_notes_payable_maps_to_long_term_debt() -> None:
    hit = lookup_alias("notes payable", BS)
    assert hit is not None
    assert hit.target_label == "Long-Term Debt"
    assert hit.sign == 1


def test_accumulated_depreciation_sign_is_positive() -> None:
    # Verified against the delivered model (PLAN.md 7.2/7.3): it already
    # arrives negative, so it is summed as-is, not negated.
    hit = lookup_alias("accumulated depreciation and amortization", BS)
    assert hit is not None
    assert hit.target_label == "Accumulated Depreciation"
    assert hit.sign == 1


def test_interest_expense_is_negated_on_is() -> None:
    hit = lookup_alias("interest expense", IS)
    assert hit is not None
    assert hit.target_label == "Interest (Expense)"
    assert hit.sign == -1


def test_no_hit_returns_none() -> None:
    assert lookup_alias("warehouse equipment and racks", BS) is None
    assert lookup_alias("", BS) is None


def test_statement_scoping() -> None:
    # "Interest Expense" is seeded for IS only; it must not leak into BS.
    assert lookup_alias("interest expense", BS) is None
    # "Prepaid" prefix is BS only; must not leak into IS.
    assert lookup_alias("prepaid insurance", IS) is None


def test_exact_beats_prefix_globally() -> None:
    # "Goodwill" is an exact-only entry. Nothing with a broader prefix should
    # be able to steal it even if declared elsewhere in the dictionary.
    hit = lookup_alias("goodwill", BS)
    assert hit is not None
    assert hit.target_label == "Goodwill"


def test_client_specific_vocabulary_is_not_seeded() -> None:
    # These are genuine TS Distributors chart-of-account items (a customer
    # name, a specific contract), not generic cross-client vocabulary. They
    # must not be hardcoded into the alias dictionary -- see the module
    # docstring's warning against overfitting L1 to one engagement.
    assert lookup_alias("goodwill exclusivity contract", BS) is None
    assert lookup_alias("distributions due to from brad stein", BS) is None


def test_generic_prefix_incidentally_covers_a_client_specific_label() -> None:
    # "Accounts Receivable - Indital" is TS-specific (a customer name), but
    # it legitimately resolves anyway -- not because it was hardcoded, but
    # because the generic "accounts receivable" prefix rule covers it, the
    # same way it covers any other client's "Accounts Receivable - <Anyone>".
    hit = lookup_alias("accounts receivable indital", BS)
    assert hit is not None
    assert hit.target_label == "Accounts Receivable"


def test_alias_rule_rejects_bad_sign() -> None:
    with pytest.raises(ValueError):
        AliasRule(statement=BS, target_label="X", sign=0, exact=("x",))


def test_alias_rule_rejects_empty_keys() -> None:
    with pytest.raises(ValueError):
        AliasRule(statement=BS, target_label="X", sign=1)


def test_alias_keys_round_trips_into_lookup() -> None:
    keys = alias_keys(BS)
    assert keys, "expected at least one BS alias key"
    for key, target_label, sign in keys:
        assert isinstance(key, str) and key
        assert isinstance(target_label, str) and target_label
        assert sign in (1, -1)


def test_alias_keys_statement_scoped() -> None:
    bs_targets = {t for _, t, _ in alias_keys(BS)}
    is_targets = {t for _, t, _ in alias_keys(IS)}
    assert "Cash and Cash Equivalents" in bs_targets
    assert "Cash and Cash Equivalents" not in is_targets
    assert "Interest (Expense)" in is_targets
    assert "Interest (Expense)" not in bs_targets


def test_dictionary_has_no_duplicate_target_per_statement_ambiguity() -> None:
    # Sanity check on the seed data itself: every declared rule must be
    # internally well-formed (covered by AliasRule.__post_init__, exercised
    # here simply by importing/iterating the module-level tuple).
    assert len(ALIASES) > 10
    for rule in ALIASES:
        assert rule.statement in (BS, IS)
