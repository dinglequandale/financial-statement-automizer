"""Write a populated BVAL model through Excel COM automation.

PLAN.md section 4 explains why: openpyxl does not update formula references on
row insert, drops charts/conditional formatting on round-trip, and risks the
DealStats/CapIQ add-in state carried in the template's `veryHidden` sheets. A
single wrong-but-silent number is worse than any of that being slow, so this
module never re-serializes the workbook itself -- it drives real Excel, and
Excel updates every one of the 47+ sheets' formula references natively.

Consumes `fsa.write.plan.WritePlan` (row allocation + formula generation,
already decided and unit-tested before Excel ever opens) and
`fsa.mapping.template.TemplateSpec` (read via `load_template`, itself pure
openpyxl reading -- never writing). This module's only job is to *execute*
that plan against a live workbook and verify the result.

Two families of function:

  - Pure helpers (`std_year_column`, `historical_column`, `historical_rows`,
    `year_column_plan`) -- no COM, no file I/O, fully unit-testable.
  - `write_model` and its COM-touching internals -- require a real Excel
    installation, exercised only by `@pytest.mark.sample` tests.
"""

from __future__ import annotations

import gc
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from openpyxl.utils import get_column_letter

from fsa.mapping.template import TemplateLine, TemplateSpec, load_template
from fsa.model.schema import ConsolidatedTable, StatementType
from fsa.write.plan import RowAction, WritePlan, build_formula

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# The standardized BS/IS tabs carry exactly 7 period slots, columns E..K.
MAX_PERIODS = 7
FIRST_STD_COL = 5  # 'E'
LAST_STD_COL = 11  # 'K'

# Historical tabs: column A is the label, years start at column B.
FIRST_HIST_COL = 2  # 'B'

LABEL_COL = "C"

# Excel enum values (avoids depending on gencache/makepy for constants).
XL_CALCULATION_MANUAL = -4135
XL_CELL_TYPE_FORMULAS = -4123
XL_ERRORS = 16
MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3

_BALANCE_LABEL = "total liabilities and equity"


class ComWriterError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Pure helpers -- no Excel required, unit-tested directly.
# --------------------------------------------------------------------------


def std_year_column(year_index: int, n_years: int) -> str:
    """Standardized-tab column letter for the `year_index`-th of `n_years`.

    Right-aligned to K: the most recent year always lands in K, regardless of
    how many years of history exist, mirroring the delivered model (6 years
    of TS history occupy F..K, with E left as a hardcoded 0 baseline).
    """
    if n_years <= 0:
        raise ComWriterError(f"n_years must be positive, got {n_years}")
    if n_years > MAX_PERIODS:
        raise ComWriterError(
            f"the template has only {MAX_PERIODS} period slots (E..K), got n_years={n_years}"
        )
    if not 0 <= year_index < n_years:
        raise ComWriterError(f"year_index {year_index} out of range for n_years={n_years}")
    col_num = LAST_STD_COL - n_years + 1 + year_index
    return get_column_letter(col_num)


def historical_column(year_index: int) -> str:
    """Historical-tab column letter for the `year_index`-th year (0-based, ascending)."""
    if year_index < 0:
        raise ComWriterError(f"year_index must be >= 0, got {year_index}")
    return get_column_letter(FIRST_HIST_COL + year_index)


def historical_rows(table: ConsolidatedTable, start_row: int = 2) -> dict[str, int]:
    """norm_label -> the row it will land on when the Historical tab is written.

    Deterministic and pure so a caller can compute `hist_rows` for
    `build_plan()` *before* Excel is ever opened, and `write_model` reproduces
    the identical layout when it actually writes the tab. Row 1 is reserved
    for a header (account name + year labels); data starts at `start_row`.
    """
    return {row.norm_label: start_row + i for i, row in enumerate(table.rows)}


