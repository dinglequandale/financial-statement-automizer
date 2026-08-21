"""Validation checks. Each check-code function returns list[Finding].

See SPEC-PHASE0.md, "Module contracts / Agent B" for the check table and the
"Ground truth for the acceptance test" section for the audit-mode shapes this
module must discriminate between.
"""

from __future__ import annotations

from fsa.ingest.normalize import is_derived_line, similarity
from fsa.model.schema import (
    AccountRow,
    ColumnRole,
    ConsolidatedRow,
    ConsolidatedTable,
    ExtractedColumn,
    Finding,
    RowKind,
    Severity,
    StatementSet,
    StatementType,
    ValidationReport,
)

TOL = 0.01

# row_alignment_suspected fires only on a run of at least this many consecutive
# accounts shifted the same direction. Shorter runs -- including length-1/2 --
# are reported as value_delta instead (isolated, not systematic).
MIN_RUN_LENGTH = 3

# account_renamed: an account vanishing in year N and a new one appearing in
# year N pair up as a suspected rename above this normalized-label similarity.
RENAME_SIMILARITY_THRESHOLD = 0.6


def _close(a: float | None, b: float | None, tol: float = TOL) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


def _fmt(x: float | None) -> str:
    return "None" if x is None else f"{x:.2f}"


# --------------------------------------------------------------------------
# Per-file checks
# --------------------------------------------------------------------------


def subtotal_mismatch(column: ExtractedColumn) -> list[Finding]:
    """WARNING: a client *block* subtotal != sum of its DATA rows (tol 0.01).

    Only block subtotals -- those directly preceded by at least one valued DATA
    row -- are checked. A subtotal with no DATA rows since the previous subtotal
    is a derived line (grand total, margin, profit line) whose formula is
    layout-specific: "Total Assets" = TCA + TFA + TOA, "Gross Margin" = revenue
    minus COGS, "Net Profit/Loss" nets several blocks. Guessing at those
    produces confident-sounding false positives, which is worse than silence --
    so we say nothing about them.
    """
    # When the source encodes nesting, `check_hierarchy` supersedes this and
    # this one becomes actively wrong: "sum everything since the last subtotal"
    # cannot see where a nested group begins, so it charges a parent with its
    # older siblings' balances. Measured on sample 2 -- every one of the 12
    # warnings this produced was false, e.g. `Total Insurance Expense` flagged
    # at 118,705.50 against a correct 90,608.30, the gap being exactly the five
    # unrelated expense rows printed above the insurance group.
    if any(r.depth is not None for r in column.rows):
        return []

    findings: list[Finding] = []
    running_sum = 0.0
    valued_data_rows = 0
    for row in column.rows:
        if row.kind is RowKind.DATA:
            if row.value is not None:
                running_sum += row.value
                valued_data_rows += 1
        elif row.kind is RowKind.SUBTOTAL:
            if (
                valued_data_rows > 0
                and row.value is not None
                and not is_derived_line(row.raw_label)
                and not _close(running_sum, row.value)
            ):
                findings.append(
                    Finding(
                        severity=Severity.WARNING,
                        code="subtotal_mismatch",
                        message=(
                            f"subtotal '{row.raw_label}' = {_fmt(row.value)} "
                            f"but components sum to {_fmt(running_sum)}"
                        ),
                        statement=column.statement,
                        fiscal_year=column.fiscal_year,
                        account=row.raw_label,
                        ref=row.ref,
                        detail={"expected": running_sum, "actual": row.value},
                    )
                )
            running_sum = 0.0
            valued_data_rows = 0
    return findings


