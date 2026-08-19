from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pypdf import PdfReader


NavKind = Literal["contents", "figures", "tables"]
FRONT_MATTER_CACHE_VERSION = 6
logging.getLogger("pypdf").setLevel(logging.ERROR)

PAGE_LABEL = r"(?:[A-Za-z]?\d+(?:[-–—.]\d+)*|[ivxlcdmIVXLCDM]+)"
PAGE_ONLY_RE = re.compile(rf"^{PAGE_LABEL}$")
TRAILING_PAGE_RE = re.compile(rf"^(?P<title>.+?\S)\s+(?P<page>{PAGE_LABEL})\s*$")
GLUED_PAGE_RE = re.compile(r"^(?P<title>.+?[^\d\s])(?P<page>\d{1,3})$")
SPACED_ROMAN_PAGE_RE = re.compile(
    r"(?<![A-Za-z])(?P<page>[ivxlcdm](?:\s+[ivxlcdm]){1,7})\s*$",
    re.IGNORECASE,
)
SPACED_DIGIT_PAGE_RE = re.compile(r"(?<!\d)(?P<page>\d(?:\s+\d){1,3})\s*$")
LEADER_RUN_RE = re.compile(r"(?:\s*[.．·•…⋯]\s*){2,}")
LAYOUT_GAP_RE = re.compile(r"[ \t]{3,}")
MERGED_TYPED_ENTRY_RE = re.compile(
    r"(?<=\d)(?=(?:Figure|Fig\.?|Table|图|表)\s*(?:\d+|[A-Z]))",
    re.IGNORECASE,
)
ENTRY_ID_RE = re.compile(
    r"^\s*(?:(?P<type>figure|fig\.?|table|图|表)\s*)?"
    r"(?P<identifier>(?:\d+|[IVXLCDM]+|[A-Z])(?:[.．\-–—]\d+)*)"
    r"(?:[.．、):：]|(?=\s))\s*",
    re.IGNORECASE,
)
GLUED_IDENTIFIER_PREFIX_RE = re.compile(
    r"^(?P<prefix>\s*(?:(?:figure|fig\.?|table|图|表)\s*)?"
    r"\d+(?:[.．\-–—]\d+)+)(?=[^\W\d_])",
    re.IGNORECASE,
)
CHAPTER_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9一二三四五六七八九十百零〇两]+\s*[章节篇部卷]|"
    r"附录\s*[A-Za-z0-9一二三四五六七八九十]+|"
    r"(?:chapter|section|part|appendix)\s+[A-Za-z0-9一二三四五六七八九十]+"
    r")\b",
    re.IGNORECASE,
)
CHINESE_NUMBER_PREFIX_RE = re.compile(r"^\s*[一二三四五六七八九十百]+[.．、)]\s*\S")

KNOWN_UNNUMBERED = {
    "abstract",
    "abstractresumeriassunto",
    "abstractrésumériassunto",
    "acknowledgement",
    "acknowledgements",
    "acknowledgment",
    "acknowledgments",
    "bibliography",
    "conclusion",
    "contents",
    "dedication",
    "foreword",
    "glossary",
    "index",
    "introduction",
    "listoffigures",
    "listofillustrations",
    "listofabbreviations",
    "listofpublications",
    "listofsymbols",
    "listoftables",
    "nomenclature",
    "organizationofthesis",
    "preface",
    "references",
    "resume",
    "summary",
    "摘要",
    "致谢",
    "参考文献",
    "附录",
}


@dataclass(frozen=True, slots=True)
class FrontMatterEntry:
    kind: NavKind
    title: str
    page: str = ""


@dataclass(frozen=True, slots=True)
class FrontMatterSection:
    kind: NavKind
    title: str
    entries: tuple[FrontMatterEntry, ...]


