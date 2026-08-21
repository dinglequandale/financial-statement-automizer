"""L4 -- LLM proposals for accounts the deterministic layers could not resolve.

Why this layer exists (PLAN.md 7.0/7.1): on a cold-start client the profile is
empty and fuzzy matching has nothing to match against, so the deterministic
layers stall. More fundamentally, 15 of the 33 template lines a real engagement
uses do not exist in the blank template -- the engine has to *name a placeholder
slot* or *insert a line*, which no amount of string matching can do.

What is sent: account names, their section, and the template taxonomy grouped by
block. Never amounts, never the client's name. Personal names embedded in
account labels are pseudonymized first (see `redact.py`) -- a real chart of
accounts leaks owner identity through labels like `Paid in Capital - Brad Stein`.

What comes back is a *proposal*, never a decision. Every rule is marked
`Decider.LLM`, is arithmetically checked where possible (PLAN 7.3), and is
surfaced for human confirmation before it can be written.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from fsa.ingest.normalize import normalize
from fsa.mapping.redact import Redaction, redact
from fsa.mapping.template import TemplateSpec, taxonomy_prompt
from fsa.model.mapping import Decider, MappingRule, MappingSet, Target, TargetKind
from fsa.model.schema import ConsolidatedTable, RowKind, StatementType

# Measured on the TS eval: Sonnet 5 matched Opus 5 point-for-point on the balance
# sheet (91% e2e) and edged it overall (82% vs 80%) at ~60% of the cost. The gap
# is inside run-to-run variance, so this is "no worse, meaningfully cheaper".
MODEL = "claude-sonnet-5"
MAX_TOKENS = 32000  # thinking is on by default on Opus 5 and shares this budget

# Effort drives thinking depth, and thinking is billed as OUTPUT ($25/M on
# Opus 5) -- so it dominates the cost of this task, not the visible response.
# Default to medium: account classification is not a deep-reasoning problem,
# and Opus 5's low/medium tiers are strong. Raise only if the eval says to.
DEFAULT_EFFORT = "medium"


class MappingLevel(str, Enum):
    """At what level of the client's own hierarchy we map.

    Measured, k=5 per statement: this single choice is the dominant source of
    run-to-run variance, and leaving it to the model makes it a coin flip. On
    the income statement 33 of 35 contested accounts were the *same* binary --
    `Operating Expenses` (one bucket) versus an invented group such as
    `Payroll & Benefits` -- flipping together. On the balance sheet the model
    alternated between mapping `Total Checking/Savings` and mapping `Checking`,
    `New Checking` and `Undeposited Funds` individually.

    Crucially the model is *not* confused about where an account belongs: given
    that it answered at all, it named the same target in 19 of 21 balance-sheet
    cases. Only the level wobbles.

    **Deciding the level for it did not work, and the default stayed ALL.**
    Recorded here because the negative result is the useful part:

    * Forcing a statement-wide level collapsed variance on sample 2 (BS
      14/14/14/14; IS 4/5/5/5; ~65% cheaper) but cost TS 8 of its 16 income
      statement labels, because the TS ground truth is *mixed within one
      statement* -- revenue on its own lines (`Freight Income`,
      `Finance Revenues`), expenses rolled into `SG&A Expenses`.
    * AUTO was meant to fix that by asking per group. On TS it scored 77% and
      62% end-to-end across two runs, against 75% and 73% for ALL -- both the
      best run and the worst. It *raised* variance.

    The reason is structural: under AUTO the first pass shows only subtotals, so
    when the model maps a subtotal instead of breaking it out, its detail rows
    are never shown at all and `reconcile` excludes them as covered. Whether it
    breaks out is itself the same coin flip, now with more leverage. Filtering
    what the model may see converts a *recoverable* wrong mapping into an
    *unrecoverable* silent rollup.

    Kept because they are useful as explicit overrides and as measurement
    controls -- and because a per-client level, once an analyst has confirmed
    one, is exactly the sort of thing the profile should pin (PLAN 7.4).

    ALL    -- show every unresolved account; the model picks the level. Default.
    AUTO   -- ask the level per subtotal group, then a second pass for the
              components of any group broken out.
    DETAIL -- force individual accounts; ignore the client's subtotals.
    ROLLUP -- force the client's subtotals, plus any account no subtotal covers.
    """

    AUTO = "auto"
    DETAIL = "detail"
    ROLLUP = "rollup"
    #: Ask about every unresolved row at once and let the model pick the level
    #: implicitly. This is the pre-2026-08 behaviour, kept as the experimental
    #: control for measuring what the explicit level decision actually buys.
    ALL = "all"  # noqa: E501 - see class docstring for why this is the default


#: ALL is the default *on evidence*, not by inertia. Every filtered level was
#: measured against it on TS (the only client with ground truth), 2 runs each,
#: and none beat it -- see the table in PLAN 7.4. Filtering has one structural
#: failure mode that outweighs its stability: an account that is never shown to
#: the model can never be mapped, and `reconcile` then excludes it as covered.
#: ALL always shows every unresolved account, so the worst case is a mapping a
#: human corrects rather than an account silently rolled up.
DEFAULT_LEVEL: dict[StatementType, MappingLevel] = {
    StatementType.BS: MappingLevel.ALL,
    StatementType.IS: MappingLevel.ALL,
}


class _Proposal(BaseModel):
    account: str = Field(description="The client account label, copied exactly as given.")
    kind: Literal["assign", "name_slot", "insert", "exclude", "break_out"] = Field(
        description=(
            "assign: map to an existing named template line. "
            "name_slot: give a name to an unnamed placeholder slot. "
            "insert: add a new line into a named block. "
            "exclude: this account should not be mapped at all. "
            "break_out: ONLY for a client subtotal -- do not map this subtotal; "
            "its component accounts belong on separate template lines and will "
            "be asked about individually."
        )
    )
    target: str = Field(
        description=(
            "For assign: the exact existing template line. For name_slot and insert: "
            "the new line name to use. For exclude: a short reason."
        )
    )
    block: str = Field(
        default="",
        description="Required for insert and name_slot: the subtotal the line rolls into.",
    )
    slot: str = Field(
        default="",
        description="Required for name_slot: the exact placeholder slot label being named.",
    )
    sign: int = Field(
        default=1,
        description="+1 normally; -1 when this account must be SUBTRACTED from its target.",
    )
    confidence: float = Field(description="0.0-1.0. Be honest; low confidence is useful.")
    rationale: str = Field(description="One short sentence.")


class _Response(BaseModel):
    proposals: list[_Proposal]


def _strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic's JSON Schema, tightened for the structured-outputs contract.

    The API requires every object to set `additionalProperties: false` and to
    list every property in `required`; Pydantic omits both for fields with
    defaults. Rewriting here keeps the defaults usable in Python while still
    satisfying the API.
    """
    schema = model.model_json_schema()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


