"""Score the mapping by the only thing that actually matters: the numbers.

Label matching penalises differences that change nothing. Mapping the rollup
`Total Checking/Savings -> Cash` and mapping its three children individually to
`Cash` put identical money on an identical line, yet strict scoring counts the
first as the answer and the second as a miss. Naming is worse still: two thirds
of Commercial Flooring's ground-truth targets are names the analyst invented.

So compare *block subtotals* instead. If our mapping reproduces the delivered
model's Total Current Assets, Operating Expenses, Pre-Tax Income and so on, the
valuation is right, whatever we called the lines or whichever altitude we
attached to. This metric is naming-independent, level-independent, and it is the
one an analyst would actually care about.
"""
from __future__ import annotations

import sys
import warnings
from collections import defaultdict

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import load_workbook

from fsa.consolidate import consolidate
from fsa.ingest.interpret import interpret, read_any
from fsa.ingest.extract import extract_workbook
from fsa.mapping.complete import complete_by_rollup, complete_by_siblings
from fsa.mapping.llm import propose_unresolved
from fsa.mapping.matcher import propose
from fsa.mapping.reconcile import reconcile
from fsa.mapping.template import load_template
from fsa.validate.mapping_checks import verify_mapping
from fsa.model.mapping import Decider
from fsa.model.schema import StatementSet, StatementType

warnings.simplefilter("ignore")
REPO = Path(__file__).resolve().parents[1]
S1 = REPO / "Konrad Project" / "sample_1_TS"
S2 = REPO / "Konrad Project" / "sample_2_flooring"
BLANK = load_template(S1 / "BVAL Model (Weaver Template).xlsx")

CLIENTS = {
    "FLOOR": (S2 / "orig_pdfs", S2 / "Draft Schedules - Commercial Flooring, Inc. (12.31.25)v2.xlsx"),
    "TS": (S1 / "TS Distributors Financial Statements (from client)", S1 / "TS Distributors, Inc. BVAL Model.xlsx"),
}
TOL = 1.0  # dollars


def load_clients(path):
    ss = StatementSet()
    files = sorted(p for p in path.iterdir() if p.suffix.lower() in (".pdf", ".xlsx", ".ods"))
    for p in files:
        if "Weaver edited" in p.name:
            continue
        if p.suffix.lower() == ".xlsx":
            cols = list(extract_workbook(p))
            if cols:
                for c in cols:
                    ss.add(c)
                continue
        for c in interpret(read_any(p))[0]:
            ss.add(c)
    return ss


#: A reference to a cell on the *same* tab, i.e. an analyst adjustment computed
#: inside the model rather than a client figure. TS nets amortization into its
#: intangibles this way (`='Historical BS'!G34-K111`), so the delivered subtotal
#: is ingestion PLUS judgement -- and PLAN 2.5 requires those stay separate. A
#: block built from any such line is reported apart, never scored as a mapping
#: failure, because reproducing it is not this layer's job.
_SELF_REF = __import__("re").compile(r"(?<![!$A-Z0-9])\$?[A-Z]{1,2}\$?\d+")


def _adjusted_blocks(model_path, spec):
    wbf = load_workbook(model_path, data_only=False)
    out = set()
    for tab in ("BS", "IS"):
        if tab not in wbf.sheetnames:
            continue
        ws = wbf[tab]
        st = StatementType[tab]
        for b in [x for x in spec.blocks if x.statement is st]:
            for r in range(b.first_row, b.last_row + 1):
                for col in "EFGHIJK":
                    f = ws[f"{col}{r}"].value
                    if not isinstance(f, str) or not f.startswith("="):
                        continue
                    if r == b.subtotal_row:
                        continue  # the block's own SUM() is not an adjustment
                    re_ = __import__("re")
                    stripped = re_.sub(r"'[^']*'!\$?[A-Z]{1,2}\$?\d+(?::\$?[A-Z]{1,2}\$?\d+)?", "", f)
                    stripped = re_.sub(r"[A-Za-z]\w*!\$?[A-Z]{1,2}\$?\d+(?::\$?[A-Z]{1,2}\$?\d+)?", "", stripped)
                    # An adjustment is ingestion *plus* a figure computed
                    # inside the model (="Historical BS"!G34-K111). Source-
                    # only is pure ingestion; self-only is a derived line.
                    if stripped != f and _SELF_REF.search(stripped):
                        out.add((tab, b.subtotal_label))
    return out


