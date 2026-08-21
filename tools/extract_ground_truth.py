"""Recover the expert mapping decisions embedded in a delivered BVAL model.

The delivered TS Distributors model encodes a complete human mapping in its
formulas: `BS!K11 = SUM('Historical BS'!G18:G21, G27)` is an analyst saying
"these five client accounts are Inventory". Parsing those references back out
gives us a labeled dataset -- the only ground truth we have for how a Weaver
analyst actually maps a chart of accounts.

Phase 1 uses this to *measure* the mapping engine rather than guess at it:
run the mapper blind over the client's account names, score against these
labels, and set the auto-accept confidence threshold from the result.

Usage:
    python tools/extract_ground_truth.py <delivered-model.xlsx> [-o out.yaml]
"""

from __future__ import annotations

import argparse
import re
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import openpyxl

# 'Historical BS'!G18:G21  |  '20 FS'!C57  |  'Dec 25 BS'!C9  |  IS!E1
#
# Deliberately matches *any* sheet name rather than a fixed one. Weaver does not
# lay the history out the same way twice: TS pasted every year onto two tabs
# (`Historical BS`/`Historical IS`), while Commercial Flooring kept one tab per
# year (`20 FS` ... `24 FS`, `Dec 25 BS`, `Dec 25 P&L`). A source sheet is
# therefore *whatever a standardized-tab formula points at*, minus the model's
# own tabs -- which needs no per-engagement configuration at all.
_REF = re.compile(
    r"(?:'([^']+)'|([A-Za-z][A-Za-z0-9_.]*))!"
    r"\$?([A-Z]{1,2})\$?(\d+)"
    r"(?::\$?[A-Z]{1,2}\$?(\d+))?"
)

#: Standardized/derived tabs a BS or IS formula may cite that are *not* client
#: history. Anything else it cites is treated as a source of client accounts.
_NOT_SOURCES = {"BS", "IS", "ADJ", "PROJ", "INPUTS", "LINKS", "DEPREC", "NWC"}

# Value columns on the standardized tabs, earliest historical year -> valuation year.
_YEAR_COLUMNS = ("E", "F", "G", "H", "I", "J", "K")

# Column holding the template's own line label on the standardized BS/IS tabs.
_LABEL_COLUMN = "C"


def _load(path: Path) -> openpyxl.Workbook:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return openpyxl.load_workbook(path, data_only=False)


def _account_labels(wb: openpyxl.Workbook, sheet: str) -> dict[int, str]:
    """Row number -> client account label on a pasted history tab.

    The label column is discovered, not assumed: TS put account names in column
    A, Commercial Flooring in column B (with print furniture in A). Whichever of
    the first three columns carries the most text wins.
    """
    if sheet not in wb.sheetnames:
        return {}
    ws = wb[sheet]
    best_col, best_n = 1, -1
    for col in (1, 2, 3):
        n = sum(
            1
            for r in range(1, min(ws.max_row, 400) + 1)
            if isinstance(ws.cell(r, col).value, str) and ws.cell(r, col).value.strip()
        )
        if n > best_n:
            best_col, best_n = col, n
    out: dict[int, str] = {}
    for r in range(1, ws.max_row + 1):
        v = ws.cell(r, best_col).value
        if isinstance(v, str) and v.strip():
            out[r] = v.strip()
    return out


def _cited_rows(formula: str | None) -> list[tuple[str, int, int]]:
    """Every Historical-tab row a formula reads, with its sign.

    Sign is recovered from whether the reference sits inside a subtracted term:
    `=A + B - SUM(C:D)` means C and D are contra items. This matters -- the
    delivered model subtracts state taxes and penalties inside Other Income.
    """
    if not isinstance(formula, str) or not formula.startswith("="):
        return []

    out: list[tuple[str, int, int]] = []
    for m in _REF.finditer(formula):
        quoted, bare, _col, r1, r2 = m.groups()
        sheet = quoted or bare
        if sheet.strip().upper() in _NOT_SOURCES:
            continue
        start, end = int(r1), int(r2) if r2 else int(r1)

        # Walk backwards from the match for the nearest +/- at paren depth 0
        # relative to the reference, treating a leading '=' as positive.
        sign = 1
        depth = 0
        for ch in reversed(formula[: m.start()]):
            if ch == ")":
                depth += 1
            elif ch == "(":
                if depth == 0:
                    break  # start of enclosing call; its own sign handled below
                depth -= 1
            elif depth == 0 and ch in "+-=,":
                sign = -1 if ch == "-" else 1
                break

        # A reference inside SUM(...) inherits the sign of the SUM call itself.
        head = formula[: m.start()]
        call = head.rfind("SUM(")
        if call != -1 and head.count("(", call) > head.count(")", call):
            for ch in reversed(head[:call]):
                if ch in "+-=,":
                    sign = -1 if ch == "-" else 1
                    break

        for r in range(start, end + 1):
            out.append((sheet.replace("  ", " "), r, sign))
    return out