def check_hierarchy(column: ExtractedColumn) -> list[Finding]:
    """WARNING: an indented subtotal != the sum of its own children.

    Stronger than `subtotal_mismatch`, and only available when the source
    encodes nesting depth (QuickBooks-style reports do; flat spreadsheets do
    not). Depth makes a subtotal's scope exact rather than inferred, so this
    checks *every* level -- including nested subtotals and grand totals, which
    `subtotal_mismatch` deliberately declines to guess at.

    A subtotal at depth `d` sums the valued rows at depth `d + 1` in the run
    immediately above it, back to the header at depth `d` that opened the group.
    Nested subtotals are collected at their own level and their children are
    skipped, so nothing is counted twice.

    Derived lines are excluded: `Gross Profit`, `Net Income` and friends are
    differences between groups, not sums of children, so summing their siblings
    yields a confident-sounding false positive.
    """
    rows = column.rows
    if not any(r.depth is not None for r in rows):
        return []

    findings: list[Finding] = []
    for i, row in enumerate(rows):
        if (
            row.kind is not RowKind.SUBTOTAL
            or row.value is None
            or row.depth is None
            or is_derived_line(row.raw_label)
        ):
            continue

        kids: list[AccountRow] = []
        for prev in reversed(rows[:i]):
            if prev.depth is None or prev.depth <= row.depth:
                break  # the header that opened this group
            if prev.depth == row.depth + 1 and prev.value is not None:
                kids.append(prev)
        if not kids:
            continue

        total = sum(k.value for k in kids)
        if not _close(total, row.value):
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="hierarchy_mismatch",
                    message=(
                        f"'{row.raw_label}' = {_fmt(row.value)} but its "
                        f"{len(kids)} child rows sum to {_fmt(total)}"
                    ),
                    statement=column.statement,
                    fiscal_year=column.fiscal_year,
                    account=row.raw_label,
                    ref=row.ref,
                    detail={
                        "expected": total,
                        "actual": row.value,
                        "children": [k.raw_label for k in reversed(kids)],
                    },
                )
            )
    return findings


_ASSETS_LABELS = ("total assets",)
_LIAB_LABELS = ("total liabilities",)
_EQUITY_LABELS = ("total owners equity", "total stockholders equity", "total equity")
_COMBINED_LABELS = ("total liabilities and equity", "total liabilities and owners equity")


def balance_sheet_unbalanced(column: ExtractedColumn) -> list[Finding]:
    """ERROR: Total Assets != Total Liabilities + Equity (tolerance 0.01)."""
    if column.statement is not StatementType.BS:
        return []

    subtotals = [r for r in column.rows if r.kind is RowKind.SUBTOTAL and r.value is not None]

    def find_one(labels: tuple[str, ...]) -> AccountRow | None:
        for r in subtotals:
            if r.norm_label in labels:
                return r
        return None

    assets_row = find_one(_ASSETS_LABELS)
    if assets_row is None:
        return []

    combined_row = find_one(_COMBINED_LABELS)
    if combined_row is not None:
        rhs = combined_row.value
        rhs_ref = combined_row.ref
    else:
        liab_row = find_one(_LIAB_LABELS)
        equity_row = find_one(_EQUITY_LABELS)
        if liab_row is None or equity_row is None:
            return []
        rhs = (liab_row.value or 0.0) + (equity_row.value or 0.0)
        rhs_ref = liab_row.ref

    if _close(assets_row.value, rhs):
        return []

    return [
        Finding(
            severity=Severity.ERROR,
            code="balance_sheet_unbalanced",
            message=(
                f"Total Assets = {_fmt(assets_row.value)} but Total Liabilities + "
                f"Equity = {_fmt(rhs)}"
            ),
            statement=StatementType.BS,
            fiscal_year=column.fiscal_year,
            account="Total Assets",
            ref=assets_row.ref or rhs_ref,
            detail={"total_assets": assets_row.value, "total_liab_plus_equity": rhs},
        )
    ]


def unparsed_row(column: ExtractedColumn) -> list[Finding]:
    """WARNING: a row with a label and a numeric value that could not be
    classified as DATA/SUBTOTAL (it fell into BLANK/TITLE despite carrying a
    real value -- a signal something upstream misjudged the header row or a
    separator)."""
    findings: list[Finding] = []
    for row in column.rows:
        if row.kind in (RowKind.BLANK, RowKind.TITLE) and row.raw_label and row.raw_label.strip() and row.value is not None:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="unparsed_row",
                    message=(
                        f"row '{row.raw_label}' has a numeric value ({_fmt(row.value)}) "
                        f"but was classified as {row.kind.value}"
                    ),
                    statement=column.statement,
                    fiscal_year=column.fiscal_year,
                    account=row.raw_label,
                    ref=row.ref,
                    detail={"kind": row.kind.value, "value": row.value},
                )
            )
    return findings