_SYSTEM = """\
You map a company's chart of accounts onto a standardized financial statement \
template used by valuation analysts.

You are given account names only. You will not be given amounts, and you should \
not ask for them. Some account labels contain <PERSON_n> placeholders where an \
individual's name was removed; treat those as opaque owner identifiers.

Rules:
- BALANCE SHEET: give a distinct class of asset or liability ITS OWN LINE. \
Valuation readers need to see each asset class separately, so do not bury one in \
a generic bucket merely because a bucket exists; use `insert` or `name_slot` \
freely. Reserve generic lines such as "Other Assets" for genuinely miscellaneous \
or immaterial residue.
- INCOME STATEMENT: the test is not "how detailed?" but "would a valuation \
analyst need to see or adjust this separately?". Give a DEDICATED line to any \
expense a valuation normalizes or adds back - owner or officer compensation, \
salaries and wages, rent (often related-party), owner retirement and benefit \
plans, insurance, professional fees, taxes other than income tax, and repairs \
and maintenance - and to any non-recurring or discretionary item. Likewise give \
each distinct REVENUE STREAM its own line (a different source of revenue, or a \
contra-revenue such as discounts, allowances or pricing adjustments), because \
revenue mix drives the multiple.
- Everything else on the income statement is ordinary operating cost that never \
gets adjusted - office supplies, postage, printing, dues and subscriptions, bank \
charges, janitorial, meals - and belongs together in the residual "Other \
Operating Expenses" line rather than on lines of its own.
- The template ships several empty "Operating Expense N" and "Revenue Component \
N" slots for exactly this purpose. Leaving them empty while everything lands in \
a single bucket defeats the model they belong to.
- Placeholder slots (e.g. "Revenue Component 1", "Operating Expense 3") carry no \
meaning. They are empty capacity. Use `name_slot` to give one a real name derived \
from the accounts you are grouping into it, and reuse the same target name for \
every account that belongs in that group.
- Do still group where the accounts are the SAME class: all cash-like accounts to \
Cash, all trade receivables and their contra accounts to Accounts Receivable, all \
prepaid accounts to Prepaid Expenses. Grouping same-class detail is right; \
flattening different classes into one bucket is not.
- ROLLUPS. Some entries below are the client's own subtotals, shown with their \
component accounts indented beneath them. When a subtotal lines up with a single \
template line, map the SUBTOTAL and mark every one of its components `exclude` \
with reason "rolled up". This is usually right for large expense groups - map \
"Total Operating Expenses" rather than fifty individual expense accounts. Map the \
components individually only when they belong on genuinely different template \
lines. Never map both a subtotal and any of its components: that double-counts.
- Set sign to -1 only when the account must be subtracted from its target - for \
example a tax or penalty netted inside an "Other Income (Expense)" line. A contra \
account that already arrives as a negative number is sign +1.
- Use `exclude` for accounts that should not be mapped: the client's own subtotals, \
memo lines, and equity detail that rolls into a single Total Equity line.
- Getting the block right matters more than getting the exact line right. An asset \
placed among liabilities, or a current item placed in non-current, is a serious \
error; choosing between two lines inside the same block is minor.

Return one proposal per account you were given, and no others."""


