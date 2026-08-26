"""Export the web collector's CSV files to one XLSX workbook.

This module has no external-service dependency. It is used by the PowerShell
commands after browser-based Reel and follower collection finishes.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape


BASE_DIR = Path(__file__).resolve().parent.parent
XLSX_FILENAME = "instagram_data.xlsx"
XLSX_INVALID_XML_CHARACTER_PATTERN = re.compile(
    r"[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]"
)
XLSX_NUMERIC_FIELDS = {"collection_number", "view_count", "follower_count", "follower_count_change", "like_count", "comment_count", "repost_count"}
XLSX_PERCENT_FIELDS: set[str] = set()
XLSX_DELTA_FIELDS = {"view_count", "like_count", "comment_count", "repost_count", "follower_count"}
XLSX_DATE_FIELDS = {"collected_at", "uploaded_at"}
XLSX_TEXT_IDENTIFIER_FIELDS = {"user_id"}
XLSX_USERS_FIELDS = [
    "collection_number",
    "user_id",
    "username",
    "biography",
    "follower_count",
    "follower_count_change",
    "collected_at",
]
XLSX_REELS_WEB_FIELDS = [
    "url",
    "collected_at",
    "user_id",
    "username",
    "title",
    "hashtags",
    "audio_name",
    "ad",
    "uploaded_at",
    "days_since_upload",
    "view_count",
    "like_count",
    "comment_count",
    "repost_count",
    "follower_count",
]
XLSX_REELS_WEB_HIDDEN_FIELDS = {"location_name"}
XLSX_REELS_COLUMNS_FIELDS = [
    "collection_number",
    "collected_at",
    "url",
    "user_id",
    "username",
    "title",
    "hashtags",
    "audio_name",
    "location_name",
    "ad",
    "uploaded_at",
    "days_since_upload",
    "view_count",
    "like_count",
    "comment_count",
    "repost_count",
    "follower_count",
]
XLSX_REELS_ROW_FIELDS = [
    "collection_number",
    "days_since_previous",
    *XLSX_REELS_COLUMNS_FIELDS[1:],
]
XLSX_REELS_ROW_DROPPED_FIELDS = {
    "collection_label",
    "reaction_rate",
    "follower_count_collected_at",
    "follower_lookup_status",
}
class DataStore:
    """Small compatibility wrapper for the existing XLSX sync commands."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.workbook = data_dir / XLSX_FILENAME

    def sync_xlsx(self, destination: Path | None = None) -> None:
        workbook = destination or self.workbook
        csv_files = [
            path
            for path in sorted(self.data_dir.glob("*.csv"), key=lambda path: path.name.lower())
            if "_legacy_" not in path.stem
        ]
        sheets: list[tuple[str, list[list[str]]]] = []
        used_names: set[str] = set()
        for csv_path in csv_files:
            with csv_path.open("r", newline="", encoding="utf-8-sig") as file:
                rows = list(csv.reader(file))
            if rows:
                sheets.append((_xlsx_sheet_name(csv_path.stem, used_names), _xlsx_project_rows(csv_path.stem, rows)))
        if not sheets:
            return
        try:
            write_xlsx_workbook(workbook, sheets)
        except PermissionError as error:
            raise PermissionError(
                f"Cannot update '{workbook}' because it is open in Excel. "
                "Close the workbook and run the XLSX sync command again; CSV data is safe."
            ) from error


def _xlsx_sheet_name(stem: str, used_names: set[str]) -> str:
    base = re.sub(r"[\\[\\]:*?/\\\\]", "_", stem)[:31] or "Sheet"
    candidate = base
    suffix = 2
    while candidate.casefold() in used_names:
        ending = f"_{suffix}"
        candidate = f"{base[:31 - len(ending)]}{ending}"
        suffix += 1
    used_names.add(candidate.casefold())
    return candidate


