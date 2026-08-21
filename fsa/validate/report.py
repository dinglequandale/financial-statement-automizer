"""Render a ValidationReport as ASCII text or JSON.

Console here is cp1252 and will raise UnicodeEncodeError on anything outside
ASCII, so every string that reaches render_text is scrubbed defensively.
"""

from __future__ import annotations

import json

from fsa.model.schema import (
    CellRef,
    ConsolidatedTable,
    Finding,
    Severity,
    StatementType,
    ValidationReport,
)

_SEVERITY_ORDER = (Severity.ERROR, Severity.WARNING, Severity.INFO)

# How many comparative_disagreement findings to print per (statement, year)
# before collapsing the rest to a count. The client restates prior periods
# constantly (see SPEC-PHASE0.md "Prior-period restatements") -- ~20 in a
# single year is normal -- so without this the one $150,000 move can get
# buried under nineteen $60 ones. This only trims what is *printed*; the
# section header count and --json always reflect every finding.
COMPARATIVE_DISPLAY_TOP_N = 5


def _ascii(s: str) -> str:
    return s.encode("ascii", "replace").decode("ascii")


def _finding_sort_key(f: Finding):
    stmt = f.statement.value if f.statement is not None else ""
    year = f.fiscal_year if f.fiscal_year is not None else -1
    if f.code == "comparative_disagreement":
        # The client restates prior periods constantly (see SPEC-PHASE0.md
        # "Prior-period restatements"); with ~20 of these in a single year,
        # the one $150,000 move must not get buried under nineteen $60 ones.
        delta = f.detail.get("delta")
        rank = -abs(delta) if delta is not None else 0.0
        return (stmt, year, f.code, rank, f.account or "")
    return (stmt, year, f.code, 0.0, f.account or "")


def _format_finding(f: Finding) -> str:
    parts = [f"[{f.code}]"]
    if f.statement is not None:
        parts.append(f.statement.value)
    if f.fiscal_year is not None:
        parts.append(f"FY{f.fiscal_year}")
    if f.account:
        parts.append(f"account={f.account}")
    line = " ".join(parts) + f": {f.message}"
    delta = f.detail.get("delta")
    if delta is not None and f.code in ("comparative_disagreement", "value_delta", "comparative_alignment_suspected"):
        line += f" [delta={delta:+.2f}]"
    if f.ref is not None:
        line += f" ({f.ref})"
    return line


def _collapse_for_listing(items: list[Finding], top_n: int = COMPARATIVE_DISPLAY_TOP_N) -> list[Finding]:
    """Within one severity group, collapse comparative_disagreement findings
    to the top N per (statement, year) by absolute delta, replacing the rest
    with a single count line. Purely a display trim: the section header
    count is computed from the untouched `items` before this runs."""
    groups: dict[tuple, list[Finding]] = {}
    other: list[Finding] = []
    for f in items:
        if f.code == "comparative_disagreement":
            groups.setdefault((f.statement, f.fiscal_year), []).append(f)
        else:
            other.append(f)
    if not groups:
        return items

    result = list(other)
    for (stmt, year), group_items in groups.items():
        ranked = sorted(group_items, key=lambda f: abs(f.detail.get("delta") or 0.0), reverse=True)
        shown = ranked[:top_n]
        result.extend(shown)
        remaining = len(ranked) - len(shown)
        if remaining > 0:
            result.append(
                Finding(
                    severity=Severity.WARNING,
                    code="comparative_disagreement",
                    message=(
                        f"...and {remaining} more comparative disagreement(s) not shown here "
                        "-- see the JSON report for the full list"
                    ),
                    statement=stmt,
                    fiscal_year=year,
                )
            )
    return result


def render_text(report: ValidationReport, table: dict[StatementType, ConsolidatedTable]) -> str:
    lines: list[str] = []

    n_err = len(report.errors)
    n_warn = len(report.warnings)
    if n_err == 0 and n_warn == 0:
        lines.append("OK")
    else:
        lines.append(f"{n_err} error(s), {n_warn} warning(s)")
    lines.append("")

    lines.append("=== SUMMARY ===")
    for stmt in (StatementType.BS, StatementType.IS):
        t = table.get(stmt)
        if t is None:
            continue
        years_desc = f"{t.years[0]}-{t.years[-1]}" if t.years else "none"
        lines.append(f"{stmt.value}: years {years_desc}, {len(t.rows)} accounts")
    lines.append("")

    for sev in _SEVERITY_ORDER:
        items = [f for f in report.findings if f.severity is sev]
        if not items:
            continue
        lines.append(f"=== {sev.value.upper()} ({len(items)}) ===")
        for f in sorted(_collapse_for_listing(items), key=_finding_sort_key):
            lines.append(_format_finding(f))
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    return _ascii(text)


def _ref_to_dict(ref: CellRef | None):
    if ref is None:
        return None
    return {"file": str(ref.file), "sheet": ref.sheet, "cell": ref.cell}


def _finding_to_dict(f: Finding) -> dict:
    return {
        "severity": f.severity.value,
        "code": f.code,
        "message": f.message,
        "statement": f.statement.value if f.statement is not None else None,
        "fiscal_year": f.fiscal_year,
        "account": f.account,
        "ref": _ref_to_dict(f.ref),
        "detail": f.detail,
    }


def _table_to_dict(t: ConsolidatedTable) -> dict:
    return {
        "years": list(t.years),
        "rows": [
            {
                "label": r.raw_label,
                "norm_label": r.norm_label,
                "section": r.section,
                "kind": r.kind.value,
                "values": {str(y): v for y, v in r.values.items()},
            }
            for r in t.rows
        ],
    }


def render_json(report: ValidationReport, table: dict[StatementType, ConsolidatedTable]) -> str:
    payload = {
        "verdict": "ok" if report.ok else "error",
        "error_count": len(report.errors),
        "warning_count": len(report.warnings),
        "findings": [_finding_to_dict(f) for f in report.findings],
        "tables": {stmt.value: _table_to_dict(t) for stmt, t in table.items()},
    }
    return json.dumps(payload, indent=2, default=str, ensure_ascii=True)