def delivered_blocks(model_path):
    """{(statement, block_label): {year: subtotal value}} from the delivered model."""
    spec = load_template(model_path)
    wb = load_workbook(model_path, data_only=True)
    out = defaultdict(dict)
    for tab in ("BS", "IS"):
        if tab not in wb.sheetnames:
            continue
        ws = wb[tab]
        years = {}
        for col in "EFGHIJK":
            v = ws[f"{col}8"].value or ws[f"{col}10"].value
            s = str(v or "")
            digits = "".join(ch for ch in s if ch.isdigit())
            if len(digits) >= 4:
                years[col] = int(digits[:4])
        st = StatementType[tab]
        for b in [x for x in spec.blocks if x.statement is st]:
            for col, yr in years.items():
                val = ws[f"{col}{b.subtotal_row}"].value
                if isinstance(val, (int, float)):
                    out[(tab, b.subtotal_label)][yr] = float(val)
    return out


def our_blocks(ss, use_llm):
    out = defaultdict(lambda: defaultdict(float))
    for st in (StatementType.BS, StatementType.IS):
        table = consolidate(ss, st)
        ms = propose(table, BLANK, profile=None)
        if use_llm:
            propose_unresolved(ms, table, BLANK)
        # Same post-processing order as `tools/burden.py`. Two eval tools
        # running two different pipelines is how a project ends up with two
        # contradictory numbers and no way to tell which is true, so the
        # sequence lives in one place: complete -> reconcile -> verify.
        complete_by_siblings(ms, table, BLANK)
        complete_by_rollup(ms, table, BLANK)
        reconcile(ms, table, BLANK)
        verify_mapping(ms, table, BLANK)
        for r in ms.rules:
            if r.decided_by is Decider.UNRESOLVED:
                continue
            line = BLANK.find(st, r.target.label)
            block = (line.block if line is not None else None) or r.target.block
            if not block:
                continue
            row = table.find(r.norm_account)
            if row is None:
                continue
            for yr, v in row.values.items():
                if isinstance(v, (int, float)):
                    out[(st.value, block)][yr] += r.sign * v
    return out


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "FLOOR"
    use_llm = "--llm" in sys.argv
    clients, model = CLIENTS[name]
    ss = load_clients(clients)
    want = delivered_blocks(model)
    got = our_blocks(ss, use_llm)
    adjusted = _adjusted_blocks(model, load_template(model))
    have_years = set()
    for st in (StatementType.BS, StatementType.IS):
        have_years |= set(ss.years(st))

    hit = tot = skipped_adj = skipped_year = 0
    print(f"\n{name}: block subtotal reproduction (tolerance ${TOL})\n" + "=" * 78)
    for key in sorted(want, key=lambda k: (k[0], k[1])):
        tab, block = key
        for yr in sorted(want[key]):
            w = want[key][yr]
            if abs(w) < TOL:
                continue  # nothing to reproduce
            if yr not in have_years:
                skipped_year += 1
                continue  # the client never sent us this year
            if key in adjusted:
                skipped_adj += 1
                continue  # ingestion + analyst judgement; not comparable
            g = got.get(key, {}).get(yr)
            tot += 1
            ok = g is not None and abs(g - w) <= max(TOL, abs(w) * 0.001)
            hit += ok
            if not ok:
                d = "missing" if g is None else f"{g - w:+,.0f}"
                print(f"  {tab} {block[:34]:<34} {yr}  want {w:>14,.0f}  {d}")
    print("=" * 78)
    print(f"  reproduced {hit}/{tot} = {100*hit/max(1,tot):.0f}% of comparable block subtotals")
    print(f"  excluded: {skipped_adj} block-years carry analyst adjustments (PLAN 2.5), "
          f"{skipped_year} are years the client never sent")


if __name__ == "__main__":
    main()