def _comparative_order_and_lookup(comparative: ExtractedColumn, prior_primary: ExtractedColumn):
    """prior_primary is the authoritative source for its own year (PLAN.md
    S6: never trust the comparative column as a data source); comparative is
    what we are cross-checking it against."""
    order = [
        (r.norm_label, r.raw_label, r.value)
        for r in prior_primary.rows
        if r.kind in (RowKind.DATA, RowKind.SUBTOTAL) and r.value is not None
    ]
    ref_lookup = {
        r.norm_label: (r.raw_label, r.value)
        for r in comparative.rows
        if r.kind in (RowKind.DATA, RowKind.SUBTOTAL) and r.value is not None
    }
    return order, ref_lookup


def _comparative_alignment_findings(
    comparative: ExtractedColumn, prior_primary: ExtractedColumn
) -> tuple[list[Finding], set[str]]:
    """Detect the subset of comparative-vs-prior-primary disagreements that
    form a positional shift (>= MIN_RUN_LENGTH, reusing the row_alignment_
    suspected run detector) or a 2-account swap. Returns the findings plus
    the set of norm_labels already explained by one of these patterns, so
    comparative_disagreement does not also report them as scattered noise."""
    order, ref_lookup = _comparative_order_and_lookup(comparative, prior_primary)
    statuses, ref_vals = _shift_statuses(order, ref_lookup)

    findings: list[Finding] = []
    covered: set[str] = set()

    runs = _runs(order, statuses)
    for start, end, direction in [r for r in runs if r[1] - r[0] + 1 >= MIN_RUN_LENGTH]:
        covered.update(order[i][0] for i in range(start, end + 1))
        labels, _first_label, _first_val = _run_report_data(order, start, end, direction)
        dir_desc = "one row behind" if direction == -1 else "one row ahead"
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="comparative_alignment_suspected",
                message=(
                    f"FY{prior_primary.fiscal_year}: comparative column looks positionally "
                    f"shifted ({dir_desc}) across {len(labels)} accounts from '{labels[0]}' "
                    f"to '{labels[-1]}'"
                ),
                statement=prior_primary.statement,
                fiscal_year=prior_primary.fiscal_year,
                account=labels[0],
                detail={"pattern": "shift", "accounts": labels, "length": len(labels), "shift": direction},
            )
        )

    for i, j in _find_swaps(order, ref_vals):
        if order[i][0] in covered or order[j][0] in covered:
            continue
        a_label, b_label = order[i][1], order[j][1]
        covered.add(order[i][0])
        covered.add(order[j][0])
        # One finding per account (not one combined finding) so each side of
        # the swap is independently searchable by `account`, with its own
        # delta -- e.g. TS Distributors FY2021 BS: "Certificate of Deposit"
        # and "Accounts Receivable - Trade" both need to show up on their own.
        for idx, other_idx in ((i, j), (j, i)):
            label = order[idx][1]
            our_val = order[idx][2]
            comp_val = ref_vals.get(idx)
            other_label = order[other_idx][1]
            delta = (comp_val - our_val) if comp_val is not None and our_val is not None else None
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="comparative_alignment_suspected",
                    message=(
                        f"FY{prior_primary.fiscal_year}: '{label}' and '{other_label}' appear to "
                        "have swapped values between the comparative and prior primary columns"
                    ),
                    statement=prior_primary.statement,
                    fiscal_year=prior_primary.fiscal_year,
                    account=label,
                    detail={
                        "pattern": "swap",
                        "accounts": [a_label, b_label],
                        "length": 2,
                        "comparative_value": comp_val,
                        "prior_primary_value": our_val,
                        "delta": delta,
                    },
                )
            )

    return findings, covered


def comparative_alignment_suspected(comparative: ExtractedColumn, prior_primary: ExtractedColumn) -> list[Finding]:
    """WARNING: comparative-column disagreements that form a positional shift
    (>= 3 accounts) or a 2-account swap, rather than scattered restatements."""
    findings, _covered = _comparative_alignment_findings(comparative, prior_primary)
    return findings