def year_column_plan(years: list[int]) -> tuple[dict[int, tuple[str, str]], list[str]]:
    """year -> (historical_column, standardized_column), plus any warnings.

    Every year gets a historical column (that tab has no capacity limit).
    Only the most recent `MAX_PERIODS` years get a standardized column, since
    the template has exactly 7 period slots; older years are recorded in the
    warning and simply not mapped onto BS/IS -- "only write columns for years
    that actually exist" in the sense of physically fitting the template.
    """
    years = sorted(years)
    warnings: list[str] = []
    hist_map = {y: historical_column(i) for i, y in enumerate(years)}

    std_years = years[-MAX_PERIODS:] if years else []
    if len(years) > MAX_PERIODS:
        dropped = years[: -MAX_PERIODS]
        warnings.append(
            f"{len(dropped)} year(s) exceed the template's {MAX_PERIODS} period "
            f"slots and were not mapped onto the standardized tab: {dropped}"
        )

    n = len(std_years)
    out: dict[int, tuple[str, str]] = {}
    for i, y in enumerate(std_years):
        out[y] = (hist_map[y], std_year_column(i, n))
    return out, warnings


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


@dataclass
class WriteReport:
    out_path: Path
    hist_rows: dict[StatementType, dict[str, int]] = field(default_factory=dict)
    n_inserts: dict[StatementType, int] = field(default_factory=dict)
    n_renames: dict[StatementType, int] = field(default_factory=dict)
    n_repurposes: dict[StatementType, int] = field(default_factory=dict)
    balance_check: dict[int, float] = field(default_factory=dict)  # year -> diff (0 == balanced)
    warnings: list[str] = field(default_factory=list)
    balanced: bool = True


# --------------------------------------------------------------------------
# COM lifecycle
# --------------------------------------------------------------------------


@contextmanager
def _excel_session(visible: bool = False):
    """A single, isolated Excel.Application instance, always cleaned up.

    `DispatchEx` (not `Dispatch`) forces a brand-new process rather than
    reusing/attaching to a user's already-open Excel, so `Quit()` here can
    never affect -- or leave dangling -- someone else's session. Everything
    that can leave an orphan EXCEL.EXE behind is wrapped in `finally`.
    """
    import pythoncom
    import win32com.client as win32

    pythoncom.CoInitialize()
    app = None
    try:
        app = win32.DispatchEx("Excel.Application")
        app.Visible = visible
        app.DisplayAlerts = False
        app.ScreenUpdating = False
        app.EnableEvents = False
        app.AskToUpdateLinks = False
        try:
            app.AutomationSecurity = MSO_AUTOMATION_SECURITY_FORCE_DISABLE
        except Exception:
            pass  # not fatal if unsupported on this Excel build
        # NOTE: `Application.Calculation` is deliberately NOT set here. Excel
        # rejects it while no workbook is open ("Unable to set the Calculation
        # property of the Application class") because calculation mode is a
        # property of the active workbook's window, not of the application.
        # It is set in `set_manual_calculation()` once a workbook exists.
        yield app
    finally:
        if app is not None:
            try:
                app.DisplayAlerts = False
                for wb in list(app.Workbooks):
                    try:
                        wb.Close(SaveChanges=False)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                app.Quit()
            except Exception:
                pass
            del app
        gc.collect()
        pythoncom.CoUninitialize()


def set_manual_calculation(app) -> bool:
    """Switch Excel to manual calculation. Call only once a workbook is open.

    Returns whether it took effect. Manual calculation is an optimization --
    it stops Excel recalculating 47 sheets after every single write -- so a
    failure here is worth reporting but must not abort the write.
    """
    try:
        app.Calculation = XL_CALCULATION_MANUAL
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Internals -- each touches COM, none is independently unit-tested.
# --------------------------------------------------------------------------


def _sheet_names(wb) -> list[str]:
    return [s.Name for s in wb.Sheets]


def _get_or_add_sheet(wb, name: str):
    if name in _sheet_names(wb):
        ws = wb.Sheets(name)
        ws.Cells.Clear()
        return ws
    ws = wb.Sheets.Add(None, wb.Sheets(wb.Sheets.Count))
    ws.Name = name
    return ws


def _write_historical(wb, table: ConsolidatedTable, sheet_name: str) -> None:
    """Column A = raw_label, then one column per year ascending, values only."""
    ws = _get_or_add_sheet(wb, sheet_name)
    years = sorted(table.years)

    header = ["Account", *years]
    grid = [tuple(header)]
    for row in table.rows:
        grid.append((row.raw_label, *[row.values.get(y) for y in years]))

    n_rows = len(grid)
    n_cols = len(years) + 1
    rng = ws.Range(ws.Cells(1, 1), ws.Cells(n_rows, n_cols))
    rng.Value = tuple(grid)


def _lines_by_row(spec: TemplateSpec, statement: StatementType) -> dict[int, TemplateLine]:
    return {l.row: l for l in spec.lines if l.statement is statement}