def extract(path: Path) -> dict:
    wb = _load(path)
    labels = {s: _account_labels(wb, s) for s in wb.sheetnames}

    mappings: list[dict] = []
    year_variant: list[dict] = []

    for tab in ("BS", "IS"):
        if tab not in wb.sheetnames:
            continue
        ws = wb[tab]
        for r in range(1, ws.max_row + 1):
            target = ws[f"{_LABEL_COLUMN}{r}"].value
            if not isinstance(target, str) or not target.strip():
                continue
            target = target.strip()

            # Collect per-year reference sets so we can spot mappings the
            # analyst changed partway through the history.
            per_year: dict[str, list[tuple[str, str, int]]] = {}
            for col in _YEAR_COLUMNS:
                cited = _cited_rows(ws[f"{col}{r}"].value)
                if not cited:
                    continue
                resolved = []
                for sheet, row, sign in cited:
                    lab = labels.get(sheet, {}).get(row)
                    if lab:
                        resolved.append((sheet, lab, sign))
                if resolved:
                    per_year[col] = resolved

            if not per_year:
                continue

            signatures = {tuple(sorted((l, s) for _, l, s in v)) for v in per_year.values()}
            if len(signatures) > 1:
                year_variant.append(
                    {
                        "statement": tab,
                        "template_line": target,
                        "variants": {
                            col: sorted({f"{l} ({'+' if s > 0 else '-'})" for _, l, s in v})
                            for col, v in per_year.items()
                        },
                    }
                )

            # Canonical mapping = the most recent year's formula, which reflects
            # the analyst's final decision.
            latest = per_year[max(per_year, key=lambda c: _YEAR_COLUMNS.index(c))]
            seen: set[str] = set()
            for _sheet, lab, sign in latest:
                if lab in seen:
                    continue
                seen.add(lab)
                mappings.append(
                    {
                        "statement": tab,
                        "client_account": lab,
                        "template_line": target,
                        "sign": sign,
                    }
                )

    by_target: dict[str, list[str]] = defaultdict(list)
    for m in mappings:
        by_target[f"{m['statement']}::{m['template_line']}"].append(m["client_account"])

    contested = {
        acct: sorted({m["template_line"] for m in mappings if m["client_account"] == acct})
        for acct in {m["client_account"] for m in mappings}
    }
    contested = {k: v for k, v in contested.items() if len(v) > 1}

    return {
        "source": path.name,
        "mappings": mappings,
        "targets": dict(by_target),
        "year_variant_mappings": year_variant,
        "contested_accounts": contested,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    args = ap.parse_args()

    if not args.model.is_file():
        print(f"not found: {args.model}", file=sys.stderr)
        return 2

    data = extract(args.model)
    m = data["mappings"]
    negatives = [x for x in m if x["sign"] < 0]

    print(f"source                : {data['source']}")
    print(f"labeled pairs         : {len(m)}")
    print(f"template lines covered: {len(data['targets'])}")
    print(f"contra (subtracted)   : {len(negatives)}")
    for x in negatives:
        print(f"    -{x['client_account']}  ->  {x['template_line']}")
    print(f"year-variant mappings : {len(data['year_variant_mappings'])}")
    for v in data["year_variant_mappings"]:
        print(f"    {v['statement']} {v['template_line']}")
        for col, accts in v["variants"].items():
            print(f"        {col}: {', '.join(accts)}")
    print(f"contested accounts    : {len(data['contested_accounts'])}")
    for a, targets in data["contested_accounts"].items():
        print(f"    {a} -> {targets}")

    if args.out:
        try:
            import yaml
        except ImportError:
            print("pyyaml not installed; skipping write", file=sys.stderr)
            return 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