@dataclass
class LLMResult:
    proposed: int
    redaction: Redaction
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = MODEL
    effort: str = DEFAULT_EFFORT

    @property
    def cost_usd(self) -> float:
        pin, pout = PRICING.get(self.model, (5.0, 25.0))
        return self.input_tokens / 1e6 * pin + self.output_tokens / 1e6 * pout


def build_payload(
    accounts: list[tuple[str, str | None, bool]],
    spec: TemplateSpec,
    statement: StatementType,
    r: Redaction,
) -> str:
    """The exact user message that will be transmitted. Inspectable by design."""
    lines = [
        f"Statement: {'Balance Sheet' if statement is StatementType.BS else 'Income Statement'}",
        "",
        "TEMPLATE TARGETS (grouped by the subtotal each line rolls into):",
        taxonomy_prompt(spec, statement),
        "",
        "CLIENT ACCOUNTS TO MAP:",
    ]
    for label, section, is_subtotal in accounts:
        safe = r.rewritten.get(label, label)
        if is_subtotal:
            lines.append(f"  - {safe}   [CLIENT SUBTOTAL of the accounts indented below]")
        else:
            lines.append(
                f"      {safe}" + (f"   [section: {section}]" if section else "")
            )
    return "\n".join(lines)


#: Per-MTok (input, output). Sonnet 5 is on intro pricing through 2026-08-31.
#: Models accepting `output_config.effort`. Haiku 4.5 and other pre-5 models
#: reject it outright (400), so it must be omitted rather than passed blindly.
EFFORT_CAPABLE = frozenset({"claude-opus-5", "claude-sonnet-5", "claude-fable-5"})