def comparative_disagreement(comparative: ExtractedColumn, prior_primary: ExtractedColumn) -> list[Finding]:
    """WARNING: file N's COMPARATIVE column != file N-1's PRIMARY, per account.

    The client genuinely restates prior periods between filings -- this is a
    disagreement between two source documents for a human to weigh, not proof
    our extraction is wrong. Accounts already explained by a detected
    comparative_alignment_suspected pattern are excluded here so they are
    reported once, not twice.
    """
    findings: list[Finding] = []
    comp_by_label = comparative.by_norm_label()
    prior_by_label = prior_primary.by_norm_label()
    _pattern_findings, covered = _comparative_alignment_findings(comparative, prior_primary)

    for label, comp_rows in comp_by_label.items():
        prior_rows = prior_by_label.get(label)
        if not prior_rows:
            continue
        comp_row = comp_rows[0]
        prior_row = prior_rows[0]
        if _close(comp_row.value, prior_row.value):
            continue
        if label in covered:
            continue
        delta = (
            comp_row.value - prior_row.value
            if comp_row.value is not None and prior_row.value is not None
            else None
        )
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="comparative_disagreement",
                message=(
                    f"'{comp_row.raw_label}': comparative column in FY{comparative.fiscal_year} "
                    f"file = {_fmt(comp_row.value)}, but FY{prior_primary.fiscal_year} file's "
                    f"primary column = {_fmt(prior_row.value)}"
                ),
                statement=comparative.statement,
                fiscal_year=comparative.fiscal_year,
                account=comp_row.raw_label,
                ref=comp_row.ref,
                detail={
                    "comparative_value": comp_row.value,
                    "prior_primary_value": prior_row.value,
                    "delta": delta,
                    "prior_ref": str(prior_row.ref) if prior_row.ref else None,
                },
            )
        )
    return findings


def check_comparative_agreement(ss: StatementSet) -> list[Finding]:
    """Run comparative_disagreement and comparative_alignment_suspected across
    every (statement, year) pair in ss where both file N's COMPARATIVE column
    and file N-1's PRIMARY column for the same year exist."""
    findings: list[Finding] = []
    for statement in StatementType:
        years = sorted({c.fiscal_year for c in ss.columns if c.statement is statement})
        for y in years:
            comp = ss.get(statement, y, ColumnRole.COMPARATIVE)
            prim = ss.get(statement, y, ColumnRole.PRIMARY)
            if comp is not None and prim is not None:
                findings.extend(comparative_disagreement(comp, prim))
                findings.extend(comparative_alignment_suspected(comp, prim))
    return findings


# --------------------------------------------------------------------------
# Cross-year checks on the consolidated table
# --------------------------------------------------------------------------


def _presence_transitions(table: ConsolidatedTable) -> dict[int, tuple[set[str], set[str]]]:
    """For each (year-1 -> year) transition: (removed labels, added labels)."""
    years = sorted(table.years)
    out: dict[int, tuple[set[str], set[str]]] = {}
    for i in range(1, len(years)):
        prev_y, y = years[i - 1], years[i]
        removed = set()
        added = set()
        for row in table.rows:
            had_prev = row.values.get(prev_y) is not None
            has_now = row.values.get(y) is not None
            if had_prev and not has_now:
                removed.add(row.norm_label)
            elif has_now and not had_prev:
                added.add(row.norm_label)
        out[y] = (removed, added)
    return out


def _label_lookup(table: ConsolidatedTable) -> dict[str, ConsolidatedRow]:
    return {r.norm_label: r for r in table.rows}


def _renamed_pairs(table: ConsolidatedTable) -> dict[int, list[tuple[str, str]]]:
    """year -> [(removed_norm_label, added_norm_label), ...] above threshold."""
    transitions = _presence_transitions(table)
    by_label = _label_lookup(table)
    out: dict[int, list[tuple[str, str]]] = {}
    for y, (removed, added) in transitions.items():
        pairs: list[tuple[str, str]] = []
        remaining_added = set(added)
        for r_label in removed:
            best_label = None
            best_score = 0.0
            for a_label in remaining_added:
                score = similarity(by_label[r_label].raw_label, by_label[a_label].raw_label)
                if score > best_score:
                    best_score = score
                    best_label = a_label
            if best_label is not None and best_score >= RENAME_SIMILARITY_THRESHOLD:
                pairs.append((r_label, best_label))
                remaining_added.discard(best_label)
        out[y] = pairs
    return out


