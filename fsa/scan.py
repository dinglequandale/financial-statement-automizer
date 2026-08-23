"""Look at a folder and say what is in it, before anything expensive happens.

A client folder is not a curated set of statements. TS Distributors' holds
Weaver's own 2020-2025 working consolidation beside the five yearly client
files; Ram Rod's holds a cash flow statement; Commercial Flooring's holds a
draft schedule and a set of already-converted PDFs. Reading the wrong one is
not a rare accident, it is the normal condition of the input.

The tool already detects two sources claiming one year -- but only after
ingest, mapping and a round of suggestions have run. That is the right answer
arriving after the expensive part, and it costs the analyst a whole second run
to act on something visible from the outset.

This does the cheap half early: open each file, note which statements and which
years it actually contains, and say where two files overlap. No mapping, no
suggestions, no network -- roughly two seconds a file, and it turns "read
everything and find out" into a decision the analyst makes with the facts in
front of them.

It is deliberately advisory. Nothing is unticked automatically, because which
file is authoritative is the analyst's call and a folder full of odd names is
not evidence of anything.
"""

from __future__ import annotations

import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from fsa.model.schema import ColumnRole, StatementType


@dataclass
class FileScan:
    """What one file turned out to hold."""

    path: Path
    size_kb: float
    #: Statement -> the fiscal years this file is the *primary* source for.
    #: Comparative columns are excluded: every client file carries last year's
    #: figures beside this year's, and treating those as claims would make
    #: every consecutive pair of files look like a conflict.
    claims: dict[str, list[int]] = field(default_factory=dict)
    error: str | None = None
    #: Years this file claims that another file claims too.
    overlaps: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.claims:
            return "no balance sheet or income statement found"
        parts = []
        for statement, years in sorted(self.claims.items()):
            span = (
                f"{min(years)}-{max(years)}" if len(years) > 1 else f"{min(years)}"
            )
            parts.append(f"{statement} {span}")
        return ", ".join(parts)


def scan_file(path: Path) -> FileScan:
    """Read one file structurally. Never raises, never calls out to a model."""
    out = FileScan(path=path, size_kb=path.stat().st_size / 1024)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        columns = []
        if path.suffix.lower() in (".xlsx", ".xls"):
            try:
                from fsa.ingest.extract import extract_workbook

                columns = list(
                    extract_workbook(path, statements=(StatementType.BS, StatementType.IS))
                )
            except Exception:  # noqa: BLE001 - fall through to the reader seam
                columns = []
        if not columns:
            try:
                from fsa.ingest.interpret import interpret, read_any

                # No resolver: this is a survey, and a survey must not spend
                # money or need a network to tell you what is in a folder.
                columns, _ = interpret(read_any(path))
            except Exception as exc:  # noqa: BLE001
                out.error = f"could not be read ({type(exc).__name__})"
                return out

    per: dict[str, set[int]] = defaultdict(set)
    for c in columns:
        if c.role is ColumnRole.PRIMARY:
            per[c.statement.value].add(c.fiscal_year)
    out.claims = {k: sorted(v) for k, v in per.items()}
    return out


def mark_overlaps(scans: list[FileScan]) -> list[FileScan]:
    """Note, on each file, which other file covers the same years.

    Collapsed per other-file rather than per year: client five's two balance
    sheet sources overlap on six years each, and six near-identical lines is a
    wall of text where one line is a fact the analyst can act on.
    """
    claimants: dict[tuple[str, int], list[FileScan]] = defaultdict(list)
    for scan in scans:
        for statement, years in scan.claims.items():
            for year in years:
                claimants[(statement, year)].append(scan)

    shared: dict[int, dict[str, list[tuple[str, int]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for (statement, year), holders in claimants.items():
        if len(holders) < 2:
            continue
        for scan in holders:
            for other in holders:
                if other is not scan:
                    shared[id(scan)][other.name].append((statement, year))

    for scan in scans:
        scan.overlaps = []
        for other_name, pairs in sorted(shared.get(id(scan), {}).items()):
            by_statement: dict[str, list[int]] = defaultdict(list)
            for statement, year in pairs:
                by_statement[statement].append(year)
            what = ", ".join(
                f"{st} {min(ys)}-{max(ys)}" if len(ys) > 1 else f"{st} {min(ys)}"
                for st, ys in sorted(by_statement.items())
            )
            scan.overlaps.append(f"{what} also in {other_name}")
    return scans


def scan_folder(folder: Path, readable: set[str] | None = None) -> list[FileScan]:
    """Every readable file in `folder`, with overlaps between them marked."""
    from fsa.job import READABLE

    exts = readable or READABLE
    files = sorted(
        f
        for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in exts and not f.name.startswith("~$")
    )
    scans = [scan_file(f) for f in files]
    mark_overlaps(scans)
    return scans
