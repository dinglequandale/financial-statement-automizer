"""Measure the metric that actually matters: analyst-minutes to a correct model.

Label accuracy is not the product. The product is a reviewed draft, so the
number to minimise is how long an analyst spends turning that draft into a
model they will sign, and the number to hold at zero is how many wrong figures
survive that review. This tool reports both.

**Burden.** The review sheet (`fsa/review/sheet.py`) sorts every account into
one of four tiers, and each tier is a different physical action:

    RED    unresolved / arithmetically contradicted -> decide from scratch
    AMBER  proposed below 0.85 confidence           -> verify a suggestion
    WHITE  proposed at or above 0.85               -> skim and move on
    GREEN  confirmed by profile or human            -> skim, faster
    EXCL   auto-excluded, pre-filled                -> skim the reason

Multiplying tier counts by per-action times gives minutes. The times are
assumptions, stated in `RATES` and adjustable from the command line -- the tier
*counts* are measured, the seconds are judgement. Three scenarios are printed
so the estimate is a range, never a false point value.

**Escape risk.** With ground truth available, the guard number is the error
rate among proposals the sheet does NOT flag -- WHITE and GREEN rows. A wrong
mapping an analyst was invited to skim is the only kind that reaches a client.
Reported alongside a calibration table, because the whole triage design rests
on confidence meaning something and that has never been checked.

**Cache.** Every run dumps its proposals to JSON. Re-scoring a saved run costs
nothing, so changing a metric no longer means re-spending money and re-arguing
from two noisy runs (PLAN 7.4's standing rule). Use --replay to score a run
again, and accumulate runs over time to escape n=16.

Usage:
    python tools/burden.py TS --llm
    python tools/burden.py FLOOR --llm
    python tools/burden.py TS --replay out/runs/TS-20260815-1.json
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from fsa.consolidate import consolidate
from fsa.ingest.extract import extract_workbook
from fsa.ingest.interpret import interpret, read_any
from fsa.ingest.normalize import normalize
from fsa.mapping.reconcile import reconcile
from fsa.mapping.template import load_template
from fsa.model.mapping import Decider, Exclusion, MappingRule, MappingSet, Target, TargetKind
from fsa.model.schema import RowKind, StatementSet, StatementType
from fsa.review.sheet import LOW_CONFIDENCE

warnings.simplefilter("ignore")

REPO = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    """Read `.env` into the environment if the key is not already set.

    `llm.py` requires ANTHROPIC_API_KEY and the repo ships a `.env`, but
    nothing loaded it -- every LLM run so far needed a manual export, which is
    exactly the kind of step that does not survive being handed to someone
    else. No value is ever printed.
    """
    import os

    env = REPO / ".env"
    if os.environ.get("ANTHROPIC_API_KEY") or not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_dotenv()
S1 = REPO / "Konrad Project" / "sample_1_TS"
S2 = REPO / "Konrad Project" / "sample_2_flooring"

S3 = REPO / "Konrad Project" / "sample_3_agam_capital"

CLIENTS = {
    "TS": {
        "files": S1 / "TS Distributors Financial Statements (from client)",
        "exclude": {"Dec 2020 -2025 Financias (Weaver edited).xlsx"},
        "truth": REPO / "eval" / "ts_distributors_mapping.yaml",
        "scope": None,
    },
    "FLOOR": {
        "files": S2 / "orig_pdfs",
        "exclude": set(),
        "truth": REPO / "eval" / "commercial_flooring_mapping.yaml",
        "scope": None,
    },
    # Sample 3 is the first client with a reporting scope to choose: every
    # workbook carries a `consolidated` tab beside `US`, `Canada` and
    # `Bermuda`. The answer key was extracted from the delivered model's
    # consolidated columns, so the measurement has to be run against the same
    # scope -- comparing a subsidiary's figures to a group answer key would
    # score the wrong thing.
    "AGAM": {
        "files": S3,
        "exclude": {
            "DRAFT - AOK Model.xlsx",
            "DRAFT - AOK Holdings, LLC Report 3.31.25.pdf",
        },
        "truth": REPO / "eval" / "agam_mapping.yaml",
        "scope": "consolidated",
    },
}
TEMPLATE = S1 / "BVAL Model (Weaver Template).xlsx"

#: Seconds per physical action on the review sheet, (fast, slow).
#:
#: These are estimates for an analyst who knows the template, working in Excel
#: with a validated dropdown. They are the only made-up numbers here and they
#: are deliberately quotable and overridable. `decide` is slowest because the
#: analyst has to form the mapping themselves; `skim` is a glance at a row that
#: already reads correctly.
RATES = {
    "decide": (25, 60),
    "verify": (10, 25),
    "skim": (3, 8),
    "skim_excl": (2, 5),
    "retype": (5, 10),
}
#: Fixed per-engagement cost outside the row-by-row work: open the sheet, save
#: it, run the writer, open the model, eyeball the totals.
OVERHEAD_MIN = (5, 15)


# --------------------------------------------------------------------------
# pipeline + cache
# --------------------------------------------------------------------------

def load_client(name: str) -> StatementSet:
    """Ingest exactly the way `prepare` does.

    This used to reimplement the read loop, which quietly meant the burden
    numbers were measured against a different pipeline than the one that ships:
    no scope selection, no page joining, no period resolution. Any accuracy
    figure produced that way is measuring a build nobody runs.
    """
    from fsa.ingest.period import PeriodResolver
    from fsa.job import ingest, resolve_inputs

    cfg = CLIENTS[name]
    files = resolve_inputs([str(cfg["files"])], sorted(cfg["exclude"]))
    ss, _findings = ingest(
        files, resolver=PeriodResolver(), scope=cfg.get("scope")
    )
    return ss


def profile_from_truth(name: str, spec) -> "object":
    """Last year's confirmed decisions, reconstructed from the answer key.

    Every measurement in this project has been a cold start with an empty
    profile, while the product is sold on the second engagement being cheap.
    That claim has never been tested. This builds the profile an analyst would
    have left behind after signing off year one -- every ground-truth pair,
    marked confirmed -- so `propose()`'s L0 replay can be measured instead of
    assumed.

    It is an **upper bound**, and deliberately so: a real second year brings a
    drifted chart of accounts, so some rows that hit the profile here would be
    new next year. The number to read from it is how much of the burden the
    replay mechanism can remove at best, not what year two will cost exactly.
    """
    from datetime import date as _date

    from fsa.model.mapping import ClientProfile, MappingRule, MappingSet, Target, TargetKind

    truth = yaml.safe_load(CLIENTS[name]["truth"].read_text(encoding="utf-8"))
    sets: dict[str, MappingSet] = {}
    for m in truth["mappings"]:
        st = StatementType(m["statement"])
        ms = sets.setdefault(st.value, MappingSet(statement=st))
        line = spec.find(st, m["template_line"])
        # Many ground-truth targets are lines the analyst renamed or inserted,
        # so they are absent from the blank template and carry no block. For a
        # burden measurement the kind is immaterial -- L0 replays the label and
        # the row lands GREEN either way -- so record ASSIGN rather than
        # fabricate a block an INSERT would require.
        ms.rules.append(
            MappingRule(
                client_account=m["client_account"],
                norm_account=normalize(m["client_account"]),
                target=Target(
                    statement=st,
                    label=m["template_line"],
                    kind=TargetKind.ASSIGN,
                    block=line.block if line else None,
                ),
                sign=int(m.get("sign", 1)),
                confidence=1.0,
                decided_by=Decider.HUMAN,
                rationale="confirmed in a prior engagement",
            )
        )
    return ClientProfile(
        client_name=name,
        template_source=str(TEMPLATE),
        created_at=_date.today(),
        updated_at=_date.today(),
        sets=sets,
    )


def run_pipeline(name: str, spec, use_llm: bool, model: str, effort: str, profile=None):
    ss = load_client(name)
    sets, tables = {}, {}
    spend = []
    for st in (StatementType.BS, StatementType.IS):
        table = consolidate(ss, st)
        from fsa.mapping.matcher import propose

        ms = propose(table, spec, profile=profile)
        if use_llm:
            from fsa.mapping.llm import propose_unresolved

            spend.append(propose_unresolved(ms, table, spec, effort=effort, model=model))
        sets[st], tables[st] = ms, table
    return sets, tables, spend


def dump_run(path: Path, name: str, sets, meta: dict) -> Path:
    """Persist proposals so any future metric can re-score this run for free."""
    payload = {
        "client": name,
        "at": datetime.now(timezone.utc).isoformat(),
        "meta": meta,
        "statements": {},
    }
    for st, ms in sets.items():
        payload["statements"][st.value] = {
            "rules": [
                {
                    "client_account": r.client_account,
                    "norm_account": r.norm_account,
                    "target": {
                        "label": r.target.label,
                        "kind": r.target.kind.value,
                        "block": r.target.block,
                        "slot_label": r.target.slot_label,
                    },
                    "sign": r.sign,
                    "confidence": r.confidence,
                    "decided_by": r.decided_by.value,
                    "verified": r.verified,
                    "rationale": r.rationale,
                }
                for r in ms.rules
            ],
            "exclusions": [
                {
                    "client_account": e.client_account,
                    "norm_account": e.norm_account,
                    "reason": e.reason,
                    "decided_by": e.decided_by.value,
                }
                for e in ms.exclusions
            ],
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


def load_run(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    sets = {}
    for stv, blob in payload["statements"].items():
        st = StatementType(stv)
        ms = MappingSet(statement=st)
        for d in blob["rules"]:
            t = d["target"]
            ms.rules.append(
                MappingRule(
                    client_account=d["client_account"],
                    norm_account=d["norm_account"],
                    target=Target(
                        statement=st,
                        label=t["label"],
                        kind=TargetKind(t["kind"]),
                        block=t["block"],
                        slot_label=t["slot_label"],
                    ),
                    sign=d["sign"],
                    confidence=d["confidence"],
                    decided_by=Decider(d["decided_by"]),
                    verified=d["verified"],
                    rationale=d["rationale"],
                )
            )
        for d in blob["exclusions"]:
            ms.exclusions.append(
                Exclusion(
                    client_account=d["client_account"],
                    norm_account=d["norm_account"],
                    reason=d["reason"],
                    decided_by=Decider(d["decided_by"]),
                )
            )
        sets[st] = ms
    return sets, payload


# --------------------------------------------------------------------------
# the burden model
# --------------------------------------------------------------------------

def tier_of(rule: MappingRule) -> str:
    """The same four tiers the review sheet colours, so counts match the UI."""
    if rule.decided_by is Decider.UNRESOLVED or rule.verified is False:
        return "red"
    if rule.decided_by.is_confirmed:
        return "green"
    if rule.confidence < LOW_CONFIDENCE:
        return "amber"
    return "white"


def burden(sets) -> Counter:
    c = Counter()
    for ms in sets.values():
        for r in ms.rules:
            c[tier_of(r)] += 1
        c["excl"] += len(ms.exclusions)
    return c


def minutes(counts: Counter, retypes: int, scenario: str) -> float:
    """scenario: best | expected | worst."""
    i = 0 if scenario == "best" else 1
    if scenario == "expected":
        rate = lambda k: sum(RATES[k]) / 2.0
        oh = sum(OVERHEAD_MIN) / 2.0
    else:
        rate = lambda k: RATES[k][i]
        oh = OVERHEAD_MIN[i]

    if scenario == "worst":
        # True worst case: the ordering helps but the proposals do not, so every
        # unconfirmed row costs a from-scratch decision. This is the number to
        # quote when asked "what if the model is useless on this client?"
        rows = counts["red"] + counts["amber"] + counts["white"]
        secs = rows * rate("decide") + counts["green"] * rate("skim")
        secs += counts["excl"] * rate("verify")
    else:
        secs = (
            counts["red"] * rate("decide")
            + counts["amber"] * rate("verify")
            + counts["white"] * rate("skim")
            + counts["green"] * rate("skim")
            + counts["excl"] * rate("skim_excl")
            + retypes * rate("retype")
        )
    return secs / 60.0 + oh


class VerifyTotals:
    """Sum the per-statement verify reports so the header reads as one number."""

    def __init__(self, reps):
        self.checked = sum(r.checked_blocks for r in reps.values())
        self.failed = sum(r.failed_blocks for r in reps.values())
        self.suppressed = sum(r.suppressed_rows for r in reps.values())
        self.flagged = sum(r.flagged_rules for r in reps.values())
        self.unsafe = sum(r.unsafe_exclusions for r in reps.values())


def manual_baseline(tables) -> tuple[int, float, float]:
    """Today's process: every data account decided by hand, nothing proposed."""
    rows = sum(
        1 for t in tables.values() for r in t.rows if r.kind is RowKind.DATA
    )
    lo = rows * RATES["decide"][0] / 60.0 + OVERHEAD_MIN[0]
    hi = rows * RATES["decide"][1] / 60.0 + OVERHEAD_MIN[1]
    return rows, lo, hi


