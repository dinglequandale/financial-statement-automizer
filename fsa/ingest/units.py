"""What the figures are denominated in: scale and currency.

Two more members of a column's identity, added *before* a client forced them.

Every previous dimension in this data model arrived the same way: a client
broke something, we discovered the field was missing, we added it. That
sequence works but it costs a client each time, and it means the tool is always
one engagement behind. Scale and currency are the last two members of the tuple
that can be named from the domain rather than from a sample, so they are being
added on the strength of the domain alone:

* **Scale.** Audited statements routinely present figures in thousands, and say
  so once, in small type, above the columns. Nothing else in the statement
  betrays it -- every subtotal still ties, every year still balances, and the
  arithmetic gate in `fsa.validate.checks` passes cleanly. A thousands-stated
  statement written into a units-stated model is a valuation wrong by three
  orders of magnitude, arrived at silently. None of the four sample clients
  declares a scale; the fifth might, and audited engagements generally will.

* **Currency.** A group with a foreign subsidiary reports somewhere. AOK
  Holdings' Canadian tab carries the header `YTD (USD)` -- that client had
  already translated, but the header exists precisely because the question does.
  Adding CAD figures to USD ones ties every subtotal too.

The response to both is refusal, not support. This tool writes figures as the
client reported them; it does not rescale and it does not translate. So the job
here is to notice a denomination we cannot honour and stop, which costs an
analyst one question, rather than to guess and cost them a valuation.
"""

from __future__ import annotations

import re

#: `(in thousands)`, `$ in thousands`, `(000's)`, `amounts in thousands of
#: dollars`, `dollars in millions`. Deliberately anchored on the number word:
#: a row *called* "Thousands Island Dressing" is not a scale declaration.
_SCALE_PATTERNS: list[tuple[re.Pattern, int, str]] = [
    (re.compile(r"\bin\s+thousands\b|\bthousands\s+of\s+(u\.?s\.?\s+)?dollars\b"
                r"|\bdollars?\s+in\s+thousands\b|\(\s*0{3}(?:'s)?\s*\)"
                r"|\$\s*0{3}\b", re.I), 1_000, "thousands"),
    (re.compile(r"\bin\s+millions\b|\bmillions\s+of\s+(u\.?s\.?\s+)?dollars\b"
                r"|\bdollars?\s+in\s+millions\b|\(\s*0{6}(?:'s)?\s*\)", re.I),
     1_000_000, "millions"),
]

#: An explicit currency marker. Three-letter codes only where they stand alone,
#: so an account called `CADENCE SOFTWARE` is not read as Canadian dollars.
_CURRENCY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bcanadian\s+dollars?\b|\bCAD\b|\bCDN\b|\bC\$", re.I), "CAD"),
    (re.compile(r"\bu\.?s\.?\s+dollars?\b|\bUSD\b|\bUS\$", re.I), "USD"),
    (re.compile(r"\beuros?\b|\bEUR\b|€", re.I), "EUR"),
    (re.compile(r"\bpounds?\s+sterling\b|\bGBP\b", re.I), "GBP"),
    (re.compile(r"\bmexican\s+pesos?\b|\bMXN\b", re.I), "MXN"),
]

#: Only the masthead of a statement declares its denomination. Scanning the
#: whole body would read every account label as a candidate and turn a client
#: with a `Euro Truck Lease` line into a European reporter.
HEAD_ROWS = 12


def detect_scale(texts) -> tuple[int, str | None]:
    """Return `(multiplier, label)`; `(1, None)` when nothing is declared.

    `1` means "as reported", which is both the overwhelming default and the
    only denomination this tool can write without rescaling.
    """
    for text in texts:
        if not text:
            continue
        for pattern, multiplier, label in _SCALE_PATTERNS:
            if pattern.search(str(text)):
                return multiplier, label
    return 1, None


def detect_currency(texts) -> str | None:
    """Return an ISO code when one is declared, else None.

    None means "unstated", not "dollars" -- a client who never says is almost
    certainly reporting in one currency throughout, and inventing a code for
    them would create a disagreement where the client stated none.
    """
    for text in texts:
        if not text:
            continue
        for pattern, code in _CURRENCY_PATTERNS:
            if pattern.search(str(text)):
                return code
    return None


def masthead(captions, rows) -> list[str]:
    """The text a statement uses to describe itself, not its contents."""
    out = [c for c in (captions or [])]
    for r in (rows or [])[:HEAD_ROWS]:
        label = getattr(r, "label", None) or getattr(r, "raw_label", None)
        if label:
            out.append(label)
    return out
