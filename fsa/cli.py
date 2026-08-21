"""Scriptable entry point: python -m fsa.cli ingest ...

The ingest layer (fsa.ingest.*) is imported lazily, inside the command
function, so this module imports cleanly even while that layer is still being
built.
"""

from __future__ import annotations

import argparse
import csv
import sys
import warnings
from pathlib import Path

from fsa.consolidate import consolidate
from fsa.model.schema import Severity, StatementSet, StatementType, ValidationReport
from fsa.validate.checks import audit_against_reference, run_all
from fsa.validate.report import render_json, render_text


def _resolve_files(paths: list[str], excludes: list[str]) -> list[Path]:
    exclude_names = set(excludes)
    files: list[Path] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            for f in sorted(pp.glob("*.xlsx")):
                if f.name in exclude_names or f.name.startswith("~$"):
                    continue
                files.append(f)
        else:
            if pp.name in exclude_names:
                continue
            files.append(pp)
    return files


def _write_csv(csv_dir: Path, table) -> None:
    csv_dir.mkdir(parents=True, exist_ok=True)
    out_path = csv_dir / f"{table.statement.value}.csv"
    with out_path.open("w", newline="", encoding="ascii", errors="replace") as fh:
        writer = csv.writer(fh)
        writer.writerow(["label", "section", *[str(y) for y in table.years]])
        for row in table.rows:
            writer.writerow(
                [
                    row.raw_label,
                    row.section or "",
                    *["" if row.values.get(y) is None else row.values.get(y) for y in table.years],
                ]
            )


def _build_display_report(report: ValidationReport, verbose: bool) -> ValidationReport:
    """A non-mutating copy of `report` for the console text report only.

    `report` itself -- used for --json and the exit code -- is never
    mutated. Without -v, INFO findings (account added/removed/renamed -- the
    client's chart of accounts drifts every year, this is expected noise) are
    left out of the console text; --json always has everything regardless.
    render_text additionally collapses long comparative_disagreement runs to
    the top few per year on its own (see fsa/validate/report.py) -- that part
    is not gated on -v since --json is always the full-detail escape hatch.
    """
    display = ValidationReport()
    if verbose:
        display.findings = list(report.findings)
    else:
        display.findings = [f for f in report.findings if f.severity is not Severity.INFO]
    return display


def _cmd_ingest(args: argparse.Namespace) -> int:
    # Lazy import: the ingest layer is owned by a different agent and may not
    # exist yet at import time of this module.
    from fsa.ingest.discover import find_statement_sheet
    from fsa.ingest.extract import extract_reference_grid, extract_workbook

    import openpyxl

    files = _resolve_files(args.paths, args.exclude)

    ss = StatementSet()
    for f in files:
        for col in extract_workbook(f, statements=(StatementType.BS, StatementType.IS)):
            ss.add(col)

    tables = {
        StatementType.BS: consolidate(ss, StatementType.BS),
        StatementType.IS: consolidate(ss, StatementType.IS),
    }

    report = run_all(ss, tables)

    if args.audit_against:
        audit_path = Path(args.audit_against)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wb = openpyxl.load_workbook(audit_path, data_only=True)
        for statement in (StatementType.BS, StatementType.IS):
            sheet = find_statement_sheet(wb, statement)
            if sheet is None:
                continue
            reference_table = extract_reference_grid(audit_path, sheet, statement)
            report.extend(audit_against_reference(tables[statement], reference_table))

    display_report = _build_display_report(report, args.verbose)
    text = render_text(display_report, tables)
    sys.stdout.write(text)

    if args.json:
        Path(args.json).write_text(render_json(report, tables), encoding="ascii", errors="replace")

    if args.csv_dir:
        csv_dir = Path(args.csv_dir)
        for table in tables.values():
            _write_csv(csv_dir, table)

    return 0 if report.ok else 1


#: Console warning lists are truncated at this many entries; the full set is
#: always in findings.json, so nothing is lost by keeping the terminal legible.
_MAX_WARNINGS = 10


