"""ODS reader: parse content.xml directly, no third spreadsheet library.

openpyxl does not read .ods, and pulling in odfpy for one format -- when the
part of the schema we actually need (tables, rows, cells, values) is this
small -- is not worth the dependency. stdlib `zipfile` + `xml.etree` is
sufficient and keeps the format matrix (pdf/ods/xlsx) on three readers, zero
new third-party packages.

Two traps, both verified against the real sample files:

1. **Repeat counts can be enormous.** ODS represents unused trailing rows and
   columns with `table:number-rows-repeated="1048576"` /
   `table:number-columns-repeated="1024"` rather than simply omitting them.
   Expanding those literally allocates gigabytes for a 96-row sheet. Anything
   above `_MAX_REPEAT` is padding, not content, and is clamped to 1.

2. **The display text lies for numbers.** `<text:p>` holds the formatted,
   often-rounded string ("1,990,148.32" or worse, locale-rounded). The exact
   value lives in the `office:value` attribute for float/currency/percentage
   cells and must be read from there, never from the paragraph text.

3. **Not every text cell is a label.** QuickBooks' print header puts page
   furniture -- a print timestamp, a print date -- in column A, one row above
   and one row below where the real label ("Balance Sheet") sits in column B.
   Taking "first non-empty text cell" per row would read the timestamp as a
   label and the actual title as furniture. Instead we tally, once per part,
   which column carries the most non-empty text cells across every row, and
   treat *that* column as the label column for the whole part. Text in any
   other column is furniture: it has no `label`, and its text is pushed to
   `part.captions` instead so `interpret.py` still sees it.

Depth is never recovered here: this corpus's ODS files are QuickBooks'
Excel-style export, and the account-nesting indentation that the PDF reader
recovers from x-offsets simply is not present in this XML -- not as a column
offset, not as leading whitespace. `RawRow.depth` stays `None` throughout;
`interpret.py` falls back to its heuristics for these sources.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from fsa.ingest.raw import RawDoc, RawPart, RawRow, UnreadableSource

_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"

_TABLE_TAG = f"{{{_TABLE_NS}}}table"
_ROW_TAG = f"{{{_TABLE_NS}}}table-row"
_CELL_TAG = f"{{{_TABLE_NS}}}table-cell"
_COVERED_CELL_TAG = f"{{{_TABLE_NS}}}covered-table-cell"
_P_TAG = f"{{{_TEXT_NS}}}p"

_NAME_ATTR = f"{{{_TABLE_NS}}}name"
_COLS_REP_ATTR = f"{{{_TABLE_NS}}}number-columns-repeated"
_ROWS_REP_ATTR = f"{{{_TABLE_NS}}}number-rows-repeated"
_VALUE_TYPE_ATTR = f"{{{_OFFICE_NS}}}value-type"
_VALUE_ATTR = f"{{{_OFFICE_NS}}}value"

_NUMERIC_TYPES = {"float", "currency", "percentage"}

#: A repeat count above this is sheet padding (ODS's nominal 1024x1048576
#: extent), not real content. See module docstring, trap 1.
_MAX_REPEAT = 200

#: Cap on how many blank-of-numbers rows feed `RawPart.captions` (PLAN
#: contract, shared with the other readers).
_MAX_CAPTIONS = 40


def read(path: Path) -> RawDoc:
    """Read an .ods spreadsheet into the universal intermediate.

    One `RawPart` per `table:table`. Raises `UnreadableSource` only if the
    file cannot be opened as a zip or `content.xml` cannot be parsed --
    an empty sheet is not an error, it just yields a `RawPart` with no rows.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            data = zf.read("content.xml")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise UnreadableSource(f"cannot open {path.name} as .ods: {exc}") from exc

    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise UnreadableSource(
            f"cannot parse {path.name}'s content.xml: {exc}"
        ) from exc

    out = RawDoc(source=path, kind="ods")
    for table_el in root.iter(_TABLE_TAG):
        out.parts.append(_read_table(table_el))
    return out


def _repeat(el: ET.Element, attr: str) -> int:
    """Read a `number-*-repeated` attribute, clamping padding to 1 (trap 1)."""
    raw = el.get(attr)
    if raw is None:
        return 1
    try:
        n = int(raw)
    except ValueError:
        return 1
    if n < 1 or n > _MAX_REPEAT:
        return 1
    return n


