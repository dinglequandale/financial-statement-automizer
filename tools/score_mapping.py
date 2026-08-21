"""Score a proposed mapping against the expert ground truth.

This is the measurement Phase 1 turns on. It answers, with numbers:

  - how far do the deterministic layers get on a cold-start client?
  - does adding the LLM layer close the gap, and by how much?
  - at what confidence threshold is auto-accept actually safe?

Scoring rules follow PLAN.md 7.0. Two deliberate choices:

1. **Coverage and accuracy are reported separately.** A mapper that resolves
   20 of 60 accounts perfectly is not "100% accurate" -- it left 40 for a
   human. Reporting only accuracy-over-attempted would flatter it badly.

2. **Cross-block errors are counted separately from within-block errors.**
   Arithmetic verification catches the former and cannot catch the latter
   (PLAN 7.3), so they carry different operational risk and must not be
   averaged into one number.

Usage:
    python tools/score_mapping.py --truth eval/ts_distributors_mapping.yaml \\
        --template "Konrad Project/sample_1_TS/BVAL Model (Weaver Template).xlsx" \\
        --clients "Konrad Project/sample_1_TS/TS Distributors Financial Statements (from client)"
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from fsa.ingest.normalize import normalize
from fsa.model.mapping import Decider, MappingSet
from fsa.model.schema import StatementType

_EXCLUDE_DEFAULT = "Dec 2020 -2025 Financias (Weaver edited).xlsx"


@dataclass
class Outcome:
    """What happened to one ground-truth account."""

    account: str
    expected_line: str
    expected_sign: int
    got_line: str | None = None
    got_sign: int | None = None
    decided_by: str = "missing"
    confidence: float = 0.0

    @property
    def attempted(self) -> bool:
        return self.got_line is not None and self.decided_by != Decider.UNRESOLVED.value

    renamed: bool = False  # right row, but the analyst relabelled it

    @property
    def line_correct(self) -> bool:
        """Exact label match."""
        return self.attempted and normalize(self.got_line) == normalize(self.expected_line)

    @property
    def placement_correct(self) -> bool:
        """Landed on the row the analyst used, allowing for their relabelling.

        `Goodwill` -> `Goodwill (Net)` and `Unearned Revenue` -> `Deferred
        Revenue` are the same template row under a new name: the number goes to
        the right place and the label is a cosmetic fix during review. Counting
        those as failures would understate the engine and, worse, push us to
        "fix" behaviour that is already right.
        """
        return self.line_correct or (self.attempted and self.renamed)

    @property
    def correct(self) -> bool:
        return self.placement_correct and self.got_sign == self.expected_sign

    @property
    def strict_correct(self) -> bool:
        return self.line_correct and self.got_sign == self.expected_sign


@dataclass
class Score:
    statement: str
    outcomes: list[Outcome] = field(default_factory=list)
    cross_block: int = 0
    within_block: int = 0
    block_unknown: int = 0
    sign_only: int = 0

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def attempted(self) -> int:
        return sum(1 for o in self.outcomes if o.attempted)

    @property
    def correct(self) -> int:
        return sum(1 for o in self.outcomes if o.correct)

    @property
    def strict(self) -> int:
        return sum(1 for o in self.outcomes if o.strict_correct)

    @property
    def renames(self) -> int:
        return sum(1 for o in self.outcomes if o.correct and not o.strict_correct)

    @property
    def coverage(self) -> float:
        return self.attempted / self.total if self.total else 0.0

    @property
    def precision(self) -> float:
        """Of what it attempted, how much was right."""
        return self.correct / self.attempted if self.attempted else 0.0

    @property
    def end_to_end(self) -> float:
        """Of everything, how much came out right with no human involved."""
        return self.correct / self.total if self.total else 0.0


#: Above this similarity, an expected label absent from the template is treated
#: as the analyst's rename of the proposed label rather than a different line.
#: Calibrated on the observed renames (`Goodwill (Net)` 0.80,
#: `Deferred Revenue` 0.75) against the nearest true miss (`Sales Revenue` vs
#: `Revenue` 0.70, which IS a different row -- the subtotal, not a slot).
RENAME_THRESHOLD = 0.73


def _is_rename(spec, statement: StatementType, got: str | None, expected: str) -> bool:
    """Did the analyst relabel the very line we proposed?

    Only applies when the expected label does not exist in the template -- if it
    does exist, it is a genuinely different row and a miss is a miss.
    """
    if not got:
        return False
    if spec.find(statement, expected) is not None:
        return False
    from fsa.ingest.normalize import similarity

    return similarity(got, expected) >= RENAME_THRESHOLD


def _block_of(spec, statement: StatementType, label: str, delivered=None) -> str | None:
    """Which subtotal does this line roll into?

    Falls back to the *delivered* model when the label is absent from the blank
    template. That fallback is load-bearing: 15 of the 33 lines the analyst used
    were inserted or renamed, so without it every inserted target resolves to
    `None` and the cross-block/within-block split -- the whole point of the
    metric -- silently degrades to "unknown".

    A collapsible block subtotal *is* its own block. `Revenue` (IS row 14) is
    `derived, collapsible, block=None`, so mapping `Sales -> Revenue` used to
    resolve to `None` and land in `block_unknown` -- when in fact the money is
    inside the Revenue block and every total still ties. PLAN 7.0 already says
    collapsing onto a block subtotal is legal; the metric has to agree.
    """
    for src in (spec, delivered):
        if src is None:
            continue
        line = src.find(statement, label)
        if line is None:
            continue
        if line.block:
            return line.block
        if line.derived and line.collapsible:
            return line.label
    return None


def _got_block(spec, statement: StatementType, rule, delivered=None) -> str | None:
    """The block a *proposal* lands in, taken from the proposal itself.

    Do not re-derive this from the label. For `NAME_SLOT` and `INSERT` -- which
    is most of the income statement -- the label is the model's own invented
    English (`Net Product Sales`, `Finance & Other Revenue`) and exists in
    neither the blank template nor the delivered model, so a label lookup
    always returns `None` and the error is filed as `block_unknown`. That was
    burying roughly half of all errors in an unclassifiable bucket and made the
    cross-block count a lower bound rather than a measurement.

    `Target.block` carries the authoritative placement and is populated on
    every LLM proposal (`llm.py`) and every reconciled slot (`reconcile.py`).
    `value_score.py` already reads it this way; this brings the label scorer in
    line. Fall back to the label lookup only when the target carries no block.
    """
    if rule is None:
        return None
    if rule.target.block:
        return rule.target.block
    if rule.target.slot_label:
        slot = spec.find(statement, rule.target.slot_label)
        if slot is not None and slot.block:
            return slot.block
    return _block_of(spec, statement, rule.target.label, delivered)


def score(
    truth: dict, proposals: dict[str, MappingSet], spec, delivered=None
) -> dict[str, Score]:
    out: dict[str, Score] = {}
    for statement in ("BS", "IS"):
        st = StatementType(statement)
        s = Score(statement=statement)
        rules = {r.norm_account: r for r in proposals.get(statement, MappingSet(st)).rules}

        for m in truth["mappings"]:
            if m["statement"] != statement:
                continue
            norm = normalize(m["client_account"])
            o = Outcome(
                account=m["client_account"],
                expected_line=m["template_line"],
                expected_sign=int(m.get("sign", 1)),
            )
            r = rules.get(norm)
            if r is not None:
                o.decided_by = r.decided_by.value
                o.confidence = r.confidence
                if r.decided_by is not Decider.UNRESOLVED:
                    o.got_line = r.target.label
                    o.got_sign = r.sign
                    o.renamed = _is_rename(spec, st, o.got_line, o.expected_line)
            s.outcomes.append(o)

            if o.attempted and not o.placement_correct:
                want = _block_of(spec, st, o.expected_line, delivered)
                got = _got_block(spec, st, r, delivered)
                if want is None or got is None:
                    s.block_unknown += 1
                elif want != got:
                    s.cross_block += 1
                else:
                    s.within_block += 1
            elif o.placement_correct and not o.correct:
                s.sign_only += 1

        out[statement] = s
    return out


def render(scores: dict[str, Score], verbose: bool = False) -> str:
    lines: list[str] = []
    grand = Score(statement="ALL")
    for s in scores.values():
        grand.outcomes.extend(s.outcomes)
        grand.cross_block += s.cross_block
        grand.within_block += s.within_block
        grand.block_unknown += s.block_unknown
        grand.sign_only += s.sign_only

    lines.append("=" * 74)
    lines.append(f"{'':10s} {'labels':>7} {'tried':>7} {'right':>7} "
                 f"{'cover':>7} {'prec':>7} {'e2e':>7}")
    lines.append("-" * 74)
    for name in ("BS", "IS"):
        s = scores[name]
        lines.append(
            f"{name:10s} {s.total:>7d} {s.attempted:>7d} {s.correct:>7d} "
            f"{s.coverage:>6.0%} {s.precision:>6.0%} {s.end_to_end:>6.0%}"
        )
    lines.append("-" * 74)
    lines.append(
        f"{'TOTAL':10s} {grand.total:>7d} {grand.attempted:>7d} {grand.correct:>7d} "
        f"{grand.coverage:>6.0%} {grand.precision:>6.0%} {grand.end_to_end:>6.0%}"
    )
    lines.append("=" * 74)
    lines.append("  cover = attempted / labels      (how much needed no human)")
    lines.append("  prec  = correct / attempted     (of what it tried, how much was right)")
    lines.append("  e2e   = correct / labels        (the number that actually matters)")
    if grand.renames:
        lines.append("")
        lines.append(
            f"  {grand.renames} correct landed on the right row under the template's original"
        )
        lines.append(
            f"  name, which the analyst then relabelled. Strict label match: "
            f"{grand.strict}/{grand.total} = {grand.strict / grand.total:.0%}."
        )
    lines.append("")
    block_ok = grand.correct + grand.within_block
    lines.append(
        f"block placement: {block_ok}/{grand.total} = {block_ok / grand.total:.0%} land in the "
        f"right subtotal"
    )
    lines.append(
        f"  -> totals tie and the valuation is unaffected for these, even where the "
        f"exact line differs"
    )
    lines.append("")
    lines.append(f"errors: {grand.cross_block} cross-block "
                 f"(arithmetic verification CATCHES these), "
                 f"{grand.within_block} within-block (it does NOT), "
                 f"{grand.block_unknown} block-unknown, "
                 f"{grand.sign_only} sign-only")

    by_layer = Counter(o.decided_by for s in scores.values() for o in s.outcomes)
    lines.append("")
    lines.append("resolved by layer:")
    for layer, n in by_layer.most_common():
        ok = sum(
            1 for s in scores.values() for o in s.outcomes
            if o.decided_by == layer and o.correct
        )
        lines.append(f"  {layer:<12} {n:>4}  ({ok} correct)")

    wrong = [o for s in scores.values() for o in s.outcomes if o.attempted and not o.correct]
    if wrong:
        lines.append("")
        lines.append(f"incorrect ({len(wrong)}):")
        for o in wrong[: None if verbose else 15]:
            sign = "" if o.got_sign == o.expected_sign else f"  [sign {o.got_sign:+d} != {o.expected_sign:+d}]"
            lines.append(
                f"  {o.account[:34]:<34} got {str(o.got_line)[:24]:<24} "
                f"want {o.expected_line[:24]:<24}{sign}"
            )
        if not verbose and len(wrong) > 15:
            lines.append(f"  ... and {len(wrong) - 15} more (-v for all)")

    missed = [o for s in scores.values() for o in s.outcomes if not o.attempted]
    if missed:
        lines.append("")
        lines.append(f"left for a human ({len(missed)}):")
        for o in missed[: None if verbose else 20]:
            lines.append(f"  {o.account[:38]:<38} -> should be {o.expected_line}")
        if not verbose and len(missed) > 20:
            lines.append(f"  ... and {len(missed) - 20} more (-v for all)")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--truth", type=Path, default=Path("eval/ts_distributors_mapping.yaml"))
    ap.add_argument("--template", type=Path, required=True)
    ap.add_argument("--clients", type=Path, required=True)
    ap.add_argument("--exclude", action="append", default=[_EXCLUDE_DEFAULT])
    ap.add_argument("--profile", type=Path, help="warm-start from a saved profile")
    ap.add_argument("--llm", action="store_true", help="enable the L4 LLM layer")
    ap.add_argument("--delivered", type=Path, help="delivered model, to resolve inserted-line blocks")
    ap.add_argument("--effort", default="medium", choices=["low","medium","high","xhigh","max"])
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--level", default=None,
                    choices=["auto", "detail", "rollup", "all"],
                    help="mapping level policy (default: auto)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not args.truth.is_file():
        print(f"no ground truth at {args.truth}", file=sys.stderr)
        return 2

    from fsa.consolidate import consolidate
    from fsa.ingest.extract import extract_workbook
    from fsa.mapping.template import load_template
    from fsa.model.schema import StatementSet

    spec = load_template(args.template)
    truth = yaml.safe_load(args.truth.read_text(encoding="utf-8"))

    ss = StatementSet()
    # Clients do not all send spreadsheets. TS sent multi-column workbooks;
    # Commercial Flooring sent QuickBooks PDFs. `extract_workbook` handles the
    # multi-column spreadsheet shape, `interpret` handles single-period
    # documents in any format (PLAN 2.7) -- so dispatch on extension rather
    # than assuming one.
    from fsa.ingest.interpret import interpret, read_any

    files = sorted(
        p
        for p in args.clients.iterdir()
        if p.is_file()
        and p.suffix.lower() in (".xlsx", ".xlsm", ".ods", ".pdf")
        and p.name not in args.exclude
    )
    if not files:
        print(f"no client files under {args.clients}", file=sys.stderr)
        return 2
    for p in files:
        if p.suffix.lower() in (".xlsx", ".xlsm"):
            cols = list(extract_workbook(p))
            if cols:
                for col in cols:
                    ss.add(col)
                continue
        for col in interpret(read_any(p))[0]:
            ss.add(col)

    try:
        from fsa.mapping.matcher import propose
    except ImportError as e:
        print(f"matcher not available yet: {e}", file=sys.stderr)
        return 2

    profile = None
    if args.profile and args.profile.is_file():
        from fsa.mapping.profile import load_profile

        profile = load_profile(args.profile)

    proposals: dict[str, MappingSet] = {}
    spend: list = []
    for st in (StatementType.BS, StatementType.IS):
        table = consolidate(ss, st)
        ms = propose(table, spec, profile=profile)
        if args.llm:
            from fsa.mapping.llm import propose_unresolved

            from fsa.mapping.llm import MappingLevel

            res = propose_unresolved(
                ms, table, spec, effort=args.effort, model=args.model,
                level=MappingLevel(args.level) if args.level else None,
            )
            spend.append((st.value, res))
        proposals[st.value] = ms

    print(f"template : {args.template.name}")
    print(f"clients  : {len(files)} workbooks")
    print(f"truth    : {len(truth['mappings'])} labeled pairs")
    print(f"llm      : {(args.model + ', effort=' + args.effort) if args.llm else 'off (deterministic only)'}")
    print()
    if spend:
        ti = sum(r.input_tokens for _, r in spend)
        to = sum(r.output_tokens for _, r in spend)
        cost = sum(r.cost_usd for _, r in spend)
        print()
        for name, r in spend:
            print(f"  {name}: {r.input_tokens:>6,} in / {r.output_tokens:>6,} out  "
                  f"= ${r.cost_usd:.3f}  ({r.proposed} proposals, "
                  f"{r.redaction.person_count} names redacted)")
        print(f"  TOTAL: {ti:,} in / {to:,} out = ${cost:.3f} per client run")
    delivered = load_template(args.delivered) if args.delivered else None
    print(render(score(truth, proposals, spec, delivered), verbose=args.verbose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