def _severity_line(report: ValidationReport) -> str:
    n_err, n_warn = len(report.errors), len(report.warnings)
    if n_err:
        return f"{n_err} error(s), {n_warn} warning(s) -- see findings.json"
    if n_warn:
        return f"no errors, {n_warn} warning(s) -- see findings.json"
    return "no errors, no warnings"


def _cmd_prepare(args: argparse.Namespace) -> int:
    """Everything up to the review workbook. Deliberately stops before writing."""
    from fsa.job import JobError, prepare

    try:
        res = prepare(
            args.inputs,
            Path(args.template),
            Path(args.job),
            client_name=args.client or "",
            profile_path=Path(args.profile) if args.profile else None,
            use_llm=not args.no_llm,
            excludes=args.exclude,
            scope=args.scope,
        )
    except JobError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    out = sys.stdout
    out.write(f"\nJob prepared in {res.job_dir}\n")
    for st, table in res.tables.items():
        out.write(
            f"  {st.value}: {len(table.rows)} rows, years "
            f"{', '.join(str(y) for y in table.years)}\n"
        )
    out.write(f"  validation: {_severity_line(res.report)}\n")
    if res.llm_note:
        out.write(f"  llm: {res.llm_note}\n")
    if res.conflicts:
        out.write(
            f"  {res.conflicts} double-count conflict(s) resolved automatically "
            f"-- the losing side is shown as an exclusion in the review sheet\n"
        )
    detail_gaps = res.n_unresolved - res.n_unresolved_subtotal
    out.write(
        f"  {res.n_accounts} row(s) to review: {res.n_proposed} proposed, "
        f"{res.n_excluded} auto-excluded, {detail_gaps} account(s) need mapping"
    )
    out.write(
        f" (+{res.n_unresolved_subtotal} client subtotal(s) left alone)\n"
        if res.n_unresolved_subtotal
        else "\n"
    )
    # The technical detail lives in findings.json; what reaches the terminal
    # is what a person can act on (`fsa.explain`).
    from fsa.explain import render_text, summarize

    summary = summarize(res.report)
    rendered = render_text(summary)
    if rendered.strip():
        out.write(rendered + "\n")

    if summary.ok:
        out.write(f"\nNext: open {res.review_path}\n")
        out.write("      correct column D, save, then run `build`.\n")
    else:
        out.write(
            f"\nThe review sheet was still written ({res.review_path}) so you can see"
            f"\nwhat was read, but resolve the items above before building a model.\n"
        )

    # Errors are worth a non-zero exit for scripting, but the review workbook is
    # still written -- an unbalanced year is exactly what a human needs to see.
    return 1 if not res.report.ok else 0


