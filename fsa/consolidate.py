"""Merge a StatementSet's PRIMARY columns into one multi-year grid.

Pure function, no I/O. See SPEC-PHASE0.md, "Module contracts / Agent B".
"""

from __future__ import annotations

import warnings

from fsa.ingest.normalize import similar
from fsa.model.schema import (
    ColumnRole,
    ConsolidatedRow,
    ConsolidatedTable,
    RowKind,
    StatementSet,
    StatementType,
)

# Typo-variant labels ("Other Paybles" vs "Other Payables") unify into one row
# above this normalized-label similarity. Below it, they are distinct accounts.
SIMILARITY_THRESHOLD = 0.92

_MERGEABLE_KINDS = (RowKind.DATA, RowKind.SUBTOTAL)


def consolidate(ss: StatementSet, statement: StatementType) -> ConsolidatedTable:
    """Merge PRIMARY columns across years into one grid, keyed on norm_label.

    Row order follows the most recent year's statement. Accounts appearing only
    in earlier years are inserted after the account that precedes them in their
    own year and is also present in the output (falling back to end-of-section).
    """
    years = ss.years(statement)
    table = ConsolidatedTable(statement=statement, years=list(years), rows=[])
    if not years:
        return table

    primary_cols = {y: ss.get(statement, y, ColumnRole.PRIMARY) for y in years}

    # norm_label -> ConsolidatedRow, for rows that participate in fuzzy matching.
    # Rows involved in an unresolved same-year conflict are deliberately left
    # out of this index so they never attract further merges.
    by_norm: dict[str, ConsolidatedRow] = {}

    def find_row(norm_label: str) -> ConsolidatedRow | None:
        hit = by_norm.get(norm_label)
        if hit is not None:
            return hit
        for existing_norm, row in by_norm.items():
            if similar(norm_label, existing_norm, SIMILARITY_THRESHOLD):
                return row
        return None

    most_recent = years[-1]
    recent_col = primary_cols[most_recent]
    if recent_col is not None:
        for r in recent_col.rows:
            if r.kind not in _MERGEABLE_KINDS:
                continue
            crow = ConsolidatedRow(
                raw_label=r.raw_label,
                norm_label=r.norm_label,
                kind=r.kind,
                section=r.section,
                values={most_recent: r.value},
                provenance={most_recent: r.ref} if r.ref is not None else {},
                depth=r.depth,
            )
            table.rows.append(crow)
            by_norm[r.norm_label] = crow

    for y in reversed(years[:-1]):
        col = primary_cols[y]
        if col is None:
            continue
        rows_for_year = [r for r in col.rows if r.kind in _MERGEABLE_KINDS]

        prev_anchor_idx: int | None = None
        for r in rows_for_year:
            existing = find_row(r.norm_label)

            if existing is not None and y in existing.values and existing.values[y] is not None and r.value is not None:
                # Two distinct accounts this year both want the same merged
                # row -- they cannot both be spelling variants of one account.
                warnings.warn(
                    f"consolidate: not merging '{r.raw_label}' with "
                    f"'{existing.raw_label}' for FY{y} -- both have values "
                    "in the same year",
                    stacklevel=2,
                )
                existing = None

            if existing is not None:
                existing.values[y] = r.value
                if r.ref is not None:
                    existing.provenance[y] = r.ref
                prev_anchor_idx = table.rows.index(existing)
                continue

            new_row = ConsolidatedRow(
                raw_label=r.raw_label,
                norm_label=r.norm_label,
                kind=r.kind,
                section=r.section,
                values={y: r.value},
                provenance={y: r.ref} if r.ref is not None else {},
                depth=r.depth,
            )
            insert_idx = _insertion_point(table.rows, prev_anchor_idx, r.section)
            table.rows.insert(insert_idx, new_row)
            # Registered under its own norm_label only when it did not
            # collide above, so a later conflicting row would not silently
            # attach to a row that itself came from an unresolved conflict.
            if r.norm_label not in by_norm:
                by_norm[r.norm_label] = new_row
            prev_anchor_idx = insert_idx

    return table


def _insertion_point(
    rows: list[ConsolidatedRow], prev_anchor_idx: int | None, section: str | None
) -> int:
    if prev_anchor_idx is not None:
        return prev_anchor_idx + 1
    last_in_section = None
    for i, row in enumerate(rows):
        if row.section == section:
            last_in_section = i
    if last_in_section is not None:
        return last_in_section + 1
    return len(rows)