def account_added(table: ConsolidatedTable) -> list[Finding]:
    """INFO: account present this year, absent prior (excluding suspected renames)."""
    findings: list[Finding] = []
    transitions = _presence_transitions(table)
    renamed = _renamed_pairs(table)
    by_label = _label_lookup(table)
    for y, (_removed, added) in transitions.items():
        used = {a for (_r, a) in renamed.get(y, [])}
        for label in sorted(added - used):
            row = by_label[label]
            findings.append(
                Finding(
                    severity=Severity.INFO,
                    code="account_added",
                    message=f"account '{row.raw_label}' first appears in FY{y}",
                    statement=table.statement,
                    fiscal_year=y,
                    account=row.raw_label,
                    ref=row.provenance.get(y),
                )
            )
    return findings


def account_removed(table: ConsolidatedTable) -> list[Finding]:
    """INFO: account absent this year, present prior (excluding suspected renames)."""
    findings: list[Finding] = []
    transitions = _presence_transitions(table)
    renamed = _renamed_pairs(table)
    by_label = _label_lookup(table)
    for y, (removed, _added) in transitions.items():
        used = {r for (r, _a) in renamed.get(y, [])}
        for label in sorted(removed - used):
            row = by_label[label]
            findings.append(
                Finding(
                    severity=Severity.INFO,
                    code="account_removed",
                    message=f"account '{row.raw_label}' last appears before FY{y}",
                    statement=table.statement,
                    fiscal_year=y,
                    account=row.raw_label,
                )
            )
    return findings


def account_renamed(table: ConsolidatedTable) -> list[Finding]:
    """WARNING: suspected rename -- account A vanishes in year N, account B
    appears in year N, similarity(A, B) >= 0.6. Report both, let a human decide."""
    findings: list[Finding] = []
    renamed = _renamed_pairs(table)
    by_label = _label_lookup(table)
    for y, pairs in renamed.items():
        for r_label, a_label in pairs:
            r_row, a_row = by_label[r_label], by_label[a_label]
            score = similarity(r_row.raw_label, a_row.raw_label)
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="account_renamed",
                    message=(
                        f"suspected rename in FY{y}: '{r_row.raw_label}' -> "
                        f"'{a_row.raw_label}' (similarity {score:.2f})"
                    ),
                    statement=table.statement,
                    fiscal_year=y,
                    account=f"{r_row.raw_label} -> {a_row.raw_label}",
                    ref=a_row.provenance.get(y),
                    detail={"from": r_row.raw_label, "to": a_row.raw_label, "similarity": score},
                )
            )
    return findings


# --------------------------------------------------------------------------
# Audit mode -- the headline check
# --------------------------------------------------------------------------

# Weaver's reference sometimes carries a *standing* rename that has nothing to
# do with the shift bug (e.g. the client's "Accounts Receivable - Lawler" is
# always "Accounts Receivable - Indital" in Weaver's sheet). If an exact
# norm_label lookup fails, fall back to the best fuzzy match above this (loose,
# audit-only) threshold so one renamed label in the middle of a shift run
# doesn't fracture the run into sub-3 pieces that get missed.
AUDIT_ALIAS_FALLBACK_THRESHOLD = 0.5


def _ref_lookup(reference: ConsolidatedTable, year: int) -> dict[str, tuple[str, float | None]]:
    return {
        row.norm_label: (row.raw_label, row.values.get(year))
        for row in reference.rows
        if row.values.get(year) is not None
    }