def _cmd_build(args: argparse.Namespace) -> int:
    """Reviewed workbook -> populated BVAL model."""
    from fsa.job import JobError, build

    try:
        res = build(
            Path(args.job),
            Path(args.out),
            save_profile_to=Path(args.save_profile) if args.save_profile else None,
            overwrite=args.overwrite,
            allow_unconfirmed=args.allow_unconfirmed,
            visible=args.visible,
        )
    except JobError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    stats, wr = res.stats, res.write_report
    out = sys.stdout
    out.write(f"\nReview read back: {stats.total} row(s) -- ")
    out.write(
        f"{stats.accepted} accepted, {stats.changed} changed, "
        f"{stats.excluded} excluded, {stats.unresolved_left} still unresolved\n"
    )
    for note in stats.notes:
        out.write(f"  note: {note}\n")

    # An unresolved row is the expected consequence of leaving it blank in
    # review, not an anomaly -- listing all 89 of them buries the handful of
    # warnings that mean something. Count those, print the rest.
    for st, plan in res.plans.items():
        skipped = [w for w in plan.warnings if w.startswith("unresolved,")]
        other = [w for w in plan.warnings if not w.startswith("unresolved,")]
        out.write(
            f"  {st.value}: {len(plan.actions)} row action(s), "
            f"{len(plan.inserts)} insert(s)"
        )
        out.write(f", {len(skipped)} left unmapped\n" if skipped else "\n")
        for w in other[:_MAX_WARNINGS]:
            out.write(f"    {w}\n")
        if len(other) > _MAX_WARNINGS:
            out.write(f"    ... and {len(other) - _MAX_WARNINGS} more (see findings.json)\n")

    out.write(f"\nWrote {wr.out_path}\n")
    if wr.balance_check:
        for year, diff in sorted(wr.balance_check.items()):
            flag = "OK" if abs(diff) < 0.01 else f"OUT BY {diff:,.2f}"
            out.write(f"  balance check {year}: {flag}\n")
    # write_model folds each plan's warnings into its own report, so skip the
    # ones already counted per-statement above and show only what is new here.
    fresh = [w for w in wr.warnings if not w.startswith("unresolved,")]
    for w in fresh[:_MAX_WARNINGS]:
        out.write(f"  warning: {w}\n")
    if len(fresh) > _MAX_WARNINGS:
        out.write(f"  ... and {len(fresh) - _MAX_WARNINGS} more warning(s)\n")
    if res.profile_path:
        out.write(f"  profile saved: {res.profile_path}\n")

    # Silence here would read as success. A model missing accounts is not a
    # finished model, however cleanly it wrote.
    if stats.unresolved_left:
        out.write(
            f"\nINCOMPLETE: {stats.unresolved_left} account(s) were left blank in "
            f"review and are absent from the model. Fill them in and re-run `build`.\n"
        )
        return 1

    return 0 if wr.balanced else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m fsa.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "prepare",
        help="Ingest, validate, map and produce the review workbook.",
    )
    p.add_argument("inputs", nargs="+", help="Client files or folders (.pdf/.xlsx/.ods).")
    p.add_argument("--template", required=True, metavar="BVAL.xlsx", help="Blank BVAL model template.")
    p.add_argument("--job", required=True, metavar="DIR", help="Job directory to create.")
    p.add_argument("--client", default=None, metavar="NAME", help="Client name for the model header.")
    p.add_argument("--profile", default=None, metavar="P.yaml", help="Last year's saved profile.")
    p.add_argument("--no-llm", action="store_true", help="Deterministic layers only; no API calls.")
    p.add_argument("--scope", default=None, metavar="ENTITY",
                   help="Which entity to value when a workbook holds several "
                        "(e.g. 'consolidated'). Matched against the tab name.")
    p.add_argument("--exclude", action="append", default=[], metavar="NAME", help="Filename to skip (repeatable).")
    p.set_defaults(func=_cmd_prepare)

    p = sub.add_parser("build", help="Apply the reviewed workbook and write the BVAL model.")
    p.add_argument("--job", required=True, metavar="DIR", help="Job directory from `prepare`.")
    p.add_argument("--out", required=True, metavar="MODEL.xlsx", help="Where to write the populated model.")
    p.add_argument("--save-profile", default=None, metavar="P.yaml", help="Persist confirmed decisions for next year.")
    p.add_argument("--overwrite", action="store_true", help="Replace --out if it already exists.")
    p.add_argument("--allow-unconfirmed", action="store_true", help="Write rows the analyst never confirmed (unsafe).")
    p.add_argument("--visible", action="store_true", help="Show Excel while writing (debugging).")
    p.set_defaults(func=_cmd_build)

    p = sub.add_parser("ingest", help="Ingest client workbooks and validate them.")
    p.add_argument("paths", nargs="+", help="Directories or .xlsx files to ingest.")
    p.add_argument("--exclude", action="append", default=[], metavar="NAME", help="Filename to skip (repeatable).")
    p.add_argument("--audit-against", default=None, metavar="FILE", help="Reference consolidation workbook.")
    p.add_argument("--json", default=None, metavar="OUT.json", help="Write the JSON report here.")
    p.add_argument("--csv-dir", default=None, metavar="DIR", help="Write BS.csv / IS.csv of the consolidated grid here.")
    p.add_argument("-v", "--verbose", action="store_true", help="Include INFO findings in the text report.")
    p.set_defaults(func=_cmd_ingest)

    return parser


def main(argv: list[str] | None = None) -> int:
    from fsa.env import load_dotenv

    load_dotenv()  # so --llm works without the operator exporting anything
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