def _xlsx_project_rows(stem: str, rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        return rows
    indexes = {header: index for index, header in enumerate(rows[0])}
    if stem.casefold() == "users":
        return _xlsx_project_user_rows(rows, indexes)
    elif stem.casefold() not in {"reels", "new_reels", "reels_web", "reels_columns", "reels_rows"}:
        return rows
    elif stem.casefold() in {"new_reels", "reels_rows"}:
        extra_fields = [
            field for field in rows[0]
            if field not in XLSX_REELS_ROW_FIELDS
            and _xlsx_base_field_name(field) not in XLSX_REELS_ROW_DROPPED_FIELDS
        ]
        fields = [*XLSX_REELS_ROW_FIELDS, *extra_fields]
    elif stem.casefold() in {"reels", "reels_columns"}:
        extra_fields = [
            field for field in rows[0]
            if field not in XLSX_REELS_COLUMNS_FIELDS
            and _xlsx_base_field_name(field) not in XLSX_REELS_ROW_DROPPED_FIELDS
        ]
        fields = _xlsx_add_elapsed_fields([*XLSX_REELS_COLUMNS_FIELDS, *extra_fields])
    else:
        extra_fields = [
            field
            for field in rows[0]
            if field not in XLSX_REELS_WEB_FIELDS
            and _xlsx_base_field_name(field) not in XLSX_REELS_WEB_HIDDEN_FIELDS
            and _xlsx_base_field_name(field) not in XLSX_REELS_ROW_DROPPED_FIELDS
        ]
        fields = _xlsx_add_elapsed_fields([*XLSX_REELS_WEB_FIELDS, *extra_fields])

    projected = [fields]
    for row in rows[1:]:
        values = {
            field: row[indexes[field]] if field in indexes and indexes[field] < len(row) else ""
            for field in fields
        }
        if stem.casefold() in {"reels", "new_reels", "reels_web", "reels_columns", "reels_rows"}:
            _format_reel_row(values, fields)
        projected.append([values[field] for field in fields])
    return projected


def _xlsx_project_user_rows(
    rows: list[list[str]], indexes: dict[str, int]
) -> list[list[str]]:
    """Flatten a user's follower-count columns into collection-history rows."""
    snapshot_indexes: dict[int, dict[str, int]] = {1: {}}
    for field, index in indexes.items():
        if field in {"follower_count", "collected_at"}:
            snapshot_indexes[1][field] = index
            continue
        match = re.fullmatch(
            r"(\d+)(?:st|nd|rd|th) collect_(follower_count|collected_at)",
            field,
        )
        if match:
            snapshot_indexes.setdefault(int(match.group(1)), {})[match.group(2)] = index

    def source_value(row: list[str], field: str) -> str:
        index = indexes.get(field)
        return row[index] if index is not None and index < len(row) else ""

    projected = [list(XLSX_USERS_FIELDS)]
    for row in rows[1:]:
        identity = [
            source_value(row, "user_id"),
            source_value(row, "username"),
            source_value(row, "biography"),
        ]
        previous_follower_count = ""
        for collection_number in sorted(snapshot_indexes):
            snapshot = snapshot_indexes[collection_number]
            follower_index = snapshot.get("follower_count")
            collected_at_index = snapshot.get("collected_at")
            follower_count = (
                row[follower_index]
                if follower_index is not None and follower_index < len(row)
                else ""
            )
            collected_at = (
                row[collected_at_index]
                if collected_at_index is not None and collected_at_index < len(row)
                else ""
            )
            if collection_number != 1 and not (follower_count or collected_at):
                continue
            follower_count_change = ""
            if (
                re.fullmatch(r"-?\d+", follower_count.strip())
                and re.fullmatch(r"-?\d+", previous_follower_count.strip())
            ):
                follower_count_change = str(
                    int(follower_count) - int(previous_follower_count)
                )
            projected.append(
                [
                    str(collection_number),
                    *identity,
                    follower_count,
                    follower_count_change,
                    collected_at,
                ]
            )
            previous_follower_count = follower_count
    return projected


def _format_reel_row(values: dict[str, str], fields: list[str]) -> None:
    title = values.get("title", "").strip()
    if (
        not values.get("username", "").strip()
        and not values.get("user_id", "").strip()
        and re.fullmatch(r"[A-Za-z0-9._]{1,30}", title)
    ):
        values["username"] = title
        values["title"] = ""
    previous_counts = {field: values.get(field, "").strip() for field in XLSX_DELTA_FIELDS}
    previous_collected_at = values.get("collected_at", "").strip()
    for field in fields:
        base_field = _xlsx_base_field_name(field)
        if base_field == "days_since_upload" and values.get(field, "").strip():
            values[field] = _xlsx_days_since_upload(values[field])
        elif field != "collected_at" and base_field == "collected_at" and values.get(field, "").strip():
            current = values[field].strip()
            values[_xlsx_elapsed_field_name(field)] = _xlsx_elapsed_days(
                previous_collected_at,
                current,
            )
            previous_collected_at = current
        elif field != base_field and base_field in XLSX_DELTA_FIELDS:
            current = values.get(field, "").strip()
            if current:
                values[field] = _xlsx_metric_with_delta(current, previous_counts[base_field])
                previous_counts[base_field] = current


def _xlsx_column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _xlsx_text(value: str) -> str:
    sanitized = XLSX_INVALID_XML_CHARACTER_PATTERN.sub("", value)
    escaped = xml_escape(sanitized, {'"': "&quot;"})
    preserve = ' xml:space="preserve"' if sanitized[:1].isspace() or sanitized[-1:].isspace() else ""
    return f"<is><t{preserve}>{escaped}</t></is>"


def _xlsx_base_field_name(field_name: str) -> str:
    match = re.match(r"^(?:\d+(?:st|nd|rd|th) collect|\+\d+(?:Minute|Hour|Day|Weeks)(?:_\d+)?)_(.+)$", field_name)
    return match.group(1) if match else field_name


def _xlsx_elapsed_field_name(collected_at_field: str) -> str:
    match = re.match(
        r"^(\d+(?:st|nd|rd|th) collect)_(?:follower_count_)?collected_at$",
        collected_at_field,
    )
    return f"{match.group(1)}_days_since_previous" if match else ""


def _xlsx_add_elapsed_fields(fields: list[str]) -> list[str]:
    expanded: list[str] = []
    for field in fields:
        expanded.append(field)
        elapsed_field = _xlsx_elapsed_field_name(field)
        if elapsed_field and elapsed_field not in expanded:
            expanded.append(elapsed_field)
    return expanded


def _xlsx_elapsed_days(previous_value: str, collected_value: str) -> str:
    try:
        previous = datetime.fromisoformat(previous_value.replace("Z", "+00:00"))
        collected = datetime.fromisoformat(collected_value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=timezone.utc)
    elapsed_days = max(0.0, (collected - previous).total_seconds() / 86_400)
    rounded_days = int((elapsed_days * 10) + 0.5) / 10
    if elapsed_days > 0 and rounded_days == 0:
        rounded_days = 0.1
    displayed = f"{rounded_days:.1f}".rstrip("0").rstrip(".")
    return f"+{displayed}day"


def _xlsx_metric_with_delta(current_value: str, previous_value: str) -> str:
    current = current_value.strip()
    previous = previous_value.strip()
    if not re.fullmatch(r"-?\d+", current):
        return current_value
    current_number = int(current)
    if not re.fullmatch(r"-?\d+", previous):
        return f"{current_number:,}"
    return f"{current_number:,}({current_number - int(previous):+,d})"


def _xlsx_days_since_upload(value: str) -> str:
    candidate = value.strip()
    if not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", candidate):
        return value
    elapsed_days = float(candidate)
    if elapsed_days < 1:
        decimal_days = candidate.rstrip("0").rstrip(".") if "." in candidate else candidate
        return f"{decimal_days}day"
    return f"{int(elapsed_days)}day"


def _xlsx_cell(reference: str, value: str, field_name: str, is_header: bool) -> str:
    base_field = _xlsx_base_field_name(field_name)
    if not is_header and base_field in XLSX_DATE_FIELDS:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone(timedelta(hours=9))).replace(tzinfo=None)
            serial = (parsed - datetime(1899, 12, 30)).total_seconds() / 86400
            return f'<c r="{reference}" s="2"><v>{serial:.10f}</v></c>'
        except ValueError:
            pass
    if not is_header and base_field in XLSX_PERCENT_FIELDS and re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", value.strip()):
        return f'<c r="{reference}" s="5"><v>{value.strip()}</v></c>'
    if not is_header and base_field == "follower_count_change" and re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", value.strip()):
        return f'<c r="{reference}" s="6"><v>{value.strip()}</v></c>'
    if not is_header and base_field in XLSX_NUMERIC_FIELDS and re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", value.strip()):
        return f'<c r="{reference}" s="4"><v>{value.strip()}</v></c>'
    style = ' s="1"' if is_header else (' s="3"' if base_field in XLSX_TEXT_IDENTIFIER_FIELDS else "")
    return f'<c r="{reference}"{style} t="inlineStr">{_xlsx_text(value)}</c>'