PRICING = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def propose_unresolved(
    ms: MappingSet,
    table: ConsolidatedTable,
    spec: TemplateSpec,
    *,
    client=None,
    dry_run: bool = False,
    effort: str | None = DEFAULT_EFFORT,
    model: str = MODEL,
    level: "MappingLevel | None" = None,
) -> LLMResult:
    """Fill in every UNRESOLVED rule in `ms`, in one call.

    Batched deliberately: the model needs to see sibling accounts together to
    group them and to name a slot consistently across the group.
    """
    from fsa.mapping.reconcile import descendants, rows_under_subtotals

    if level is None:
        level = DEFAULT_LEVEL.get(ms.statement, MappingLevel.AUTO)

    kinds = {row.norm_label: row.kind for row in table.rows}
    contained = rows_under_subtotals(table)
    kids_of = {
        row.norm_label: {k.norm_label for k in descendants(table, i)}
        for i, row in enumerate(table.rows)
        if row.kind is RowKind.SUBTOTAL
    }

    def eligible(norm: str) -> bool:
        kind = kinds.get(norm)
        if level is MappingLevel.ALL:
            return True
        if level is MappingLevel.DETAIL:
            return kind is not RowKind.SUBTOTAL
        # AUTO and ROLLUP both open on the rollup question: the subtotals
        # themselves, plus any account no subtotal covers (an orphan has to be
        # mapped by name -- nothing rolls it up). Under AUTO the model may
        # answer `break_out`, which sends that group's children to a second pass.
        return kind is RowKind.SUBTOTAL or norm not in contained

    meta = {
        row.norm_label: (row.section, row.kind is RowKind.SUBTOTAL)
        for row in table.rows
    }
    # Emit in the client's own row order so subtotals sit above their components;
    # the indentation in build_payload only reads correctly in that order.
    order = {row.norm_label: i for i, row in enumerate(table.rows)}

    def batch(rules):
        rules = sorted(rules, key=lambda x: order.get(x.norm_account, 10**6))
        return [
            (r.client_account, *meta.get(r.norm_account, (None, False))) for r in rules
        ]

    first = [
        r
        for r in ms.rules
        if r.decided_by is Decider.UNRESOLVED and eligible(r.norm_account)
    ]
    accounts = batch(first)

    r = redact([a for a, _, _ in accounts])
    if not first:
        return LLMResult(proposed=0, redaction=r)

    def directive_for(lv: MappingLevel, second: bool) -> str:
        if second:
            return (
                "MAPPING LEVEL: detail. These are the component accounts of "
                "subtotals you chose to break out. Map each on its own merits."
            )
        if lv is MappingLevel.ALL:
            return ""
        if lv is MappingLevel.DETAIL:
            return (
                "MAPPING LEVEL: detail. You are being shown individual accounts, "
                "not the client's subtotals. Map each account on its own merits."
            )
        if lv is MappingLevel.ROLLUP:
            return (
                "MAPPING LEVEL: rollup. Map these subtotals. Do not propose "
                "mappings for the accounts inside them."
            )
        return (
            "MAPPING LEVEL: decide per group. You are being shown the client's "
            "own subtotals plus any account no subtotal contains. For EACH "
            "subtotal choose one: map it (when the whole group belongs on a "
            "single template line), or answer `break_out` (when its components "
            "belong on different template lines -- you will then be asked about "
            "those components individually). Choosing `break_out` is not a "
            "failure; it is the right answer whenever the template has several "
            "lines that the group's components map onto separately. Never map "
            "both a subtotal and its components."
        )

    payload = f"{directive_for(level, False)}\n\n{build_payload(accounts, spec, ms.statement, r)}"
    if dry_run:
        print(payload)
        return LLMResult(proposed=0, redaction=r)

    if client is None:
        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Load it from .env before running with --llm."
            )
        client = anthropic.Anthropic()

    def ask(text_payload: str) -> tuple[_Response, object]:
        # Streamed because thinking is on by default and shares max_tokens with
        # the response; at this budget the SDK refuses a non-streaming call.
        with client.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text_payload}],
            # No `temperature`: it is *deprecated* on Claude 5 models and
            # returns HTTP 400, so sampling variance cannot be dialled out at
            # the API. Stability comes from asking a better-posed question --
            # see `MappingLevel` and PLAN 7.4.
            output_config={
                **({"effort": effort} if effort and model in EFFORT_CAPABLE else {}),
                "format": {
                    "type": "json_schema",
                    "schema": _strict_schema(_Response),
                },
            },
        ) as stream:
            resp = stream.get_final_message()

        if resp.stop_reason == "refusal":
            raise RuntimeError(f"model declined: {getattr(resp, 'stop_details', None)}")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError(
                f"response truncated at max_tokens={MAX_TOKENS}; raise it or split the batch"
            )
        body = next((b.text for b in resp.content if b.type == "text"), None)
        if not body:
            raise RuntimeError("no text block in response")
        return _Response.model_validate(json.loads(body)), resp.usage

    parsed, usage = ask(payload)
    result = LLMResult(
        proposed=len(parsed.proposals),
        redaction=r,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        effort=effort,
        model=model,
    )
    broken = {
        normalize(r.restore(p.account))
        for p in parsed.proposals
        if p.kind == "break_out"
    }
    _apply(parsed, ms, r, spec)

    # Second pass: the components of every group the model chose to break out.
    # This is what lets one statement carry a mixed level -- TS needs revenue
    # broken out onto separate lines while its expenses roll up.
    if level not in (MappingLevel.DETAIL, MappingLevel.ALL) and broken:
        want = set().union(*(kids_of.get(b, set()) for b in broken))
        second = [
            rule
            for rule in ms.rules
            if rule.decided_by is Decider.UNRESOLVED
            and rule.norm_account in want
            and kinds.get(rule.norm_account) is RowKind.DATA
        ]
        if second:
            accounts2 = batch(second)
            r2 = redact([a for a, _, _ in accounts2])
            payload2 = (
                f"{directive_for(level, True)}\n\n"
                f"{build_payload(accounts2, spec, ms.statement, r2)}"
            )
            parsed2, usage2 = ask(payload2)
            result.proposed += len(parsed2.proposals)
            result.input_tokens += usage2.input_tokens
            result.output_tokens += usage2.output_tokens
            _apply(parsed2, ms, r2, spec)

    return result