def _write_actions(
    ws,
    plan: WritePlan,
    statement: StatementType,
    hist_sheet: str,
    year_cols: dict[int, tuple[str, str]],
    lines_by_row: dict[int, TemplateLine],
) -> tuple[int, int, int]:
    """Execute one statement's row actions bottom-up, then its formulas.

    Bottom-up (highest planned row first) is the entire point: `RowAction.row`
    is computed once, before any insert happens, against the *original*
    template layout. `Rows(n).Insert()` only ever shifts rows at or below n
    downward -- never rows above it. So processing from the highest row down,
    every row we are about to touch is still exactly where the plan says it
    is, and once we have written to it we never need its coordinate again
    (later inserts may relocate the physical row, but its content already
    landed correctly).
    """
    n_insert = n_rename = n_repurpose = 0

    for action in sorted(plan.actions, key=lambda a: a.row, reverse=True):
        if action.kind == "insert":
            ws.Rows(action.row).Insert()
            n_insert += 1
        else:
            line = lines_by_row.get(action.row)
            if line is not None and line.derived and not line.collapsible:
                raise ComWriterError(
                    f"refusing to write into a derived line: {statement.value}!"
                    f"{LABEL_COL}{action.row} ({line.label!r}) is computed by the "
                    "template, not an input"
                )

        if action.kind in ("insert", "rename_slot", "repurpose"):
            ws.Range(f"{LABEL_COL}{action.row}").Value = action.target_label
            if action.kind == "rename_slot":
                n_rename += 1
            elif action.kind == "repurpose":
                n_repurpose += 1

        sources = plan.sources.get(action.target_label)
        if not sources:
            continue
        for _year, (hist_col, std_col) in year_cols.items():
            formula = build_formula(hist_sheet, hist_col, sources)
            if formula:
                ws.Range(f"{std_col}{action.row}").Formula = formula

    return n_insert, n_rename, n_repurpose


def _write_inputs(
    wb,
    entity_name: str | None,
    valuation_date: date | None,
    n_periods: int,
) -> None:
    ws = wb.Sheets("Inputs")
    if entity_name is not None:
        ws.Range("B15").Value = entity_name
    if valuation_date is not None:
        ws.Range("B9").Value = datetime(
            valuation_date.year, valuation_date.month, valuation_date.day
        )
    ws.Range("B34").Value = n_periods


def find_errors(ws) -> dict[str, str]:
    """address -> error text (e.g. "#DIV/0!") for every formula-error cell on `ws`.

    Uses `SpecialCells(xlCellTypeFormulas, xlErrors)` -- one COM round trip
    that lets Excel itself find every error cell, rather than reading the
    whole used range back into Python and scanning it. `.Value` collapses
    every error type to the same opaque sentinel (-2146826281), so `.Text`
    (a handful of per-cell reads, since error cells are rare) is what
    actually distinguishes "#REF!" from "#DIV/0!".
    """
    import pywintypes

    try:
        rng = ws.UsedRange.SpecialCells(XL_CELL_TYPE_FORMULAS, XL_ERRORS)
    except pywintypes.com_error:
        return {}  # "No cells were found" -- the clean, expected case
    if rng is None:
        return {}
    # `.Address` is exposed as a plain property under dynamic (non-gencache)
    # dispatch -- calling it like a method (`.Address(False, False)`) fails
    # with "'str' object is not callable". The default (absolute, e.g.
    # "$E$62") is precise enough for an error report.
    return {f"{ws.Name}!{cell.Address}": cell.Text for cell in rng.Cells}


def find_ref_errors(ws) -> list[str]:
    """Cell addresses on `ws` holding specifically a #REF! error.

    This -- not any error cell -- is the signature of a broken row insert (a
    formula whose referenced range was invalidated by a mis-shifted insert).
    A blank BVAL template already carries dozens of #DIV/0! cells (margin
    ratios dividing by a still-zero Revenue on an unmapped period); those are
    ordinary template behavior on a partially populated model, not corruption,
    and must not be confused with #REF!.
    """
    return [addr for addr, text in find_errors(ws).items() if text == "#REF!"]


