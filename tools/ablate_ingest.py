"""Does the model layer in ingest earn its place? Re-ask this on every client.

Runs each sample client twice through the *current* pipeline -- once with the
period resolver's model enabled, once with it switched off so only the
deterministic layer speaks -- and diffs the columns, the consolidated grid and
the errors. Everything else (scope selection, page joining, the hardened
structural parser) is identical in both arms, so any difference is
attributable to the model alone.

Worth running whenever a new client lands, because the honest answer changes
by client and it is easy to credit a model for work the regexes did:

    TS      0 calls, output identical
    FLOOR   0 calls, output identical
    AGAM   12 calls, income statement FY2025 taken from the right entity at
           the right date instead of the wrong entity at a fabricated one

The pattern to watch for is a client where the model is never invoked. That is
not a failure -- it means the structural layer covered the client's phrasing --
but a run of them is evidence the fallback could be dropped, and a client with
many calls is evidence it is carrying real weight.

Usage:
    python tools/ablate_ingest.py            # every configured client
    python tools/ablate_ingest.py AGAM       # just one
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

warnings.simplefilter("ignore")

from burden import CLIENTS  # noqa: E402

from fsa.consolidate import consolidate  # noqa: E402
from fsa.env import load_dotenv  # noqa: E402
from fsa.ingest.period import PeriodResolver  # noqa: E402
from fsa.job import ingest, resolve_inputs  # noqa: E402
from fsa.model.schema import ColumnRole, StatementType  # noqa: E402

load_dotenv()


def run(name: str, use_llm: bool) -> dict:
    cfg = CLIENTS[name]
    files = resolve_inputs([str(cfg["files"])], sorted(cfg["exclude"]))
    r = PeriodResolver(use_llm=use_llm)
    ss, findings = ingest(files, resolver=r, scope=cfg.get("scope"))

    cols = {
        (c.statement.value, c.fiscal_year): (
            str(c.period_end), c.period_months, c.entity, len(c.rows)
        )
        for c in ss.columns
        if c.role is ColumnRole.PRIMARY
    }
    rows = {}
    for st in (StatementType.BS, StatementType.IS):
        t = consolidate(ss, st)
        rows[st.value] = (len(t.rows), t.years)
    errs = sorted(f.code for f in findings if f.severity.value == "error")
    return {"cols": cols, "rows": rows, "errs": errs, "calls": r.calls}


def report(name: str) -> None:
    off, on = run(name, False), run(name, True)

    print("=" * 78)
    print(f"  {name}    model calls when enabled: {on['calls']}")
    print("=" * 78)
    if on["calls"] == 0:
        print("  the structural layer read every caption; the model was never asked.")

    same = off["rows"] == on["rows"]
    print(f"  grid   off = {off['rows']}")
    print(f"         on  = {on['rows']}   {'(identical)' if same else '<-- DIFFERS'}")

    if off["errs"] == on["errs"]:
        print(f"  errors identical: {off['errs'] or 'none'}")
    else:
        print(f"  errors off = {off['errs']}")
        print(f"         on  = {on['errs']}")

    diffs = [
        k for k in sorted(set(off["cols"]) | set(on["cols"]))
        if off["cols"].get(k) != on["cols"].get(k)
    ]
    if not diffs:
        print("  columns identical")
    else:
        print(f"  columns {len(diffs)} differ  (end, months, entity, rows):")
        for k in diffs:
            print(f"      {k[0]} FY{k[1]}")
            print(f"         off: {off['cols'].get(k)}")
            print(f"         on : {on['cols'].get(k)}")
    print()


if __name__ == "__main__":
    wanted = sys.argv[1:] or list(CLIENTS)
    for client in wanted:
        report(client)
