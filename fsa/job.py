"""One engagement, start to finish: files in, reviewed BVAL model out.

Every piece below this module was built and tested in isolation, and that was
the problem -- only the test suite could run them. This is the seam that turns a
library into a tool, and it is deliberately thin: it sequences existing calls
and persists just enough between them, adding no mapping logic of its own.

The flow is split into two commands rather than one, because the human step in
the middle is not optional (PLAN.md 10). `prepare` does everything up to the
review workbook and stops; `build` picks up the reviewed workbook and writes the
model. Between the two, state lives in a *job directory*:

    job/
      job.json        what was ingested, from where, against which template
      proposed.yaml   the machine's mapping, as a ClientProfile
      review.xlsx     the analyst's copy -- the only file they ever touch
      findings.json   the validation report from ingest

`proposed.yaml` reuses the profile serializer rather than inventing a job
format, so the proposal and the saved-for-next-year profile are the same shape
on disk and diff against each other directly.

The consolidated tables are *not* persisted. `build` re-ingests from the paths
in `job.json`, which is safe because ingest is deterministic and has no LLM in
it -- and it means a job directory stays small and readable instead of carrying
a pickled snapshot of the client's figures.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from fsa.consolidate import consolidate
from fsa.model.schema import (
    ExtractedColumn,
    Finding,
    Severity,
    StatementSet,
    StatementType,
    ValidationReport,
)

#: Formats the reader seam can open. Anything else in a dropped-in folder is
#: ignored rather than treated as an error -- clients send read-me's and logos.
READABLE = {".pdf", ".xlsx", ".xls", ".ods"}

JOB_FILE = "job.json"
PROPOSED_FILE = "proposed.yaml"
REVIEW_FILE = "review.xlsx"
FINDINGS_FILE = "findings.json"

STATEMENTS = (StatementType.BS, StatementType.IS)


class JobError(RuntimeError):
    """A condition the operator has to resolve; never a bug report."""


# --------------------------------------------------------------------------
# Input resolution and ingest
# --------------------------------------------------------------------------


def resolve_inputs(paths: list[str], excludes: list[str] | None = None) -> list[Path]:
    """Expand directories, drop what we cannot or should not read.

    Excel lock files (`~$foo.xlsx`) are skipped unconditionally: they appear
    whenever the client has the workbook open, and they are not workbooks.
    """
    skip = set(excludes or [])
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.is_file() and f.suffix.lower() in READABLE:
                    out.append(f)
        elif p.exists():
            out.append(p)
        else:
            raise JobError(f"input not found: {p}")

    keep = [
        f
        for f in out
        if f.name not in skip
        and not f.name.startswith("~$")
        and f.suffix.lower() in READABLE
    ]
    if not keep:
        raise JobError(
            f"no readable files found (looked for {', '.join(sorted(READABLE))})"
        )
    return keep


def ingest(
    files: list[Path], *, resolver=None, scope: str | None = None
) -> tuple[StatementSet, list[Finding]]:
    """Read every file into one StatementSet, collecting findings.

    `.xlsx` gets the dedicated extractor first, since it understands the
    multi-column comparative layout that a spreadsheet can have and a printed
    statement cannot. When that finds nothing -- a workbook converted from a PDF
    often has no recognisable header grid -- we fall back to the same reader
    seam every other format goes through, rather than declaring the file
    unreadable.

    Reading the files is only half of it. A workbook can hold several tabs that
    each claim the same fiscal year, and a client can hand over a stub period
    that looks annual; both are settled here, once, before anything downstream
    assumes one column per year (`fsa.ingest.scope`).
    """
    from fsa.ingest.extract import extract_workbook
    from fsa.ingest.interpret import interpret, read_any
    from fsa.ingest.raw import UnreadableSource
    from fsa.ingest.scope import (
        check_period_basis,
        check_period_coverage,
        infer_missing_period_ends,
        merge_pages,
        select_scope,
    )

    ss = StatementSet()
    findings: list[Finding] = []

    for f in files:
        columns: list[ExtractedColumn] = []
        if f.suffix.lower() in (".xlsx", ".xls"):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    columns = list(extract_workbook(f, statements=STATEMENTS))
            except Exception as exc:  # noqa: BLE001 - fall through to the seam
                findings.append(
                    Finding(
                        severity=Severity.INFO,
                        code="extractor_declined",
                        message=f"{f.name}: dedicated .xlsx extractor failed ({exc}); using the generic reader",
                    )
                )

        if not columns:
            try:
                columns, more = interpret(read_any(f), resolver=resolver)
                findings.extend(more)
            except UnreadableSource as exc:
                findings.append(
                    Finding(
                        severity=Severity.WARNING,
                        code="source_unreadable",
                        message=f"{f.name}: {exc}",
                    )
                )
                continue

        if not columns:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="no_statements_found",
                    message=f"{f.name}: no balance sheet or income statement recognised; skipped",
                )
            )
            continue

        for col in columns:
            ss.add(col)

    if not ss.columns:
        raise JobError(
            "nothing was ingested -- no file yielded a balance sheet or income statement"
        )

    findings.extend(merge_pages(ss))
    findings.extend(select_scope(ss, scope))
    findings.extend(infer_missing_period_ends(ss))
    findings.extend(check_period_basis(ss))
    findings.extend(check_period_coverage(ss))
    return ss, findings


def consolidate_all(ss: StatementSet) -> dict[StatementType, "object"]:
    """Consolidated grid per statement, omitting statements the client never sent."""
    tables = {}
    for st in STATEMENTS:
        table = consolidate(ss, st)
        if table.rows:
            tables[st] = table
    if not tables:
        raise JobError("ingest produced no rows to map")
    return tables


# --------------------------------------------------------------------------
# Job state
# --------------------------------------------------------------------------


@dataclass
class JobState:
    client_name: str
    template: str
    inputs: list[str] = field(default_factory=list)
    profile_path: str | None = None
    used_llm: bool = False
    created: str = ""
    scope: str | None = None
    #: Period captions the structural parser could not settle, and what they
    #: were resolved to. Persisted so `build` re-ingests to exactly the figures
    #: the analyst reviewed rather than merely to the same figures usually --
    #: the model layer is only safe here because its answers are frozen once.
    period_cache: dict = field(default_factory=dict)

    def save(self, job_dir: Path) -> None:
        (job_dir / JOB_FILE).write_text(
            json.dumps(self.__dict__, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, job_dir: Path) -> "JobState":
        p = job_dir / JOB_FILE
        if not p.exists():
            raise JobError(
                f"{p} not found -- is {job_dir} a job directory? Run `prepare` first."
            )
        return cls(**json.loads(p.read_text(encoding="utf-8")))


def _as_profile(client_name: str, template: Path, sets: dict) -> "object":
    from fsa.model.mapping import ClientProfile

    if not client_name:
        # `load_profile` rejects a nameless profile, so an omitted --client
        # silently produced a job that `build` could not reopen. A placeholder
        # keeps the round trip working; the analyst can rename it in the YAML.
        client_name = "Unnamed client"

    return ClientProfile(
        client_name=client_name,
        template_source=str(template),
        created_at=date.today(),
        updated_at=date.today(),
        sets={st.value: ms for st, ms in sets.items()},
    )


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------


@dataclass
class PrepareResult:
    job_dir: Path
    review_path: Path
    report: ValidationReport
    tables: dict
    sets: dict
    n_accounts: int = 0  # rows the analyst will see
    n_proposed: int = 0  # a target was chosen for them
    n_unresolved: int = 0  # no proposal at all -- pure manual work
    n_unresolved_subtotal: int = 0  # of those, client rollups (usually fine to skip)
    n_excluded: int = 0  # covered by a mapped rollup, or dropped
    conflicts: int = 0
    llm_note: str = ""


def prepare(
    inputs: list[str],
    template: Path,
    job_dir: Path,
    *,
    client_name: str = "",
    profile_path: Path | None = None,
    use_llm: bool = True,
    excludes: list[str] | None = None,
    scope: str | None = None,
) -> PrepareResult:
    """Files in -> a review workbook the analyst can open. Stops there, on purpose."""
    from fsa.mapping.matcher import propose
    from fsa.mapping.profile import load_profile
    from fsa.mapping.reconcile import reconcile
    from fsa.mapping.template import load_template
    from fsa.mapping.profile import save_profile
    from fsa.model.mapping import Decider
    from fsa.review.sheet import export_review
    from fsa.validate.checks import run_all

    template = Path(template)
    if not template.exists():
        raise JobError(f"template not found: {template}")
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    from fsa.ingest.period import PeriodResolver

    files = resolve_inputs(inputs, excludes)
    resolver = PeriodResolver(use_llm=use_llm)
    ss, ingest_findings = ingest(files, resolver=resolver, scope=scope)
    for note in resolver.notes:
        ingest_findings.append(
            Finding(severity=Severity.WARNING, code="period_model_unavailable", message=note)
        )
    tables = consolidate_all(ss)

    report = run_all(ss, tables)
    report.extend(ingest_findings)

    spec = load_template(template)
    profile = load_profile(Path(profile_path)) if profile_path else None

    sets: dict = {}
    llm_notes: list[str] = []
    conflicts = 0
    for st, table in tables.items():
        ms = propose(table, spec, profile=profile)
        if use_llm:
            from fsa.mapping.llm import propose_unresolved

            try:
                res = propose_unresolved(ms, table, spec)
                llm_notes.append(
                    f"{st.value}: {res.proposed} proposed (${res.cost_usd:.2f})"
                )
            except Exception as exc:  # noqa: BLE001
                # A missing API key or a network failure must not lose the
                # deterministic work already done -- the analyst can still
                # review what L0-L3 produced and fill the rest in by hand.
                report.add(
                    Finding(
                        severity=Severity.WARNING,
                        code="llm_unavailable",
                        message=f"{st.value}: LLM proposal step failed ({exc}); unresolved rows left for review",
                        statement=st,
                    )
                )
        rep = reconcile(ms, table, spec)
        report.extend(rep.findings)
        conflicts += rep.conflicts
        sets[st] = ms

    review_path = export_review(
        sets, tables, spec, job_dir / REVIEW_FILE, client_name=client_name
    )

    save_profile(_as_profile(client_name, template, sets), job_dir / PROPOSED_FILE)
    JobState(
        client_name=client_name,
        template=str(template.resolve()),
        inputs=[str(f.resolve()) for f in files],
        profile_path=str(Path(profile_path).resolve()) if profile_path else None,
        used_llm=use_llm,
        created=date.today().isoformat(),
        scope=scope,
        period_cache=resolver.cache,
    ).save(job_dir)

    from fsa.validate.report import render_json

    (job_dir / FINDINGS_FILE).write_text(render_json(report, tables), encoding="utf-8")

    # An unmapped *client subtotal* is usually the correct outcome, not a gap:
    # once its members are mapped, mapping the rollup too would double-count.
    # Lumping it in with genuinely unmapped detail accounts overstates how much
    # manual work is actually waiting in the review sheet.
    from fsa.model.schema import RowKind

    rollups = {
        (st, row.norm_label)
        for st, table in tables.items()
        for row in table.rows
        if row.kind is RowKind.SUBTOTAL
    }
    rules = [(st, r) for st, ms in sets.items() for r in ms.rules]
    unresolved = [(st, r) for st, r in rules if r.decided_by is Decider.UNRESOLVED]
    excluded = sum(len(ms.exclusions) for ms in sets.values())
    return PrepareResult(
        job_dir=job_dir,
        review_path=review_path,
        report=report,
        tables=tables,
        sets=sets,
        n_accounts=len(rules) + excluded,
        n_proposed=len(rules) - len(unresolved),
        n_unresolved=len(unresolved),
        n_unresolved_subtotal=sum(
            1 for st, r in unresolved if (st, r.norm_account) in rollups
        ),
        n_excluded=excluded,
        conflicts=conflicts,
        llm_note="; ".join(llm_notes),
    )


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


@dataclass
class BuildResult:
    out_path: Path
    stats: "object"
    write_report: "object"
    plans: dict
    profile_path: Path | None = None


def build(
    job_dir: Path,
    out: Path,
    *,
    save_profile_to: Path | None = None,
    overwrite: bool = False,
    allow_unconfirmed: bool = False,
    visible: bool = False,
) -> BuildResult:
    """Reviewed workbook -> populated BVAL model, and the profile for next year."""
    from fsa.mapping.profile import load_profile, save_profile
    from fsa.mapping.template import load_template
    from fsa.review.sheet import import_review
    from fsa.write.com_writer import historical_rows, write_model
    from fsa.write.plan import build_plan

    job_dir = Path(job_dir)
    state = JobState.load(job_dir)
    review_path = job_dir / REVIEW_FILE
    if not review_path.exists():
        raise JobError(f"{review_path} not found -- nothing to apply")

    template = Path(state.template)
    if not template.exists():
        raise JobError(f"template recorded in the job no longer exists: {template}")

    missing = [p for p in state.inputs if not Path(p).exists()]
    if missing:
        raise JobError(
            "these client files have moved or been deleted since `prepare`:\n  "
            + "\n  ".join(missing)
        )

    # Replay `prepare`'s period decisions rather than re-deriving them. The
    # cache is authoritative and the model is switched off, so `build` cannot
    # quietly resolve a caption differently from the run the analyst reviewed.
    from fsa.ingest.period import PeriodResolver

    ss, _ = ingest(
        [Path(p) for p in state.inputs],
        resolver=PeriodResolver(use_llm=False, cache=dict(state.period_cache or {})),
        scope=state.scope,
    )
    tables = consolidate_all(ss)
    spec = load_template(template)

    proposed = load_profile(job_dir / PROPOSED_FILE)
    sets = {st: proposed.get(st) for st in tables}

    confirmed, stats = import_review(review_path, sets, spec)

    plans = {}
    for st, table in tables.items():
        plans[st] = build_plan(
            confirmed[st],
            spec,
            historical_rows(table),
            require_confirmed=not allow_unconfirmed,
        )

    report = write_model(
        template,
        Path(out),
        tables,
        plans,
        entity_name=state.client_name or None,
        overwrite=overwrite,
        visible=visible,
    )

    saved_to = None
    if save_profile_to is not None:
        saved_to = Path(save_profile_to)
        saved_to.parent.mkdir(parents=True, exist_ok=True)
        save_profile(_as_profile(state.client_name, template, confirmed), saved_to)

    return BuildResult(
        out_path=report.out_path,
        stats=stats,
        write_report=report,
        plans=plans,
        profile_path=saved_to,
    )
