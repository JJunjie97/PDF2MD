from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser


TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.IGNORECASE | re.DOTALL)


@dataclass(slots=True)
class TableCell:
    text: str
    colspan: int = 1
    rowspan: int = 1
    header: bool = False


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[TableCell]] = []
        self._row: list[TableCell] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_attrs: dict[str, str | None] = {}
        self._cell_header = False

    @staticmethod
    def _positive_span(value: str | None) -> int:
        try:
            return max(1, min(100, int(value or "1")))
        except ValueError:
            return 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = dict(attrs)
        if tag == "tr":
            self._finish_row()
            self._row = []
        elif tag in {"td", "th"}:
            self._finish_cell()
            if self._row is None:
                self._row = []
            self._cell_parts = []
            self._cell_attrs = attributes
            self._cell_header = tag == "th"
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts.append(" ")
        elif tag == "img" and self._cell_parts is not None:
            source = (attributes.get("src") or "").strip()
            alternate = (attributes.get("alt") or "").strip()
            if source:
                self._cell_parts.append(f"![{alternate}]({source})")
            elif alternate:
                self._cell_parts.append(alternate)
        elif tag in {"p", "div", "li"} and self._cell_parts:
            self._cell_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"}:
            self._finish_cell()
        elif tag == "tr":
            self._finish_row()
        elif tag in {"p", "div", "li"} and self._cell_parts:
            self._cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def close(self) -> None:
        super().close()
        self._finish_cell()
        self._finish_row()

    def _finish_cell(self) -> None:
        if self._cell_parts is None:
            return
        text = re.sub(r"\s+", " ", "".join(self._cell_parts)).strip()
        assert self._row is not None
        self._row.append(
            TableCell(
                text=text,
                colspan=self._positive_span(self._cell_attrs.get("colspan")),
                rowspan=self._positive_span(self._cell_attrs.get("rowspan")),
                header=self._cell_header,
            )
        )
        self._cell_parts = None
        self._cell_attrs = {}
        self._cell_header = False

    def _finish_row(self) -> None:
        self._finish_cell()
        if self._row is not None and any(cell.text for cell in self._row):
            self.rows.append(self._row)
        self._row = None


def _expand_table_grid(rows: list[list[TableCell]]) -> list[list[str]]:
    grid: list[list[str]] = []
    active_rowspans: dict[int, int] = {}

    for cells in rows:
        row: list[str] = []
        column = 0

        def fill_active() -> None:
            nonlocal column
            while active_rowspans.get(column, 0) > 0:
                row.append("")
                active_rowspans[column] -= 1
                if active_rowspans[column] <= 0:
                    del active_rowspans[column]
                column += 1

        for cell in cells:
            fill_active()
            for offset in range(cell.colspan):
                row.append(cell.text if offset == 0 else "")
                if cell.rowspan > 1:
                    active_rowspans[column] = cell.rowspan - 1
                column += 1
        fill_active()
        grid.append(row)

    width = max((len(row) for row in grid), default=0)
    return [row + [""] * (width - len(row)) for row in grid]


def _header_row_index(rows: list[list[TableCell]], grid: list[list[str]]) -> int:
    for index, row in enumerate(rows):
        if any(cell.header for cell in row):
            return index
    for index, (cells, expanded) in enumerate(zip(rows, grid)):
        nonempty = sum(bool(value.strip()) for value in expanded)
        if nonempty >= 2 and all(cell.colspan == 1 for cell in cells):
            return index
    return 0


def _escape_cell(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value.replace("|", "\\|")


def _render_markdown_table(rows: list[list[TableCell]]) -> str | None:
    if not rows:
        return None
    grid = _expand_table_grid(rows)
    if not grid or len(grid[0]) < 2:
        return None

    header_index = _header_row_index(rows, grid)
    header = [_escape_cell(value) for value in grid[header_index]]
    for index, value in enumerate(header):
        if not value:
            header[index] = f"Column {index + 1}"

    prelude: list[str] = []
    for row in grid[:header_index]:
        values = [_escape_cell(value) for value in row if value.strip()]
        if len(values) == 1:
            prelude.append(f"**{values[0]}**")

    rendered = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row_index, row in enumerate(grid):
        if row_index == header_index or row_index < header_index:
            continue
        values = [_escape_cell(value) for value in row]
        original_cells = rows[row_index]
        if (
            sum(bool(value) for value in values) == 1
            and any(cell.colspan > 1 for cell in original_cells)
        ):
            first = next((index for index, value in enumerate(values) if value), 0)
            values[first] = f"**{values[first]}**"
        rendered.append("| " + " | ".join(values) + " |")

    if prelude:
        return "\n\n".join((*prelude, "\n".join(rendered)))
    return "\n".join(rendered)


def _convert_table_fragment(fragment: str) -> str:
    parser = _TableParser()
    try:
        parser.feed(fragment)
        parser.close()
        rendered = _render_markdown_table(parser.rows)
    except Exception:
        return fragment
    return rendered or fragment


def convert_html_tables(content: str) -> str:
    """Convert simple MinerU HTML tables to GFM Markdown tables."""
    return TABLE_RE.sub(lambda match: _convert_table_fragment(match.group(0)), content)