def _ref_value_for(
    raw_label: str,
    norm_label: str,
    lookup: dict[str, tuple[str, float | None]],
    claimed: frozenset[str] = frozenset(),
) -> float | None:
    hit = lookup.get(norm_label)
    if hit is not None:
        return hit[1]
    best_val = None
    best_score = AUDIT_ALIAS_FALLBACK_THRESHOLD
    for cand_norm, (cand_raw, cand_val) in lookup.items():
        if cand_norm in claimed:
            # Already exactly claimed by a different account this year --
            # never steal it as a fuzzy fallback for someone else. Without
            # this, an unrelated but similarly-worded label (e.g. two
            # different "Accounts Receivable - X" accounts) can outscore the
            # genuine standing-rename match and silently break a run.
            continue
        score = similarity(raw_label, cand_raw)
        if score > best_score:
            best_score = score
            best_val = cand_val
    return best_val


def _shift_statuses(
    order: list[tuple[str, str, float | None]],
    lookup: dict[str, tuple[str, float | None]],
) -> tuple[dict[int, int], dict[int, float]]:
    """Core run/shift classifier, shared by audit mode (row_alignment_suspected,
    against a reference ConsolidatedTable) and comparative-column checking
    (comparative_alignment_suspected, against a comparative ExtractedColumn).

    order  -- list of (norm_label, raw_label, value) in the *canonical*
              (authoritative) source's order, restricted to non-None values.
    lookup -- norm_label -> (raw_label, value) for the column being checked
              against `order`.

    Returns (statuses, ref_vals):
      statuses -- dict: index in `order` -> +1 / -1 / 0
                  +1  the checked column's value at this label equals our
                      value one position later (it lags behind)
                  -1  the checked column's value at this label equals our
                      value one position earlier (it is ahead)
                   0  disagreement matches neither neighbour (isolated, not
                      part of a shift)
                  Positions absent from `statuses` are either in agreement or
                  have no comparable data for that label.
      ref_vals -- dict: index in `order` -> the value used for the comparison,
                  for every index present in `statuses`.
    """
    # Labels with an exact norm_label hit are "claimed" up front, so the fuzzy
    # fallback (used for standing renames) never reassigns them to a
    # different, merely similarly-worded, account.
    claimed = frozenset(label for label, _raw, _value in order if label in lookup)

    statuses: dict[int, int] = {}
    ref_vals: dict[int, float] = {}
    for i, (label, raw, value) in enumerate(order):
        ref_val = _ref_value_for(raw, label, lookup, claimed)
        if ref_val is None:
            continue
        if _close(ref_val, value):
            continue
        ref_vals[i] = ref_val
        prev_val = order[i - 1][2] if i - 1 >= 0 else None
        next_val = order[i + 1][2] if i + 1 < len(order) else None
        matches_prev = prev_val is not None and _close(ref_val, prev_val)
        matches_next = next_val is not None and _close(ref_val, next_val)
        if matches_prev and not matches_next:
            statuses[i] = -1
        elif matches_next and not matches_prev:
            statuses[i] = +1
        elif matches_prev and matches_next:
            # Ambiguous (e.g. a run of equal values). Prefer continuing
            # whatever direction the previous position in the run used.
            statuses[i] = statuses.get(i - 1, -1)
        else:
            statuses[i] = 0

    return statuses, ref_vals


def _year_shift_analysis(ours: ConsolidatedTable, reference: ConsolidatedTable, year: int):
    """Audit-mode wrapper around _shift_statuses: `ours` is the authoritative
    ConsolidatedTable we built, `reference` is Weaver's hand-built grid, both
    restricted to the given year.

    Returns (order, statuses, ref_vals) -- see _shift_statuses for the shape
    of statuses/ref_vals. `order` is ours' canonical row order, restricted to
    accounts present (value is not None) that year.
    """
    order = [
        (row.norm_label, row.raw_label, row.values.get(year))
        for row in ours.rows
        if row.values.get(year) is not None
    ]
    lookup = _ref_lookup(reference, year)
    statuses, ref_vals = _shift_statuses(order, lookup)
    return order, statuses, ref_vals