# --------------------------------------------------------------------------
# the guard: what slips past an unflagged row
# --------------------------------------------------------------------------

def escapes(sets, truth_path: Path, spec, delivered=None):
    """Ground-truth errors sitting on rows the sheet invites the analyst to skim."""
    if not truth_path.is_file():
        return None
    truth = yaml.safe_load(truth_path.read_text(encoding="utf-8"))
    from score_mapping import _block_of, _got_block, _is_rename

    out = {
        "flagged_wrong": 0, "flagged_right": 0,
        "unflagged_wrong": 0, "unflagged_right": 0,
        "retypes": 0, "cross_block_escapes": [], "unknown_block": 0,
        "buckets": defaultdict(lambda: [0, 0]),          # placement
        "name_buckets": defaultdict(lambda: [0, 0]),     # label
    }
    for stv in ("BS", "IS"):
        st = StatementType(stv)
        ms = sets[st]
        by_norm = {r.norm_account: r for r in ms.rules}
        for m in truth["mappings"]:
            if m["statement"] != stv:
                continue
            norm = normalize(m["client_account"])
            r = by_norm.get(norm)
            if r is None or r.decided_by is Decider.UNRESOLVED:
                # excluded-because-a-rollup-covers-it is a decision, not a miss:
                # the money is on the line via the rollup. Not scored here.
                continue
            tier = tier_of(r)
            flagged = tier in ("red", "amber")
            same_label = normalize(r.target.label) == normalize(m["template_line"])
            renamed = _is_rename(spec, st, r.target.label, m["template_line"])
            sign_ok = r.sign == int(m.get("sign", 1))
            name_right = (same_label or renamed) and sign_ok

            # The two failures are not the same failure and must never be
            # averaged. A wrong *name* costs one retype and cannot change a
            # valuation. A wrong *block* moves money between subtotals and is
            # the only kind that can reach a client as an error.
            want = _block_of(spec, st, m["template_line"], delivered)
            got = _got_block(spec, st, r, delivered)
            if want is None or got is None:
                out["unknown_block"] += 1
                place_right = name_right
            else:
                place_right = (want == got) and sign_ok

            if place_right and not same_label:
                out["retypes"] += 1

            key = "flagged" if flagged else "unflagged"
            out[f"{key}_{'right' if place_right else 'wrong'}"] += 1
            b = round(min(0.99, r.confidence) * 10) / 10
            out["buckets"][b][0] += place_right
            out["buckets"][b][1] += 1
            out["name_buckets"][b][0] += name_right
            out["name_buckets"][b][1] += 1

            if not place_right and want and got:
                out["cross_block_escapes"].append(
                    (stv, m["client_account"], r.target.label, m["template_line"],
                     got, want, tier, r.confidence)
                )
    return out


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("client", choices=sorted(CLIENTS))
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--replay", type=Path, help="score a cached run instead of calling the API")
    ap.add_argument("--delivered", type=Path)
    ap.add_argument("--out", type=Path, default=REPO / "out" / "runs")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip arithmetic verification (to measure its effect)")
    ap.add_argument("--no-rollup", action="store_true",
                    help="skip rollup completion only")
    ap.add_argument("--no-complete", action="store_true",
                    help="skip sibling completion (the other half of the A/B)")
    ap.add_argument("--repeat-client", action="store_true",
                    help="seed L0 from the answer key, i.e. measure the second "
                         "engagement rather than the cold start (upper bound)")
    args = ap.parse_args()

    spec = load_template(TEMPLATE)
    ss = load_client(args.client)
    tables = {st: consolidate(ss, st) for st in (StatementType.BS, StatementType.IS)}

    if args.replay:
        sets, payload = load_run(args.replay)
        meta = payload.get("meta", {})
        print(f"replaying {args.replay.name}  ({meta})")
    else:
        prof = profile_from_truth(args.client, spec) if args.repeat_client else None
        sets, tables, spend = run_pipeline(
            args.client, spec, args.llm, args.model, args.effort, profile=prof
        )
        meta = {"llm": args.llm, "model": args.model, "effort": args.effort,
                "repeat_client": args.repeat_client,
                "cost_usd": round(sum(s.cost_usd for s in spend), 4) if spend else 0.0}
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        p = dump_run(args.out / f"{args.client}-{stamp}.json", args.client, sets, meta)
        print(f"run cached -> {p}")

    # One post-processing order for both fresh runs and replays, so an A/B is
    # never comparing two different pipelines:
    #   complete (free inference) -> reconcile (exactly-once) -> verify.
    creps, vreps = {}, {}
    if not args.no_complete:
        from fsa.mapping.complete import complete_by_rollup, complete_by_siblings

        for st, ms in sets.items():
            c = complete_by_siblings(ms, tables[st], spec)
            if not args.no_rollup:
                r = complete_by_rollup(ms, tables[st], spec)
                c.completed += r.completed
                c.groups_used += r.groups_used
                c.groups_skipped_split += r.groups_skipped_split
                c.groups_skipped_thin += r.groups_skipped_thin
                c.detail.extend(r.detail)
            creps[st] = c
    for st, ms in sets.items():
        reconcile(ms, tables[st], spec)
    if not args.no_verify:
        from fsa.validate.mapping_checks import verify_mapping

        for st, ms in sets.items():
            vreps[st] = verify_mapping(ms, tables[st], spec)

    if creps and any(c.completed for c in creps.values()):
        print(f"\n{'=' * 74}\n  SIBLING COMPLETION (no API call)\n{'=' * 74}")
        for st, c in creps.items():
            for section, label, n in c.detail:
                print(f"    {st.value}  {section[:34]:<34} -> {label[:26]:<26} "
                      f"({n} rows)")
        tc = sum(c.completed for c in creps.values())
        print(f"    completed {tc} rows from "
              f"{sum(c.groups_used for c in creps.values())} homogeneous groups; "
              f"skipped {sum(c.groups_skipped_split for c in creps.values())} "
              f"split groups, "
              f"{sum(c.groups_skipped_thin for c in creps.values())} too thin, "
              f"{sum(c.groups_skipped_specific for c in creps.values())} specifically mapped (no bucket to join)")

    counts = burden(sets)
    truth = CLIENTS[args.client]["truth"]
    delivered = load_template(args.delivered) if args.delivered else None
    esc = escapes(sets, truth, spec, delivered)
    retypes = esc["retypes"] if esc else 0

    if vreps:
        print(f"\n{'=' * 74}\n  ARITHMETIC VERIFICATION (PLAN 7.3)\n{'=' * 74}")
        tot = VerifyTotals(vreps)
        print(f"    template blocks rebuilt and checked against the client: "
              f"{tot.checked}  ({tot.failed} did not reconcile)")
        print(f"    rows suppressed as computed results / grand totals: {tot.suppressed}")
        print(f"    rules contradicted -> forced to red: {tot.flagged}")
        print(f"    exclusions that drop money: {tot.unsafe}")
        for st, rep in vreps.items():
            for f in rep.findings:
                print(f"      [{f.severity.value.upper():<7}] {f.message}")

    total_rows = sum(counts[k] for k in ("red", "amber", "white", "green", "excl"))
    print(f"\n{'=' * 74}\n{args.client}: analyst review burden   {meta}\n{'=' * 74}")
    print(f"  {'rows on the review sheet':<44}{total_rows:>6}")
    for k, lbl in (("red", "RED   decide from scratch"),
                   ("amber", "AMBER verify a low-confidence proposal"),
                   ("white", "WHITE skim a confident proposal"),
                   ("green", "GREEN skim a confirmed row (profile/human)"),
                   ("excl", "EXCL  skim an auto-exclusion")):
        print(f"    {lbl:<42}{counts[k]:>6}")
    if retypes:
        print(f"    {'+ renames to retype (right row, wrong name)':<42}{retypes:>6}")

    print(f"\n  {'scenario':<20}{'minutes':>10}{'':>4}assumption")
    for sc, note in (("best", "proposals mostly land; fast reader"),
                     ("expected", "midpoint of the rate table"),
                     ("worst", "ordering helps, proposals do not: every")):
        m = minutes(counts, retypes, sc)
        print(f"  {sc:<20}{m:>10.0f}    {note}")
        if sc == "worst":
            print(f"  {'':<20}{'':>10}    unconfirmed row decided from scratch")

    rows, lo, hi = manual_baseline(tables)
    print(f"\n  manual baseline ({rows} data accounts, every one decided by hand):"
          f"  {lo:.0f}-{hi:.0f} min")
    exp = minutes(counts, retypes, "expected")
    wor = minutes(counts, retypes, "worst")
    print(f"  -> expected saving {100 * (1 - exp / ((lo + hi) / 2)):.0f}%,"
          f"  worst-case saving {100 * (1 - wor / ((lo + hi) / 2)):.0f}%")

    if esc is None:
        print("\n  (no ground truth for this client -- escape risk unmeasurable)")
        return 0

    fl_w, fl_r = esc["flagged_wrong"], esc["flagged_right"]
    un_w, un_r = esc["unflagged_wrong"], esc["unflagged_right"]
    print(f"\n{'-' * 74}\n  GUARD: money in the wrong block, on rows the sheet does NOT flag\n{'-' * 74}")
    print(f"    flagged   (RED/AMBER): {fl_r + fl_w:>3} scored rows, {fl_w:>3} misplaced")
    print(f"    unflagged (WHITE/GRN): {un_r + un_w:>3} scored rows, {un_w:>3} misplaced"
          f"   <- reaches a client")
    if un_r + un_w:
        print(f"    unflagged misplacement rate: {100 * un_w / (un_r + un_w):.0f}%")
    if esc["unknown_block"]:
        print(f"    ({esc['unknown_block']} rows had no resolvable block on one side; "
              f"scored on label instead)")
    ce = esc["cross_block_escapes"]
    print(f"\n    misplaced accounts (money in the wrong subtotal): {len(ce)}")
    for stv, acct, got, want, gb, wb_, tier, conf in ce:
        print(f"      [{tier:<5} {conf:.2f}] {stv} {acct[:30]:<30} "
              f"{got[:20]:<20} ({gb}) should be {want[:20]:<20} ({wb_})")

    print(f"\n    calibration -- is confidence usable for triage?")
    print(f"      {'conf':<8}{'placement':>14}{'name':>14}")
    for b in sorted(esc["buckets"], reverse=True):
        ok, n = esc["buckets"][b]
        nok, nn = esc["name_buckets"][b]
        print(f"      {b:<8.1f}{f'{ok}/{n} = {100*ok/n:.0f}%':>14}"
              f"{f'{nok}/{nn} = {100*nok/nn:.0f}%':>14}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