def _apply(parsed: _Response, ms: MappingSet, r: Redaction, spec: TemplateSpec) -> None:
    """Fold proposals back onto the UNRESOLVED rules, un-redacting as we go."""
    by_norm = {
        normalize(r.restore(p.account)): p for p in parsed.proposals
    }
    now = datetime.now(timezone.utc)

    for rule in ms.rules:
        if rule.decided_by is not Decider.UNRESOLVED:
            continue
        p = by_norm.get(rule.norm_account)
        if p is None:
            continue

        if p.kind == "exclude":
            rule.rationale = f"model suggests excluding: {p.target}"
            rule.confidence = p.confidence
            continue

        if p.kind == "break_out":
            # Not a mapping: a decision that this group's members belong on
            # separate lines. The rollup stays UNRESOLVED (mapping it as well
            # would double-count) and its children go to the second pass.
            rule.rationale = f"broken out into components: {p.rationale}"
            rule.confidence = p.confidence
            continue

        kind = {
            "assign": TargetKind.ASSIGN,
            "name_slot": TargetKind.NAME_SLOT,
            "insert": TargetKind.INSERT,
        }[p.kind]

        # Guard the model's structural claims before trusting them.
        if kind is TargetKind.ASSIGN:
            line = spec.find(ms.statement, p.target)
            if line is None or not line.is_target:
                kind = TargetKind.INSERT  # names a line that does not exist
        if kind is TargetKind.INSERT and not p.block:
            continue  # unusable without a block; leave UNRESOLVED for a human
        if kind is TargetKind.NAME_SLOT and not p.slot:
            kind, _ = TargetKind.INSERT, None
            if not p.block:
                continue

        try:
            target = Target(
                statement=ms.statement,
                label=p.target.strip(),
                kind=kind,
                block=p.block.strip() or None,
                slot_label=p.slot.strip() or None,
            )
        except ValueError:
            continue  # malformed; a human sees it as UNRESOLVED

        rule.target = target
        rule.sign = -1 if p.sign < 0 else 1
        rule.confidence = max(0.0, min(1.0, p.confidence))
        rule.decided_by = Decider.LLM
        rule.rationale = p.rationale
        rule.decided_at = now