def _runs(order, statuses: dict[int, int]) -> list[tuple[int, int, int]]:
    """Group consecutive indices with the same nonzero shift direction into
    (start, end, direction) runs, end inclusive.

    A single position that breaks continuity (no comparable data, or a
    coincidental value match in the "wrong" direction -- real client data has
    plenty of repeated 0.00 balances among small AR/contra accounts, which
    produces exactly this kind of one-row noise) is bridged when the runs on
    either side share the same direction. Real example: TS Distributors
    FY2022 BS has a genuine one-row gap ('A/R terms - Allowed write off',
    -0.38 vs reference's 0.00) sitting between two halves of what is
    otherwise one continuous 9-account misalignment.
    """
    raw_runs: list[list[int]] = []
    i = 0
    n = len(order)
    while i < n:
        d = statuses.get(i)
        if d is None or d == 0:
            i += 1
            continue
        start = i
        while i + 1 < n and statuses.get(i + 1) == d:
            i += 1
        raw_runs.append([start, i, d])
        i += 1

    merged: list[list[int]] = []
    i = 0
    n_runs = len(raw_runs)
    while i < n_runs:
        cur = list(raw_runs[i])
        while i + 1 < n_runs:
            nxt = raw_runs[i + 1]
            # Case 1: exactly one unclassified position (no raw_run at all)
            # separates cur from a same-direction continuation.
            if nxt[2] == cur[2] and nxt[0] - cur[1] == 2:
                cur[1] = nxt[1]
                i += 1
                continue
            # Case 2: a single-position blip (classified, but as noise or the
            # "wrong" direction -- real data has plenty of coincidental 0.00
            # matches among small AR/contra accounts) sits between cur and a
            # same-direction continuation just beyond it.
            if (
                nxt[0] - cur[1] == 1
                and nxt[1] == nxt[0]
                and i + 2 < n_runs
                and raw_runs[i + 2][2] == cur[2]
                and raw_runs[i + 2][0] - nxt[1] == 1
            ):
                cur[1] = raw_runs[i + 2][1]
                i += 2
                continue
            break
        merged.append(cur)
        i += 1
    return [tuple(r) for r in merged]


def _run_report_data(order, start: int, end: int, direction: int):
    """Extend a run's reported account list with the "origin" position -- the
    account whose true value visibly moved, even though that position itself
    has no comparable reference row (that is exactly why it moved: the
    reference sheet has no slot for it). direction == -1 means the reference
    shows each position's *predecessor* value, so the origin is start - 1;
    direction == +1 is the mirror image at end + 1."""
    idxs = list(range(start, end + 1))
    if direction == -1 and start - 1 >= 0:
        idxs = [start - 1] + idxs
    elif direction == +1 and end + 1 < len(order):
        idxs = idxs + [end + 1]
    labels = [order[i][1] for i in idxs]
    first_label, first_val = order[idxs[0]][1], order[idxs[0]][2]
    return labels, first_label, first_val


def _find_swaps(order, ref_vals: dict[int, float]) -> list[tuple[int, int]]:
    """Adjacent-pair value swaps: position i's checked value equals position
    i+1's true value, and position i+1's checked value equals position i's
    true value.

    This is checked directly against the raw looked-up values (`ref_vals`),
    not the smoothed +1/-1 `statuses` -- the ambiguity rule in
    _shift_statuses ("an ambiguous position inherits the previous run's
    direction") exists to keep long uniform cascades from flip-flopping on
    coincidental repeated values, but that same rule can mask a genuine 2-row
    swap (a real instance: TS Distributors FY2021 BS, where "Certificate of
    Deposit" and "Accounts Receivable - Trade" swap values between the
    comparative and prior primary columns) by making position i+1 inherit
    i's direction instead of reporting its true, opposite one. A swap is not
    a uniform-direction run either way, so _runs() would never report it --
    this is a distinct, deliberately narrow pattern (minimum length 2)."""
    swaps: list[tuple[int, int]] = []
    n = len(order)
    i = 0
    while i < n - 1:
        if (
            i in ref_vals
            and (i + 1) in ref_vals
            and _close(ref_vals[i], order[i + 1][2])
            and _close(ref_vals[i + 1], order[i][2])
        ):
            swaps.append((i, i + 1))
            i += 2
        else:
            i += 1
    return swaps