def _find_balance_check_row(ws) -> int:
    used = ws.UsedRange
    last_row = used.Row + used.Rows.Count - 1
    values = ws.Range(f"{LABEL_COL}1:{LABEL_COL}{last_row}").Value
    for i, cell in enumerate(values, start=1):
        v = cell[0] if isinstance(cell, tuple) else cell
        if isinstance(v, str) and v.strip().casefold() == _BALANCE_LABEL:
            return i + 1
    raise ComWriterError(
        f"could not locate {_BALANCE_LABEL!r} on {ws.Name} to verify the balance check"
    )


def _read_balance_check(ws, year_cols: dict[int, tuple[str, str]]) -> dict[int, float]:
    check_row = _find_balance_check_row(ws)
    out: dict[int, float] = {}
    for year, (_hist_col, std_col) in sorted(year_cols.items()):
        val = ws.Range(f"{std_col}{check_row}").Value
        if val is None or (isinstance(val, str) and val.strip() == ""):
            out[year] = 0.0
        else:
            try:
                out[year] = float(val)
            except (TypeError, ValueError):
                out[year] = float("nan")
    return out


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def write_model(
    template: Path,
    out: Path,
    tables: dict[StatementType, ConsolidatedTable],
    plans: dict[StatementType, WritePlan],
    *,
    entity_name: str | None = None,
    valuation_date: date | None = None,
    visible: bool = False,
    overwrite: bool = False,
) -> WriteReport:
    """Write a populated BVAL model. See module docstring for the constraint
    that shapes everything here: writing happens through Excel COM, never
    openpyxl, and `template` is opened by nothing but `shutil.copy2`.
    """
    template = Path(template)
    out = Path(out)

    if not template.exists():
        raise ComWriterError(f"template not found: {template}")
    if out.exists() and not overwrite:
        raise ComWriterError(f"refusing to overwrite existing file: {out} (pass overwrite=True)")
    if not tables:
        raise ComWriterError("no tables supplied")
    for statement, plan in plans.items():
        if statement not in tables:
            raise ComWriterError(f"plan given for {statement.value} but no table supplied")

    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, out)  # the only file operation ever done on `template`

    # Read-only: reflects the file exactly as copied, before COM touches it.
    spec = load_template(out)

    report = WriteReport(out_path=out)
    for statement, table in tables.items():
        report.hist_rows[statement] = historical_rows(table)

    with _excel_session(visible=visible) as app:
        wb = app.Workbooks.Open(str(out.resolve()), UpdateLinks=0, IgnoreReadOnlyRecommended=True)
        # Only now is Calculation settable -- see set_manual_calculation().
        if not set_manual_calculation(app):
            report.warnings.append(
                "could not switch Excel to manual calculation; write will be slower"
            )
        try:
            for statement, table in tables.items():
                _write_historical(wb, table, f"Historical {statement.value}")

            year_cols_by_stmt: dict[StatementType, dict[int, tuple[str, str]]] = {}
            for statement, plan in plans.items():
                ws = wb.Sheets(statement.value)
                hist_sheet = f"Historical {statement.value}"
                year_cols, warn = year_column_plan(tables[statement].years)
                year_cols_by_stmt[statement] = year_cols
                report.warnings.extend(warn)

                n_ins, n_ren, n_rep = _write_actions(
                    ws, plan, statement, hist_sheet, year_cols, _lines_by_row(spec, statement)
                )
                report.n_inserts[statement] = n_ins
                report.n_renames[statement] = n_ren
                report.n_repurposes[statement] = n_rep
                report.warnings.extend(plan.warnings)

            n_periods = 0
            if StatementType.BS in year_cols_by_stmt:
                n_periods = len(year_cols_by_stmt[StatementType.BS])
            elif StatementType.IS in year_cols_by_stmt:
                n_periods = len(year_cols_by_stmt[StatementType.IS])
            _write_inputs(wb, entity_name, valuation_date, n_periods)

            app.CalculateFullRebuild()

            if StatementType.BS in plans:
                ws_bs = wb.Sheets(StatementType.BS.value)
                report.balance_check = _read_balance_check(ws_bs, year_cols_by_stmt[StatementType.BS])
                report.balanced = all(abs(v) < 0.01 for v in report.balance_check.values())

            for statement in plans:
                ws = wb.Sheets(statement.value)
                errs = find_ref_errors(ws)
                if errs:
                    report.balanced = False
                    report.warnings.append(f"{statement.value}: formula errors at {errs}")

            wb.Save()
        finally:
            try:
                wb.Close(SaveChanges=False)
            except Exception:
                pass
            del wb

    return report
