"""Read user-selected collector exports in memory, without touching collector state."""
from __future__ import annotations

import csv
import io
import json
import math
import posixpath
import re
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    from ...src.reels.reel_records import parse_snapshot_field
    from ...src.reels.korean_classification import normalize_account_country
    from .classification import korean_value
except ImportError:
    from src.reels.reel_records import parse_snapshot_field
    from src.reels.korean_classification import normalize_account_country
    from classification import korean_value

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 256
MAX_XML_BYTES = 64 * 1024 * 1024
KST = timezone(timedelta(hours=9))
METRICS = ("view_count", "like_count", "comment_count", "share_count", "repost_count", "saved_count", "follower_count")
FIELDS = ("url", "username", "title", "korean", "korean_source", "account_country", "collected_at", "collection_number", "video_duration_seconds", *METRICS)
ALIASES = {
    "릴스 주소": "url", "릴스주소": "url", "reel_url": "url", "작성자": "username",
    "내용": "title", "수집 시각": "collected_at", "수집시각": "collected_at",
    "수집 차수": "collection_number", "수집 회차": "collection_number", "회차": "collection_number",
    "영상 길이": "video_duration_seconds", "영상길이": "video_duration_seconds", "영상 길이(초)": "video_duration_seconds",
    "조회수": "view_count", "좋아요": "like_count", "댓글": "comment_count",
    "공유": "share_count", "재게시": "repost_count", "저장": "saved_count", "팔로워": "follower_count",
    "한국 여부": "korean", "한국여부": "korean",
}


def metric_number(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    # Exported XLSX cells may include a parenthesized delta after the actual count.
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\([+-]\d+(?:\.\d+)?\))?", text)
    if not match:
        return None
    number = float(match[1])
    return number if math.isfinite(number) and number <= 9_007_199_254_740_991 else None


def collected_time(value, excel=False):
    text = str(value or "").strip()
    try:
        if excel and re.fullmatch(r"\d+(?:\.\d+)?", text):
            parsed = datetime(1899, 12, 30, tzinfo=KST) + timedelta(days=float(text))
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=KST)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return ""


def normalized_rows(raw_rows, *, excel=False):
    result = []
    recognized = False
    for source in raw_rows:
        if not isinstance(source, dict) or len(source) > MAX_COLUMNS:
            raise ValueError("행 형식 또는 컬럼 수를 확인하세요. 최대 256개 컬럼을 지원합니다.")
        source = {ALIASES.get(str(key).strip().casefold(), str(key).strip()): value for key, value in source.items()}
        recognized |= any(key in source for key in FIELDS)
        initial = {key: source.get(key, "") for key in FIELDS}
        candidates = [initial]
        snapshots = {}
        for key, value in source.items():
            parsed = parse_snapshot_field(key)
            if parsed:
                snapshots.setdefault(parsed["label"], {})[parsed["baseField"]] = value
        if snapshots and not initial.get("collected_at"):
            candidates = []  # Some wide files store every snapshot in prefixed columns.
        elif snapshots:
            # Collector wide exports keep the first snapshot in base columns;
            # top-level collection_number is the total completed round count.
            initial["collection_number"] = 1
        for label, values in snapshots.items():
            if values.get("collected_at"):
                candidate = {**initial, **{key: values.get(key, initial.get(key, "") if key in {"korean", "korean_source", "account_country", "video_duration_seconds"} else "") for key in FIELDS if key not in {"url", "username", "title"}}}
                match = re.match(r"(\d+)(?:st|nd|rd|th) collect", label)
                candidate["collection_number"] = match[1] if match else ""
                candidates.append(candidate)
        for row in candidates:
            if not any(row.get(key) not in (None, "") for key in FIELDS):
                continue
            output = {key: str(row.get(key) or "")[:300] for key in ("url", "username", "title")}
            # Normalize /reel/ and /reels/ identities for latest-per-Reel scope.
            match = re.fullmatch(r"https?://(?:www\.)?instagram\.com/reels?/([\w-]+)/?(?:[?#].*)?", output["url"], re.I)
            if match:
                output["url"] = f"https://www.instagram.com/reels/{match[1]}/"
            output["collected_at"] = collected_time(row.get("collected_at"), excel)
            output["korean"] = korean_value(row)
            output["korean_source"] = str(row.get("korean_source") or "")[:80]
            output["account_country"] = normalize_account_country(row.get("account_country"))[:100]
            number = metric_number(row.get("collection_number"))
            output["collection_number"] = int(number) if number is not None and number.is_integer() and number > 0 else None
            output.update({key: metric_number(row.get(key)) for key in METRICS})
            output["video_duration_seconds"] = metric_number(row.get("video_duration_seconds"))
            result.append(output)
            if len(result) > MAX_ROWS:
                raise ValueError("수집 기록은 최대 100,000건까지 읽을 수 있습니다. 파일을 나눠 선택하세요.")
    if not recognized or not result:
        raise ValueError("수집 데이터 컬럼을 찾지 못했습니다. url, collected_at 또는 조회수·좋아요 컬럼이 있는 파일을 선택하세요.")
    return result


def matrix_records(matrix):
    iterator = iter(matrix)
    headers = next(iterator, [])
    if not headers or len(headers) > MAX_COLUMNS:
        raise ValueError("첫 행에 컬럼 이름이 있어야 합니다. 최대 256개 컬럼을 지원합니다.")
    headers = [str(value).strip().lstrip("\ufeff") for value in headers]
    named = [value for value in headers if value]
    if len(set(named)) != len(named):
        raise ValueError("중복된 컬럼 이름이 있습니다. 첫 행의 컬럼 이름을 확인하세요.")
    for index, values in enumerate(iterator):
        if index >= MAX_ROWS:
            raise ValueError("최대 100,000행까지 지원합니다. 파일을 나눠 선택하세요.")
        if len(values) > MAX_COLUMNS:
            raise ValueError("최대 256개 컬럼까지 지원합니다.")
        if any(value not in (None, "") for value in values[len(headers):]):
            raise ValueError("헤더보다 값이 많은 행이 있습니다. 구분자와 캡션의 따옴표를 확인하세요.")
        if any(value not in (None, "") for value in values):
            yield {key: values[i] if i < len(values) else "" for i, key in enumerate(headers) if key}