def strip_inline_markdown(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text.strip().strip("*_")


def normalized_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", strip_inline_markdown(text)).casefold()
    return "".join(character for character in value if unicodedata.category(character)[0] in {"L", "N"})


def navigation_kind(title: str) -> NavKind | None:
    value = unicodedata.normalize("NFKC", strip_inline_markdown(title)).casefold().strip()
    value = re.sub(
        r"\s*(?:\((?:continued|cont\.?|续)\)|[-–—:]\s*(?:continued|cont\.?|续))\s*$",
        "",
        value,
    )
    compact = re.sub(r"[^\w\u3400-\u9fff]+", "", value)
    if compact in {
        "contents",
        "tableofcontents",
        "sommaire",
        "inhalt",
        "indice",
        "目录",
    }:
        return "contents"
    if compact in {
        "listoffigures",
        "listoffiguresandtables",
        "listoffigurestables",
        "listofillustrations",
        "listofplates",
        "listoftablesandfigures",
        "listoftablesfigures",
        "图目录",
        "插图目录",
        "图表目录",
    }:
        return "figures"
    if compact in {"listoftables", "表目录"}:
        return "tables"
    return None


def _is_navigation_page_header(value: str, kind: NavKind) -> bool:
    """Recognize repeated navigation headings with a leading/trailing page label."""
    cleaned = unicodedata.normalize("NFKC", strip_inline_markdown(value)).strip()
    patterns = (
        rf"^(?:{PAGE_LABEL})\s+(?P<title>.+?)$",
        rf"^(?P<title>.+?)\s+(?:{PAGE_LABEL})$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, cleaned, flags=re.I)
        if match is not None and navigation_kind(match.group("title")) == kind:
            return True
    return False


def canonical_identifier(value: str) -> str:
    return unicodedata.normalize("NFKC", value).upper().replace("-", ".").replace("–", ".").replace("—", ".")


def _normalized_entry_prefix(title: str) -> str:
    cleaned = unicodedata.normalize("NFKC", strip_inline_markdown(title))
    return GLUED_IDENTIFIER_PREFIX_RE.sub(r"\g<prefix> ", cleaned, count=1)


def entry_identifier(title: str) -> str:
    match = ENTRY_ID_RE.match(_normalized_entry_prefix(title))
    return canonical_identifier(match.group("identifier")) if match else ""


def explicit_entry_kind(title: str) -> NavKind | None:
    match = ENTRY_ID_RE.match(_normalized_entry_prefix(title))
    explicit_type = (match.group("type") if match else "") or ""
    explicit_type = explicit_type.casefold().rstrip(".")
    if not explicit_type:
        return None
    return "tables" if explicit_type in {"table", "表"} else "figures"


def _known_unnumbered(title: str) -> bool:
    value = normalized_text(title)
    return value in KNOWN_UNNUMBERED or (
        len(value) <= 80 and (value.startswith("precis") or value.startswith("précis"))
    )


def looks_like_entry_title(title: str, kind: NavKind) -> bool:
    cleaned = strip_inline_markdown(title).strip(" .·•…⋯")
    if not cleaned or len(cleaned) > 2000:
        return False
    if kind in {"figures", "tables"}:
        match = ENTRY_ID_RE.match(_normalized_entry_prefix(cleaned))
        if match is None:
            return False
        identifier = match.group("identifier")
        return bool(match.group("type") or re.search(r"[.．\-–—]", identifier))
    return bool(
        entry_identifier(cleaned)
        or CHAPTER_PREFIX_RE.match(cleaned)
        or CHINESE_NUMBER_PREFIX_RE.match(cleaned)
        or navigation_kind(cleaned) is not None
        or _known_unnumbered(cleaned)
    )


def _clean_leaders(value: str) -> tuple[str, bool]:
    had_leaders = bool(LEADER_RUN_RE.search(value))
    value = LEADER_RUN_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value, had_leaders


def split_title_page(value: str, kind: NavKind) -> tuple[str, str] | None:
    cleaned, had_leaders = _clean_leaders(strip_inline_markdown(value))
    if not cleaned:
        return None
    match = TRAILING_PAGE_RE.match(cleaned)
    if match:
        title = match.group("title").strip(" .·•…⋯")
        technical_standard = bool(
            re.search(r"\b(?:ISO|IEC|IEEE|ASTM|DIN|EN)\s*$", title, flags=re.I)
            and match.group("page").isdigit()
        )
        if not technical_standard and (had_leaders or looks_like_entry_title(title, kind)):
            return title, match.group("page")
    glued = GLUED_PAGE_RE.match(cleaned)
    if glued:
        title = glued.group("title").strip(" .·•…⋯")
        if title[-1:].islower() and looks_like_entry_title(title, kind):
            return title, glued.group("page")
    if looks_like_entry_title(cleaned, kind):
        return cleaned.strip(" .·•…⋯"), ""
    return None


def _starts_entry(value: str, kind: NavKind) -> bool:
    cleaned = strip_inline_markdown(value)
    parsed = split_title_page(cleaned, kind)
    if parsed is not None and parsed[1]:
        return looks_like_entry_title(parsed[0], kind)
    if kind in {"figures", "tables"}:
        return looks_like_entry_title(cleaned, kind)
    return bool(
        entry_identifier(cleaned)
        or CHAPTER_PREFIX_RE.match(cleaned)
        or CHINESE_NUMBER_PREFIX_RE.match(cleaned)
        or _known_unnumbered(cleaned)
    )


def _join_wrapped(left: str, right: str) -> str:
    left = left.rstrip()
    right = right.lstrip()
    if left.endswith("-") and right[:1].islower():
        return left[:-1] + right
    return f"{left} {right}".strip()


def _normalize_layout_line(value: str) -> str:
    line = unicodedata.normalize("NFKC", value).strip()
    line = re.sub(r"(?<=\d)\s*[.．]\s*(?=\d)", ".", line)
    for pattern in (SPACED_ROMAN_PAGE_RE, SPACED_DIGIT_PAGE_RE):
        match = pattern.search(line)
        if match:
            page = re.sub(r"\s+", "", match.group("page"))
            line = f"{line[: match.start()].rstrip()} {page}".strip()
            break
    return line


def _split_layout_columns(value: str, kind: NavKind) -> list[str]:
    """Split layout-text columns only at a completed-entry boundary."""
    if not value.strip():
        return [""]
    parts: list[str] = []
    start = 0
    for gap in LAYOUT_GAP_RE.finditer(value):
        left = value[start : gap.start()].strip()
        right = value[gap.end() :].strip()
        normalized_left = _normalize_layout_line(left)
        normalized_right = _normalize_layout_line(right)
        parsed = split_title_page(normalized_left, kind) if normalized_left else None
        if parsed is None or not parsed[1] or not _starts_entry(normalized_right, kind):
            continue
        parts.append(left)
        start = gap.end()
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts or [""]


def _layout_ordered_lines(lines: list[str], kind: NavKind) -> list[str]:
    rows = [_split_layout_columns(line, kind) for line in lines if len(line) <= 4096]
    ordered: list[str] = []
    index = 0
    while index < len(rows):
        width = len(rows[index])
        if width < 2:
            ordered.extend(rows[index])
            index += 1
            continue
        end = index + 1
        while end < len(rows) and len(rows[end]) == width:
            end += 1
        if end - index < 2:
            ordered.extend(rows[index])
            index += 1
            continue
        for column in range(width):
            ordered.extend(rows[row][column] for row in range(index, end))
        index = end
    return ordered


def parse_entry_lines(lines: list[str], kind: NavKind) -> list[FrontMatterEntry]:
    entries: list[FrontMatterEntry] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        parsed = split_title_page(buffer, kind) if buffer else None
        if parsed:
            title, page = parsed
            entries.append(
                FrontMatterEntry(
                    kind=explicit_entry_kind(title) or kind,
                    title=title,
                    page=page,
                )
            )
        buffer = ""

    expanded_lines = [
        part
        for line in _layout_ordered_lines(lines, kind)
        for part in MERGED_TYPED_ENTRY_RE.split(line)
    ]
    for line_index, raw_line in enumerate(expanded_lines):
        line = _normalize_layout_line(raw_line)
        if not line:
            flush()
            continue
        if _is_navigation_page_header(line, kind):
            flush()
            continue
        line_navigation_kind = navigation_kind(line)
        if line_navigation_kind is not None and line_navigation_kind == kind:
            continue
        if PAGE_ONLY_RE.fullmatch(line):
            if buffer:
                joined = f"{buffer} {line}"
                parsed = split_title_page(joined, kind)
                if parsed is not None and parsed[1]:
                    buffer = joined
                    flush()
            continue
        if buffer and _starts_entry(line, kind):
            flush()
        buffer = _join_wrapped(buffer, line) if buffer else line
        parsed = split_title_page(buffer, kind)
        if parsed and parsed[1]:
            next_line = expanded_lines[line_index + 1].strip() if line_index + 1 < len(expanded_lines) else ""
            if (
                next_line
                and navigation_kind(next_line) is None
                and not PAGE_ONLY_RE.fullmatch(next_line)
                and not _starts_entry(next_line, kind)
            ):
                continue
            flush()
    flush()

    deduplicated: list[FrontMatterEntry] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.kind, normalized_text(entry.title))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        deduplicated.append(entry)
    return deduplicated


def _page_segments(lines: list[str]) -> list[tuple[NavKind, str, list[str]]]:
    markers = [
        (index, kind, line.strip())
        for index, line in enumerate(lines)
        if len(line) <= 4096
        if (kind := navigation_kind(line)) is not None
    ]
    segments: list[tuple[NavKind, str, list[str]]] = []
    for position, (index, kind, title) in enumerate(markers):
        end = markers[position + 1][0] if position + 1 < len(markers) else len(lines)
        segments.append((kind, title, lines[index + 1 : end]))
    return segments


def _read_cache(
    source: Path, cache_path: Path | None, max_pages: int
) -> dict[NavKind, FrontMatterSection] | None:
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        stat = source.stat()
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != FRONT_MATTER_CACHE_VERSION:
            return None
        signature = payload.get("source", {})
        if not isinstance(signature, dict):
            return None
        if (
            signature.get("size") != stat.st_size
            or signature.get("mtime_ns") != stat.st_mtime_ns
            or payload.get("max_pages") != max_pages
        ):
            return None
        result: dict[NavKind, FrontMatterSection] = {}
        raw_sections = payload.get("sections", [])
        if not isinstance(raw_sections, list):
            return None
        for item in raw_sections:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind not in {"contents", "figures", "tables"}:
                continue
            raw_entries = item.get("entries", [])
            if not isinstance(raw_entries, list):
                continue
            entries = tuple(
                FrontMatterEntry(
                    kind=entry["kind"],
                    title=entry["title"],
                    page=entry.get("page", ""),
                )
                for entry in raw_entries
                if isinstance(entry, dict)
                and entry.get("kind") in {"contents", "figures", "tables"}
                and isinstance(entry.get("title"), str)
            )
            if entries:
                result[kind] = FrontMatterSection(
                    kind=kind,
                    title=str(item.get("title") or ""),
                    entries=entries,
                )
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write_cache(
    source: Path,
    cache_path: Path | None,
    max_pages: int,
    result: dict[NavKind, FrontMatterSection],
) -> None:
    if cache_path is None:
        return
    try:
        stat = source.stat()
        payload = {
            "version": FRONT_MATTER_CACHE_VERSION,
            "max_pages": max_pages,
            "source": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
            "sections": [
                {
                    "kind": section.kind,
                    "title": section.title,
                    "entries": [
                        {"kind": entry.kind, "title": entry.title, "page": entry.page}
                        for entry in section.entries
                    ],
                }
                for section in result.values()
            ],
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_name(f"{cache_path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(cache_path)
    except OSError:
        return


def extract_front_matter(
    source: Path,
    max_pages: int = 64,
    cache_path: Path | None = None,
    force: bool = False,
) -> dict[NavKind, FrontMatterSection]:
    """Read only likely front-matter pages and recover clean navigation entries."""
    cached = None if force else _read_cache(source, cache_path, max_pages)
    if cached is not None:
        return cached
    try:
        reader = PdfReader(str(source))
    except Exception:
        return {}

    collected: dict[NavKind, list[FrontMatterEntry]] = {}
    titles: dict[NavKind, str] = {}
    active: NavKind | None = None
    found_any = False
    last_section_page = -1

    for page_index in range(min(len(reader.pages), max_pages)):
        try:
            page_text = reader.pages[page_index].extract_text(extraction_mode="layout") or ""
        except Exception:
            try:
                page_text = reader.pages[page_index].extract_text() or ""
            except Exception:
                page_text = ""
        lines = page_text.splitlines()
        segments = _page_segments(lines)
        page_entries = 0
        if segments:
            found_any = True
            for kind, title, segment_lines in segments:
                active = kind
                titles.setdefault(kind, re.sub(r"\s*\(.*?continued.*?\)\s*$", "", title, flags=re.I).strip())
                parsed = parse_entry_lines(segment_lines, kind)
                collected.setdefault(kind, []).extend(parsed)
                page_entries += len(parsed)
                last_section_page = page_index
        elif active is not None:
            parsed = parse_entry_lines(lines, active)
            page_bearing = sum(bool(entry.page) for entry in parsed)
            if len(parsed) >= 3 and page_bearing * 5 >= len(parsed) * 3:
                collected.setdefault(active, []).extend(parsed)
                page_entries += len(parsed)
                last_section_page = page_index
            else:
                active = None

        if found_any and active is None and page_index > last_section_page + 2:
            break
        if found_any and page_entries == 0 and page_index >= last_section_page + 3:
            break

    result: dict[NavKind, FrontMatterSection] = {}
    for kind, entries in collected.items():
        deduplicated: list[FrontMatterEntry] = []
        seen: set[str] = set()
        for entry in entries:
            key = normalized_text(entry.title)
            if not key or key in seen:
                continue
            seen.add(key)
            deduplicated.append(entry)
        if deduplicated:
            default_title = {
                "contents": "Contents",
                "figures": "List of Figures",
                "tables": "List of Tables",
            }[kind]
            result[kind] = FrontMatterSection(
                kind=kind,
                title=titles.get(kind, default_title),
                entries=tuple(deduplicated),
            )
    _write_cache(source, cache_path, max_pages, result)
    return result