def row_alignment_suspected(ours: ConsolidatedTable, reference: ConsolidatedTable) -> list[Finding]:
    """ERROR, audit mode: a run of >= 3 consecutive accounts where the
    reference's value for account i equals our value for account i+1 (or
    i-1). One finding per run, not one per row."""
    findings: list[Finding] = []
    common_years = sorted(set(ours.years) & set(reference.years))
    for year in common_years:
        order, statuses, _ref_vals = _year_shift_analysis(ours, reference, year)
        runs = _runs(order, statuses)
        qualifying = [r for r in runs if r[1] - r[0] + 1 >= MIN_RUN_LENGTH]
        # Defensive cap: at most 2 findings per year even if the run detector
        # over-segments for some reason.
        qualifying.sort(key=lambda r: r[1] - r[0], reverse=True)
        for start, end, direction in qualifying[:2]:
            labels, first_label, first_val = _run_report_data(order, start, end, direction)
            length = len(labels)
            dir_desc = (
                "reference is one row behind (shows the previous account's value)"
                if direction == -1
                else "reference is one row ahead (shows the next account's value)"
            )
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="row_alignment_suspected",
                    message=(
                        f"FY{year}: suspected row misalignment spanning {length} consecutive "
                        f"accounts from '{first_label}' to '{labels[-1]}' -- {dir_desc}. "
                        f"e.g. '{first_label}' = {_fmt(first_val)}."
                    ),
                    statement=ours.statement,
                    fiscal_year=year,
                    account=first_label,
                    detail={
                        "accounts": labels,
                        "length": length,
                        "shift": direction,
                        "example_account": first_label,
                        "example_value": first_val,
                    },
                )
            )
    return findings


def value_delta(ours: ConsolidatedTable, reference: ConsolidatedTable) -> list[Finding]:
    """WARNING, audit mode: same account, different value, no alignment
    pattern -- a genuine analyst adjustment."""
    findings: list[Finding] = []
    common_years = sorted(set(ours.years) & set(reference.years))
    for year in common_years:
        order, statuses, ref_vals = _year_shift_analysis(ours, reference, year)
        runs = _runs(order, statuses)
        qualifying_indices: set[int] = set()
        qualifying = [r for r in runs if r[1] - r[0] + 1 >= MIN_RUN_LENGTH]
        qualifying.sort(key=lambda r: r[1] - r[0], reverse=True)
        for start, end, _direction in qualifying[:2]:
            qualifying_indices.update(range(start, end + 1))

        for i, (_label, raw, our_val) in enumerate(order):
            if i in qualifying_indices:
                continue
            if i not in statuses:
                continue  # aligned, or reference has no comparable data
            ref_val = ref_vals.get(i)
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="value_delta",
                    message=(
                        f"FY{year}: '{raw}' = {_fmt(our_val)} in our extraction but "
                        f"{_fmt(ref_val)} in the reference (delta {_fmt(ref_val - our_val) if ref_val is not None else 'None'})"
                    ),
                    statement=ours.statement,
                    fiscal_year=year,
                    account=raw,
                    detail={"our_value": our_val, "reference_value": ref_val, "delta": (ref_val - our_val) if ref_val is not None else None},
                )
            )
    return findings


def audit_against_reference(ours: ConsolidatedTable, reference: ConsolidatedTable) -> list[Finding]:
    """Audit mode entry point. Emits row_alignment_suspected and value_delta
    findings, both with fiscal_year set."""
    return row_alignment_suspected(ours, reference) + value_delta(ours, reference)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_all(ss: StatementSet, tables: dict[StatementType, "ConsolidatedTable"]) -> ValidationReport:
    """Every non-audit check. Returns a ValidationReport."""
    report = ValidationReport()

    for col in ss.columns:
        if col.role is not ColumnRole.PRIMARY:
            continue
        report.extend(subtotal_mismatch(col))
        report.extend(check_hierarchy(col))
        report.extend(unparsed_row(col))
        if col.statement is StatementType.BS:
            report.extend(balance_sheet_unbalanced(col))

    report.extend(check_comparative_agreement(ss))

    for table in tables.values():
        report.extend(account_added(table))
        report.extend(account_removed(table))
        report.extend(account_renamed(table))

    return report