def _cell_text(cell_el: ET.Element) -> str:
    """Join every `text:p` child's text. `itertext()` only works per-element."""
    paras = cell_el.findall(_P_TAG)
    return "\n".join("".join(p.itertext()) for p in paras)


def _row_cells(row_el: ET.Element) -> list[tuple[int, bool, str | None, float | None]]:
    """Expand one row's cells to (column, is_numeric, text, value) tuples.

    Column-repeated cells are replicated at their clamped count so later
    column-index math (locators) stays correct for cells after them.
    """
    out: list[tuple[int, bool, str | None, float | None]] = []
    col = 0
    for cell_el in row_el:
        if cell_el.tag not in (_CELL_TAG, _COVERED_CELL_TAG):
            continue
        rep = _repeat(cell_el, _COLS_REP_ATTR)
        value_type = cell_el.get(_VALUE_TYPE_ATTR)
        is_numeric = value_type in _NUMERIC_TYPES

        text: str | None = None
        value: float | None = None
        if is_numeric:
            raw_val = cell_el.get(_VALUE_ATTR)
            if raw_val is not None:
                try:
                    value = float(raw_val)
                except ValueError:
                    value = None
        else:
            text = _cell_text(cell_el)

        for _ in range(rep):
            col += 1
            out.append((col, is_numeric, text, value))
    return out


def _col_letter(idx: int) -> str:
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _dominant_label_column(
    row_records: list[tuple[int, list[tuple[int, bool, str | None, float | None]]]]
) -> int | None:
    """The column with the most non-empty text cells across the whole part.

    Ties (and the no-text-anywhere case) resolve to the leftmost column --
    see module docstring, trap 3.
    """
    counts: dict[int, int] = {}
    for _, cells in row_records:
        for col, is_numeric, text, _value in cells:
            if not is_numeric and text and text.strip():
                counts[col] = counts.get(col, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    return min(col for col, n in counts.items() if n == best)


def _build_row(
    row_num: int,
    cells: list[tuple[int, bool, str | None, float | None]],
    label_col: int | None,
) -> tuple[RawRow, str | None]:
    """Build one row, returning it alongside a caption candidate (or None).

    `label` comes only from `label_col`. Non-empty text in any other column
    is furniture: it is never a label, and is surfaced as the caption
    candidate instead when the row has no real label of its own.
    """
    label: str | None = None
    stray: str | None = None
    stray_col: int | None = None
    first_numeric_col: int | None = None
    values: list[float | None] = []

    for col, is_numeric, text, value in cells:
        if is_numeric:
            values.append(value)
            if first_numeric_col is None:
                first_numeric_col = col
            continue
        if not text or not text.strip():
            continue
        stripped = text.strip()
        if col == label_col:
            label = stripped
        elif stray is None:
            stray, stray_col = stripped, col

    if label is not None:
        locator_col = label_col
    elif stray is not None:
        locator_col = stray_col
    else:
        locator_col = first_numeric_col
    locator = f"{_col_letter(locator_col)}{row_num}" if locator_col else ""
    truncated = bool(label) and (label.endswith("...") or label.endswith("…"))

    row = RawRow(
        index=row_num,
        label=label,
        values=values,
        depth=None,
        locator=locator,
        truncated=truncated,
        value_cols=[c for c, is_num, _t, v in cells if is_num and v is not None],
        texts=[(c, t.strip()) for c, is_num, t, _v in cells
               if not is_num and t and t.strip()],
    )

    if label is not None:
        caption = label if not any(v is not None for v in values) else None
    else:
        caption = stray

    return row, caption


def _read_table(table_el: ET.Element) -> RawPart:
    part = RawPart(name=table_el.get(_NAME_ATTR) or "")

    row_records: list[tuple[int, list[tuple[int, bool, str | None, float | None]]]] = []
    row_num = 0
    for row_el in table_el.iter(_ROW_TAG):
        rep = _repeat(row_el, _ROWS_REP_ATTR)
        cells = _row_cells(row_el)
        for _ in range(rep):
            row_num += 1
            row_records.append((row_num, cells))

    label_col = _dominant_label_column(row_records)

    for row_num, cells in row_records:
        row, caption = _build_row(row_num, cells, label_col)
        part.rows.append(row)
        if caption and len(part.captions) < _MAX_CAPTIONS:
            part.captions.append(caption)

    return part
