"""The analyst review surface: a plain Excel workbook, round-tripped.

Design premise: the mapping will never be perfect, so reviewing and correcting
it is a first-class step, not an afterthought. These are Excel people -- giving
them a spreadsheet with a dropdown beats any UI we could build and ask them to
learn.

Four decisions, each driven by how the review actually gets done:

1. **Sorted by risk, not alphabetically.** Nobody reads 170 rows evenly. Rows
   needing attention float to the top, and within that, by descending amount --
   so the biggest uncertain numbers are the first thing on screen.

2. **The amounts are shown.** We deliberately never send figures to the model
   (PLAN 7.1), but a human cannot judge a mapping without materiality: a $50
   misclassification is noise, a $3,000,000 one is the FY2022 bug (PLAN 2.4).

3. **One editable column.** The analyst overwrites the proposed target in
   place -- no parallel "override" column to reconcile. A validated dropdown
   prevents typos, and free text is still permitted so they can name a brand
   new line the tool never considered.

4. **Leaving a row alone means accepting it.** That is what review means. The
   read-back reports how many were changed vs accepted so the split is visible.

openpyxl is used here deliberately and safely: this is a scratch workbook we
create ourselves. It remains forbidden for the BVAL template (PLAN 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from fsa.ingest.normalize import normalize
from fsa.mapping.template import TemplateSpec
from fsa.model.mapping import (
    Decider,
    Exclusion,
    MappingRule,
    MappingSet,
    Target,
    TargetKind,
)
from fsa.model.schema import ConsolidatedTable, RowKind, StatementType

EXCLUDE_TOKEN = "-- EXCLUDE --"
_LOOKUP_SHEET = "_targets"

_HEADERS = [
    ("Review?", 9),
    ("Client account", 42),
    ("Amount (latest yr)", 18),
    ("Map to  <-- EDIT THIS", 34),
    ("Rolls into", 26),
    ("Sign", 6),
    ("Proposed by", 14),
    ("Conf", 7),
    ("Why", 46),
    ("Your notes", 26),
    # Hidden. The client account's *display* label is not unique -- Commercial
    # Flooring's balance sheet prints "Total Credit Cards" twice, at two nesting
    # levels, and the consolidator disambiguates them by section. Reading the
    # sheet back by display label therefore applied one row's decision to both
    # and reported the other as an unknown account. This carries the identity.
    ("_key", 30),
]

_COL = {name: get_column_letter(i + 1) for i, (name, _) in enumerate(_HEADERS)}
_TARGET_COL = _COL["Map to  <-- EDIT THIS"]
_KEY_INDEX = len(_HEADERS)  # 1-based column of the hidden identity key

_RED = PatternFill("solid", fgColor="FFC7CE")
_AMBER = PatternFill("solid", fgColor="FFEB9C")
_GREEN = PatternFill("solid", fgColor="C6EFCE")
_HEAD = PatternFill("solid", fgColor="1F3864")

#: Below this confidence a proposal is flagged amber even if it came from a
#: layer we normally trust.
LOW_CONFIDENCE = 0.85


@dataclass
class ReviewStats:
    total: int = 0
    changed: int = 0
    accepted: int = 0
    excluded: int = 0
    unresolved_left: int = 0
    notes: list[str] = field(default_factory=list)


def _latest_amount(table: ConsolidatedTable, norm: str) -> float | None:
    for row in table.rows:
        if row.norm_label == norm:
            for y in sorted(row.values, reverse=True):
                v = row.values.get(y)
                if isinstance(v, (int, float)):
                    return float(v)
    return None


def _risk(
    rule: MappingRule, amount: float | None, is_subtotal: bool = False
) -> tuple[int, int, float]:
    """Sort key: attention first, detail before rollups, then by size.

    The middle term matters more than it looks. A client subtotal is large by
    construction, so ranking purely on amount floats `Total Assets`,
    `Total Current Assets` and `Total Liabilities & Owners Equity` above every
    real decision -- the analyst opens the sheet to a screen of noise. A
    subtotal is also a *secondary* decision: whether to map the rollup follows
    from what you did with its detail, so it belongs underneath.
    """
    if rule.decided_by is Decider.UNRESOLVED:
        tier = 0
    elif rule.verified is False:
        tier = 0
    elif not rule.decided_by.is_confirmed and rule.confidence < LOW_CONFIDENCE:
        tier = 1
    elif not rule.decided_by.is_confirmed:
        tier = 2
    else:
        tier = 3
    return (tier, 1 if is_subtotal else 0, -abs(amount or 0.0))


def export_review(
    sets: dict[StatementType, MappingSet],
    tables: dict[StatementType, ConsolidatedTable],
    spec: TemplateSpec,
    path: Path,
    *,
    client_name: str = "",
) -> Path:
    """Write the review workbook. One sheet per statement, plus a lookup sheet."""
    wb = Workbook()
    wb.remove(wb.active)

    lookups: dict[str, list[str]] = {}

    for st, ms in sets.items():
        table = tables[st]
        ws = wb.create_sheet(st.value)

        ws["A1"] = (
            f"{client_name or 'Client'} -- {st.value} mapping review.  "
            f"Edit column {_TARGET_COL} only. Leave a row unchanged to accept it. "
            f"Type '{EXCLUDE_TOKEN}' to drop an account."
        )
        ws["A1"].font = Font(bold=True, size=11)
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(_HEADERS))

        for i, (name, width) in enumerate(_HEADERS, start=1):
            c = ws.cell(row=2, column=i, value=name)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = _HEAD
            c.alignment = Alignment(vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(i)].width = width
        ws.column_dimensions[get_column_letter(_KEY_INDEX)].hidden = True
        ws.freeze_panes = "C3"

        subtotals = {
            row.norm_label for row in table.rows if row.kind is RowKind.SUBTOTAL
        }

        # Exclusions must appear too. `reconcile` auto-excludes every account a
        # mapped rollup already covers -- 40 rows on sample 2's income statement
        # -- and a decision the analyst cannot see is a decision they cannot
        # reverse. They are shown pre-filled with the exclude token, so leaving
        # one alone accepts it and overtyping it maps the account after all.
        shown = list(ms.rules) + [
            MappingRule(
                client_account=e.client_account,
                norm_account=e.norm_account,
                target=Target(statement=st, label=EXCLUDE_TOKEN),
                decided_by=e.decided_by,
                confidence=1.0,
                rationale=e.reason,
            )
            for e in ms.exclusions
        ]
        ranked = sorted(
            shown,
            key=lambda r: _risk(
                r, _latest_amount(table, r.norm_account), r.norm_account in subtotals
            ),
        )

        for i, rule in enumerate(ranked):
            r = 3 + i
            amount = _latest_amount(table, rule.norm_account)
            unresolved = rule.decided_by is Decider.UNRESOLVED
            target = "" if unresolved else rule.target.label

            block = ""
            line = spec.find(st, target) if target else None
            if line is not None and line.block:
                block = line.block
            elif rule.target.block:
                block = rule.target.block

            ws.cell(row=r, column=1, value="REVIEW" if unresolved else "")
            ws.cell(row=r, column=2, value=rule.client_account)
            ac = ws.cell(row=r, column=3, value=amount)
            ac.number_format = "#,##0.00;(#,##0.00)"
            tc = ws.cell(row=r, column=4, value=target)
            ws.cell(row=r, column=5, value=block)
            ws.cell(row=r, column=6, value="-" if rule.sign < 0 else "+")
            ws.cell(row=r, column=7, value=rule.decided_by.value)
            cc = ws.cell(row=r, column=8, value=round(rule.confidence, 2))
            cc.number_format = "0.00"
            why = rule.rationale or ""
            if rule.norm_account in subtotals and not why:
                why = (
                    "client subtotal -- map this rollup only if you are NOT "
                    "mapping its component accounts, else it double-counts"
                )
            ws.cell(row=r, column=9, value=why)
            ws.cell(row=r, column=_KEY_INDEX, value=rule.norm_account)

            if unresolved:
                fill = _RED
            elif rule.decided_by.is_confirmed:
                fill = _GREEN
            elif rule.confidence < LOW_CONFIDENCE:
                fill = _AMBER
            else:
                fill = None
            if fill is not None:
                for col in (1, 2, 4, 7, 8):
                    ws.cell(row=r, column=col).fill = fill

        # Validated dropdown of every legal target for this statement, plus the
        # exclude token. `allow_blank` and a non-strict list keep free text
        # legal so an analyst can name a line we never proposed.
        options = sorted({l.label for l in spec.targets(st)}) + [EXCLUDE_TOKEN]
        lookups[st.value] = options

        last = 2 + len(ranked)
        if last >= 3:
            lk = _LOOKUP_SHEET
            col = get_column_letter(1 + list(sets).index(st))
            dv = DataValidation(
                type="list",
                formula1=f"'{lk}'!${col}$2:${col}${1 + len(options)}",
                allow_blank=True,
                showDropDown=False,  # False == show the dropdown arrow in Excel
            )
            dv.error = None
            dv.showErrorMessage = False  # warn, never block: free text is valid
            ws.add_data_validation(dv)
            dv.add(f"{_TARGET_COL}3:{_TARGET_COL}{last}")

    lk = wb.create_sheet(_LOOKUP_SHEET)
    lk["A1"] = "target options (generated -- do not edit)"
    for j, (name, options) in enumerate(lookups.items(), start=1):
        col = get_column_letter(j)
        lk[f"{col}1"] = name
        for i, opt in enumerate(options, start=2):
            lk[f"{col}{i}"] = opt
    lk.sheet_state = "hidden"

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def import_review(
    path: Path,
    sets: dict[StatementType, MappingSet],
    spec: TemplateSpec,
) -> tuple[dict[StatementType, MappingSet], ReviewStats]:
    """Read the reviewed workbook back into confirmed MappingSets.

    Anything the analyst left in place is treated as accepted and promoted to
    `Decider.HUMAN` -- that is what reviewing means, and it is what lets
    `build_plan(require_confirmed=True)` proceed. Rows still blank stay
    UNRESOLVED and are reported, never silently dropped.
    """
    wb = load_workbook(path, data_only=True)
    stats = ReviewStats()
    now = datetime.now(timezone.utc)
    out: dict[StatementType, MappingSet] = {}

    for st, ms in sets.items():
        if st.value not in wb.sheetnames:
            stats.notes.append(f"{st.value}: sheet missing from review file; left unconfirmed")
            out[st] = ms
            continue

        ws = wb[st.value]
        by_norm = {r.norm_account: r for r in ms.rules}
        # An excluded account can be *un*-excluded in review, so it needs a rule
        # to come back to. Exclusions are rebuilt from the sheet rather than
        # carried over, otherwise reversing one would leave it excluded anyway.
        for e in ms.exclusions:
            by_norm.setdefault(
                e.norm_account,
                MappingRule(
                    client_account=e.client_account,
                    norm_account=e.norm_account,
                    target=Target(statement=st, label=EXCLUDE_TOKEN),
                    decided_by=Decider.UNRESOLVED,
                    confidence=0.0,
                ),
            )
        new = MappingSet(statement=st)

        for row in ws.iter_rows(min_row=3, values_only=True):
            if not row or not row[1]:
                continue
            account = str(row[1]).strip()
            chosen = (str(row[3]).strip() if row[3] else "")
            # Prefer the hidden identity key; fall back to the display label so
            # a sheet from an older version, or a row an analyst typed by hand,
            # still resolves.
            key = str(row[_KEY_INDEX - 1]).strip() if len(row) >= _KEY_INDEX and row[_KEY_INDEX - 1] else ""
            norm = key or normalize(account)
            rule = by_norm.get(norm)
            if rule is None:
                stats.notes.append(f"{st.value}: unknown account in review file: {account}")
                continue

            stats.total += 1

            if chosen == EXCLUDE_TOKEN:
                new.exclusions.append(
                    Exclusion(
                        client_account=account,
                        norm_account=norm,
                        reason="excluded during review",
                        decided_by=Decider.HUMAN,
                        decided_at=now,
                    )
                )
                stats.excluded += 1
                continue

            if not chosen:
                rule.decided_by = Decider.UNRESOLVED
                new.rules.append(rule)
                stats.unresolved_left += 1
                continue

            was = rule.target.label if rule.decided_by is not Decider.UNRESOLVED else ""
            if normalize(chosen) != normalize(was):
                stats.changed += 1
                line = spec.find(st, chosen)
                if line is not None and line.is_target:
                    kind, block = TargetKind.ASSIGN, None
                else:
                    # A name the template does not have: the analyst is asking
                    # for a new line. Keep the block we already proposed, if any.
                    kind = TargetKind.INSERT
                    block = rule.target.block
                    if not block:
                        stats.notes.append(
                            f"{st.value}: {account!r} -> {chosen!r} needs a block; "
                            f"left unresolved"
                        )
                        rule.decided_by = Decider.UNRESOLVED
                        new.rules.append(rule)
                        stats.unresolved_left += 1
                        continue
                rule.target = Target(statement=st, label=chosen, kind=kind, block=block)
                rule.rationale = f"analyst override (was {was or 'unresolved'})"
            else:
                stats.accepted += 1

            rule.decided_by = Decider.HUMAN
            rule.confidence = 1.0
            rule.decided_at = now
            new.rules.append(rule)

        out[st] = new

    return out, stats