def _xlsx_worksheet_xml(rows: list[list[str]]) -> str:
    headers = rows[0]
    row_count = len(rows)
    column_count = max((len(row) for row in rows), default=1)
    widths = [
        min(max(max((len(row[index]) if index < len(row) else 0 for row in rows[:201]), default=8) + 2, 10), 42)
        for index in range(column_count)
    ]
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    rendered_rows = []
    for row_number, row in enumerate(rows, start=1):
        cells = [
            _xlsx_cell(
                f"{_xlsx_column_name(column + 1)}{row_number}",
                row[column] if column < len(row) else "",
                headers[column] if column < len(headers) else "",
                row_number == 1,
            )
            for column in range(column_count)
        ]
        rendered_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    last_column = _xlsx_column_name(column_count)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_column}{row_count}"/>'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
        '</sheetView></sheetViews>'
        f'<cols>{columns}</cols><sheetData>{"".join(rendered_rows)}</sheetData>'
        f'<autoFilter ref="A1:{last_column}1"/>'
        '</worksheet>'
    )


def write_xlsx_workbook(destination: Path, sheets: list[tuple[str, list[list[str]]]]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{destination.stem}.", suffix=".xlsx", dir=destination.parent, delete=False) as file:
            temporary = Path(file.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                + "".join(
                    f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    for index in range(1, len(sheets) + 1)
                )
                + "</Types>",
            )
            archive.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/workbook.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                + "".join(
                    f'<sheet name="{xml_escape(name, {chr(34): "&quot;"})}" sheetId="{index}" r:id="rId{index}"/>'
                    for index, (name, _) in enumerate(sheets, start=1)
                )
                + "</sheets></workbook>",
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(
                    f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
                    for index in range(1, len(sheets) + 1)
                )
                + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                + "</Relationships>",
            )
            archive.writestr(
                "xl/styles.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<numFmts count="4"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/><numFmt numFmtId="165" formatCode="@"/><numFmt numFmtId="168" formatCode="0.00%"/><numFmt numFmtId="169" formatCode="+#,##0;-#,##0;0"/></numFmts>'
                '<fonts count="2"><font><sz val="11"/><name val="Aptos"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos"/></font></fonts>'
                '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF0F766E"/><bgColor indexed="64"/></patternFill></fill></fills>'
                '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                '<cellXfs count="7"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="168" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="169" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
                '</styleSheet>',
            )
            for index, (_, rows) in enumerate(sheets, start=1):
                archive.writestr(f"xl/worksheets/sheet{index}.xml", _xlsx_worksheet_xml(rows))
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def read_reel_urls_from_xlsx(workbook: Path) -> list[str]:
    main_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    document_rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    package_rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    with zipfile.ZipFile(workbook) as archive:
        workbook_xml = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        preferred_sheet_names = {"reels": 0, "new_reels": 1, "reels_rows": 2, "reels_web": 3, "reels_columns": 4}
        sheet = min(
            (
                item
                for item in workbook_xml.findall(f".//{main_ns}sheet")
                if item.attrib.get("name", "").casefold() in preferred_sheet_names
            ),
            key=lambda item: preferred_sheet_names[item.attrib.get("name", "").casefold()],
            default=None,
        )
        if sheet is None:
            raise ValueError("The XLSX workbook does not contain a reels sheet.")
        relationship_id = sheet.attrib[f"{document_rel_ns}id"]
        relationships_xml = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target = next(item.attrib["Target"] for item in relationships_xml.findall(f"{package_rel_ns}Relationship") if item.attrib.get("Id") == relationship_id)
        sheet_path = target.lstrip("/")
        if not sheet_path.startswith("xl/"):
            sheet_path = f"xl/{sheet_path}"
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_xml = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = ["".join(node.text or "" for node in item.findall(f".//{main_ns}t")) for item in shared_xml.findall(f"{main_ns}si")]
        rows: list[list[str]] = []
        for row in ElementTree.fromstring(archive.read(sheet_path)).findall(f".//{main_ns}row"):
            values: dict[int, str] = {}
            for cell in row.findall(f"{main_ns}c"):
                letters = re.match(r"[A-Z]+", cell.attrib.get("r", "A1"))
                if letters is None:
                    continue
                column = 0
                for character in letters.group(0):
                    column = column * 26 + ord(character) - 64
                if cell.attrib.get("t", "") == "inlineStr":
                    value = "".join(node.text or "" for node in cell.findall(f".//{main_ns}t"))
                else:
                    value_node = cell.find(f"{main_ns}v")
                    value = value_node.text if value_node is not None and value_node.text else ""
                    if cell.attrib.get("t") == "s" and value:
                        value = shared_strings[int(value)]
                values[column - 1] = value
            if values:
                rows.append([values.get(index, "") for index in range(max(values) + 1)])
    if not rows:
        return []
    headers = [value.strip().casefold() for value in rows[0]]
    if "url" not in headers:
        raise ValueError("The reels_web sheet does not contain a url column.")
    url_index = headers.index("url")
    urls: list[str] = []
    seen: set[str] = set()
    for row in rows[1:]:
        value = row[url_index].strip() if url_index < len(row) else ""
        match = re.match(r"^https://(?:www\.)?instagram\.com/reels?/([A-Za-z0-9_-]+)", value, re.IGNORECASE)
        if not match:
            continue
        url = f"https://www.instagram.com/reels/{match.group(1)}/"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export Instagram web-collection CSV files to XLSX.")
    parser.add_argument("--data-dir", type=Path, default=BASE_DIR / "data_web")
    commands = parser.add_subparsers(dest="command", required=True)
    xlsx = commands.add_parser("xlsx", help="Synchronize CSV files into XLSX.")
    xlsx.add_argument("--output", type=Path)
    urls = commands.add_parser("xlsx-reel-urls", help="Export unique Reel URLs from the workbook.")
    urls.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    store = DataStore(args.data_dir.resolve())
    if args.command == "xlsx":
        workbook = args.output.resolve() if args.output else store.workbook
        store.sync_xlsx(workbook)
        print(f"XLSX saved: {workbook}")
    else:
        urls = read_reel_urls_from_xlsx(store.workbook)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("".join(f"{url}\n" for url in urls), encoding="utf-8")
        print(f"Reel URLs exported: {len(urls)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