def xlsx_sheets(content):
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(item.file_size for item in entries) > MAX_XML_BYTES:
            raise ValueError("압축 해제 크기가 64 MiB를 넘거나 구성 파일이 너무 많은 XLSX입니다. CSV로 저장해 선택하세요.")

        def xml(name):
            data = archive.read(name)
            declarations = data.replace(b"\x00", b"").upper()
            if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
                raise ValueError("지원하지 않는 XML 선언이 포함되어 있습니다.")
            return ET.fromstring(data)

        workbook = xml("xl/workbook.xml")
        relationships = {node.get("Id"): node.get("Target", "") for node in xml("xl/_rels/workbook.xml.rels")
                         if node.get("TargetMode") != "External"}
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(node.text or "" for node in item.iter(f"{ns}t"))
                      for item in xml("xl/sharedStrings.xml").findall(f"{ns}si")]
        sheets = workbook.findall(f"{ns}sheets/{ns}sheet")
        if len(sheets) > 20:
            raise ValueError("최대 20개 시트까지 지원합니다. 필요한 시트만 별도 파일로 저장하세요.")
        for sheet in sheets:
            target = relationships.get(sheet.get(rel), "")
            if not target:
                continue
            target = posixpath.normpath(target.lstrip("/") if target.startswith("/") else f"xl/{target}")
            if not target.startswith("xl/"):
                raise ValueError("워크시트 경로가 올바르지 않습니다.")
            matrix = []
            for source_row in xml(target).findall(f"{ns}sheetData/{ns}row"):
                cells = {}
                for cell in source_row.findall(f"{ns}c"):
                    letters = re.match(r"[A-Z]+", cell.get("r", ""))
                    if not letters:
                        raise ValueError("XLSX 셀 주소를 읽을 수 없습니다.")
                    column = 0
                    for char in letters[0]:
                        column = column * 26 + ord(char) - 64
                    if column > MAX_COLUMNS:
                        raise ValueError("최대 256개 컬럼까지 지원합니다.")
                    value = cell.findtext(f"{ns}v", "")
                    if cell.get("t") == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter(f"{ns}t"))
                    elif cell.get("t") == "s":
                        value = shared[int(value)]
                    cells[column - 1] = value
                if cells:
                    matrix.append([cells.get(i, "") for i in range(max(cells) + 1)])
                if len(matrix) > MAX_ROWS + 1:
                    raise ValueError("최대 100,000행까지 지원합니다. 파일을 나눠 선택하세요.")
            yield sheet.get("name", "시트"), matrix


def read_visualization_file(content: bytes, filename: str):
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("비어 있지 않은 20 MiB 이하 파일을 선택하세요.")
    suffix = Path(filename).suffix.lower()
    datasets, warnings = [], []
    try:
        if suffix == ".xlsx":
            for name, matrix in xlsx_sheets(content):
                try:
                    rows = normalized_rows(matrix_records(matrix), excel=True)
                except ValueError as error:
                    warnings.append(f"{name}: {error}")
                else:
                    if sum(len(item["rows"]) for item in datasets) + len(rows) > MAX_ROWS:
                        raise ValueError("전체 시트의 기록이 100,000건을 넘습니다. 필요한 시트를 별도 파일로 저장하세요.")
                    datasets.append({"name": name, "rows": rows})
        elif suffix in {".csv", ".json"}:
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = content.decode("cp949")
            if suffix == ".csv":
                if text.startswith("sep="):
                    text = text.partition("\n")[2]
                try:
                    dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
                except csv.Error:
                    dialect = csv.excel
                # The sample may contain no escaped quotes; collector CSVs still
                # use doubled quotes in later captions. Do not shift those cells.
                raw = matrix_records(csv.reader(io.StringIO(text, newline=""), dialect, doublequote=True, strict=True))
            else:
                raw = json.loads(text)
                if isinstance(raw, dict):
                    raw = next((raw[key] for key in ("items", "records", "rows", "reels", "data") if isinstance(raw.get(key), list)), None)
                if not isinstance(raw, list):
                    raise ValueError("JSON은 수집 기록의 배열 또는 items/records/rows/reels/data 배열이어야 합니다.")
                if len(raw) > MAX_ROWS:
                    raise ValueError("최대 100,000건까지 지원합니다.")
            datasets.append({"name": "수집 데이터", "rows": normalized_rows(raw)})
        else:
            raise ValueError("CSV, JSON, XLSX 형식만 지원합니다. XLS 파일은 XLSX로 저장하세요.")
    except (zipfile.BadZipFile, ET.ParseError, KeyError, IndexError, UnicodeError, csv.Error, RecursionError, OverflowError, RuntimeError, zlib.error) as error:
        raise ValueError("파일을 읽을 수 없습니다. 손상 여부를 확인하고 CSV 또는 XLSX로 다시 저장하세요.") from error
    if not datasets:
        raise ValueError("수집 기록이 있는 시트를 찾지 못했습니다. " + " / ".join(warnings[:3]))
    if sum(len(item["rows"]) for item in datasets) > MAX_ROWS:
        raise ValueError("전체 시트의 기록이 100,000건을 넘습니다. 필요한 시트를 별도 파일로 저장하세요.")
    return {"datasets": datasets, "warnings": warnings}
