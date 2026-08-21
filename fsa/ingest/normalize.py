"""Account-label normalization.

Client charts of accounts drift in spelling but not in meaning. Observed in the
TS Distributors sample set:

    "Accumulated Depreciation & Amortization"  (client file, every year)
    "Accumulated Depreciation & Amort."        (Weaver's consolidation)

Both must normalize to the same key. Genuine typos ("Other Paybles" vs
"Other Payables") are NOT handled here -- normalization stays deterministic and
lossless-in-meaning; typo tolerance is the fuzzy matcher's job. Use `similar()`
for that.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Slash forms are expanded before punctuation is stripped, otherwise "a/r"
# collapses to the meaningless token "ar".
_SLASH_FORMS: list[tuple[str, str]] = [
    (r"\ba\s*/\s*r\b", "accounts receivable"),
    (r"\ba\s*/\s*p\b", "accounts payable"),
    (r"\bn\s*/\s*r\b", "notes receivable"),
    (r"\bn\s*/\s*p\b", "notes payable"),
    (r"\br\s*/\s*e\b", "retained earnings"),
    (r"\bw\s*/\s*o\b", "write off"),
    (r"\bp\s*&\s*l\b", "profit and loss"),
]

# Applied per-token, so "amort" never matches inside "amortization".
_ABBREVIATIONS: dict[str, str] = {
    "amort": "amortization",
    "amortiz": "amortization",
    "dep": "depreciation",
    "depr": "depreciation",
    "deprec": "depreciation",
    "accum": "accumulated",
    "acct": "account",
    "accts": "accounts",
    "exp": "expense",
    "exps": "expenses",
    "equip": "equipment",
    "mach": "machinery",
    "furn": "furniture",
    "recv": "receivable",
    "receiv": "receivable",
    "pmt": "payment",
    "pymt": "payment",
    "liab": "liability",
    "liabs": "liabilities",
    "misc": "miscellaneous",
    "ppd": "prepaid",
    "prepd": "prepaid",
    "ins": "insurance",
    "adj": "adjustment",
    "adjs": "adjustments",
    "bal": "balance",
    "beg": "beginning",
    "int": "interest",
    "lt": "long term",
    "cogs": "cost of goods sold",
    "ppe": "property plant and equipment",
    "tot": "total",
    "inv": "inventory",
    "sec": "section",
    "yr": "year",
    "yrs": "years",
}

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def normalize(label: str | None) -> str:
    """Reduce a client account label to a stable matching key.

    Idempotent: normalize(normalize(x)) == normalize(x).
    """
    if label is None:
        return ""

    s = unicodedata.normalize("NFKD", str(label))
    s = s.casefold().strip()
    if not s:
        return ""

    # Typographic characters -> ASCII equivalents.
    for ch in ("‘", "’", "“", "”"):
        s = s.replace(ch, "'")
    for ch in ("–", "—", "−"):
        s = s.replace(ch, "-")
    s = s.replace(" ", " ")

    for pattern, repl in _SLASH_FORMS:
        s = re.sub(pattern, repl, s)

    s = s.replace("&", " and ")
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if not s:
        return ""

    tokens = [_ABBREVIATIONS.get(t, t) for t in s.split(" ")]
    return " ".join(tokens)


def similar(a: str | None, b: str | None, threshold: float = 0.90) -> bool:
    """True when two labels are close enough to be the same account.

    Operates on normalized forms. Intended for typo tolerance
    ("other paybles" ~ "other payables"), not for semantic matching.
    """
    return similarity(a, b) >= threshold


def similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


# Derived lines that are computed from other lines rather than being accounts in
# their own right. They must not be treated as DATA: summing them double-counts.
# Observed in the sample IS, where "EBITDA" (= Total Operating Profit + Total
# Other Income) sits between two blocks with no "Total" in its name.
_DERIVED_LINES: frozenset[str] = frozenset(
    {
        "ebitda",
        "ebit",
        "ebt",
        "operating profit",
        "operating income",
        "operating profit loss",
        "profit before taxes",
        "profit before tax",
        "pretax income",
        "pre tax income",
        "income before taxes",
        "income before income taxes",
    }
)

_DERIVED_PREFIXES: tuple[str, ...] = (
    "net ",
    "gross ",
    "ebitda",
    "ebit ",
)


def is_total_label(label: str | None) -> bool:
    """Heuristic: is this row a subtotal or a derived line rather than an account?

    Covers both explicit subtotals ("Total Current Assets") and derived lines
    that carry no "Total" in their name ("EBITDA", "Gross Margin"). Both must be
    excluded from component sums, and neither is eligible for mapping.

    Deliberately conservative -- callers cross-check against whether the value
    actually equals the sum of the preceding block.
    """
    n = normalize(label)
    if not n:
        return False
    if n in _DERIVED_LINES:
        return True
    return (
        n.startswith("total ")
        or n == "total"
        or n.startswith(_DERIVED_PREFIXES)
        or " total" in n
    )


def is_derived_line(label: str | None) -> bool:
    """Is this a computed line rather than the total of the block above it?

    Both derived lines and block subtotals are non-mappable, but only block
    subtotals can be validated as "sum of the preceding DATA rows". A derived
    line combines earlier subtotals, so checking it against whatever block
    happens to sit above it produces a false positive.

    The sample IS is exactly this shape: "EBITDA" follows the Other Income
    block, but equals Total Operating Profit + Total Other Income, not the
    Other Income block alone.

    Note "Total ..." labels are deliberately NOT derived -- most are genuine
    block subtotals. Grand totals like "Total Assets" are filtered out by the
    caller instead, on the grounds that no DATA rows precede them.
    """
    n = normalize(label)
    if not n:
        return False
    return n in _DERIVED_LINES or n.startswith(_DERIVED_PREFIXES)
