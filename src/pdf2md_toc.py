from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass


HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
TOC_HEADING_RE = re.compile(
    r"^(?:目\s*录|contents?|table\s+of\s+contents|"
    r"图\s*目录|表\s*目录|list\s+of\s+(?:figures|tables)|"
    r"sommaire|inhalt|indice)$",
    re.IGNORECASE,
)
PAGE_LABEL = r"(?:[A-Za-z]?\d+(?:[-–—.]\d+)*|[ivxlcdmIVXLCDM]+)"
LEADER_RE = re.compile(
    rf"^(?P<title>.+?)\s*(?:[.．·•…⋯]{{2,}}|(?:\.\s*){{3,}})\s*"
    rf"(?P<page>{PAGE_LABEL})\s*$"
)
SPACED_PAGE_RE = re.compile(
    rf"^(?P<title>.+?)\s{{2,}}(?P<page>{PAGE_LABEL})\s*$"
)
PAGE_ONLY_RE = re.compile(
    rf"^(?:[.．·•…⋯\s]{{2,}})?(?P<page>{PAGE_LABEL})\s*$"
)
RENDERED_ENTRY_RE = re.compile(
    rf"^\s*[-*+]\s+(?:\[(?P<link_title>.+?)\]\(#[^)]+\)|(?P<title>.+?))"
    rf"\s+[—-]\s+(?P<page>{PAGE_LABEL})\s*$"
)
NUMBERED_PREFIX_RE = re.compile(
    r"^\s*(?P<prefix>"
    r"第\s*[0-9一二三四五六七八九十百零〇两]+\s*[章节篇部卷]|"
    r"(?:chapter|section|part|appendix)\s+[A-Za-z0-9一二三四五六七八九十]+|"
    r"\d+(?:\.\d+)*(?:[.．、)]|(?=\s))|"
    r"[一二三四五六七八九十百]+[.．、)]"
    r")\s*",
    re.IGNORECASE,
)
MERGED_ENTRY_RE = re.compile(
    rf"(?:[.．·•…⋯]{{2,}}|(?:\.\s*){{3,}})\s*{PAGE_LABEL}\s*"
    r"(?=(?:图|表)\s*\d+\s*[-–—.]\s*\d+|(?:figure|table)\s+\d+|"
    r"第\s*[0-9一二三四五六七八九十百零〇两]+\s*[章节篇部卷]|"
    r"附录\s*[A-Za-z0-9一二三四五六七八九十]+|"
    r"(?:chapter|section|part|appendix)\s+[A-Za-z0-9一二三四五六七八九十]+|"
    r"\d+(?:\.\d+)+(?:\s|[^\d])|\d+[.．]\s+)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class TocEntry:
    line_start: int
    line_end: int
    title: str
    page: str
    depth: int
    target: "Heading | None" = None


@dataclass(slots=True)
class Heading:
    line_index: int
    title: str
    level: int
    promoted: bool = False
    anchor: str = ""


def _strip_inline_markdown(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text.strip().strip("*_")


def _normalized_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", _strip_inline_markdown(text)).casefold()
    return "".join(
        character
        for character in text
        if unicodedata.category(character)[0] in {"L", "N"}
    )


def _prefix_and_body(text: str) -> tuple[str, str]:
    cleaned = unicodedata.normalize("NFKC", _strip_inline_markdown(text))
    match = NUMBERED_PREFIX_RE.match(cleaned)
    if match is None:
        return "", _normalized_title(cleaned)
    return _normalized_title(match.group("prefix")), _normalized_title(cleaned[match.end() :])


def _entry_depth(title: str) -> int:
    cleaned = unicodedata.normalize("NFKC", _strip_inline_markdown(title))
    match = re.match(r"^\s*(\d+(?:\.\d+)*)", cleaned)
    if match:
        return min(5, max(0, match.group(1).count(".")))
    return 0


def _heading_level(title: str, fallback: int) -> int:
    prefix, _body = _prefix_and_body(title)
    cleaned = unicodedata.normalize("NFKC", _strip_inline_markdown(title))
    match = re.match(r"^\s*(\d+(?:\.\d+)*)", cleaned)
    if match:
        return min(6, 2 + match.group(1).count("."))
    if prefix:
        return 2
    return fallback


def _clean_entry_source(line: str) -> str:
    line = line.strip()
    heading = HEADING_RE.match(line)
    if heading:
        line = heading.group(2)
    line = re.sub(r"^\s*[-*+]\s+", "", line)
    return _strip_inline_markdown(line)


def _parse_toc_entry(line: str) -> tuple[str, str] | None:
    rendered = RENDERED_ENTRY_RE.match(line)
    if rendered:
        title = rendered.group("link_title") or rendered.group("title") or ""
        return _strip_inline_markdown(title), rendered.group("page")

    cleaned = _clean_entry_source(line)
    if not cleaned or cleaned.startswith(("|", "![", "```")):
        return None
    for pattern in (LEADER_RE, SPACED_PAGE_RE):
        match = pattern.match(cleaned)
        if match and _normalized_title(match.group("title")):
            return match.group("title").strip(), match.group("page")
    return None


def _split_merged_toc_entries(line: str) -> list[str]:
    """Split OCR lines where one entry's page number touches the next title."""
    boundaries = [match.end() for match in MERGED_ENTRY_RE.finditer(line)]
    if not boundaries:
        return [line]
    parts: list[str] = []
    start = 0
    for end in boundaries:
        parts.append(line[start:end].strip())
        start = end
    if line[start:].strip():
        parts.append(line[start:].strip())
    return parts


def _is_toc_heading(title: str) -> bool:
    return bool(TOC_HEADING_RE.fullmatch(_strip_inline_markdown(title)))


def _toc_ranges(lines: list[str]) -> list[tuple[int, int]]:
    starts = [
        index
        for index, line in enumerate(lines)
        if (match := HEADING_RE.match(line)) and _is_toc_heading(match.group(2))
    ]
    ranges: list[tuple[int, int]] = []
    for position, start in enumerate(starts):
        limit = starts[position + 1] if position + 1 < len(starts) else len(lines)
        end = limit
        seen_entries = 0
        for index in range(start + 1, limit):
            match = HEADING_RE.match(lines[index])
            if match:
                if _parse_toc_entry(match.group(2)) is not None:
                    seen_entries += 1
                    continue
                end = index
                break
            if _parse_toc_entry(lines[index]) is not None:
                seen_entries += 1
                continue
            cleaned = _clean_entry_source(lines[index])
            if not cleaned or seen_entries < 2:
                continue
            before_blank = index > start + 1 and not lines[index - 1].strip()
            after_blank = index + 1 >= limit or not lines[index + 1].strip()
            next_is_page = (
                index + 1 < limit
                and PAGE_ONLY_RE.fullmatch(_clean_entry_source(lines[index + 1]))
            )
            if before_blank and after_blank and not next_is_page:
                end = index
                break
        ranges.append((start, end))
    return ranges


def _line_in_ranges(index: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in ranges)


def _looks_numbered_title(title: str) -> bool:
    return NUMBERED_PREFIX_RE.match(
        unicodedata.normalize("NFKC", _strip_inline_markdown(title))
    ) is not None


def _toc_entries(lines: list[str], ranges: list[tuple[int, int]]) -> list[TocEntry]:
    entries: list[TocEntry] = []
    for start, end in ranges:
        index = start + 1
        while index < end:
            merged_parts = _split_merged_toc_entries(lines[index])
            parsed_parts = [_parse_toc_entry(part) for part in merged_parts]
            if parsed_parts and all(parsed is not None for parsed in parsed_parts):
                for parsed in parsed_parts:
                    assert parsed is not None
                    title, page = parsed
                    entries.append(TocEntry(index, index, title, page, _entry_depth(title)))
                index += 1
                continue

            current = _clean_entry_source(lines[index])
            if current and index + 1 < end:
                next_cleaned = _clean_entry_source(lines[index + 1])
                page_only = PAGE_ONLY_RE.fullmatch(next_cleaned)
                next_entry = _parse_toc_entry(lines[index + 1])
                if page_only and _looks_numbered_title(current):
                    entries.append(
                        TocEntry(
                            index,
                            index + 1,
                            current,
                            page_only.group("page"),
                            _entry_depth(current),
                        )
                    )
                    index += 2
                    continue
                if (
                    next_entry is not None
                    and _looks_numbered_title(current)
                    and not _looks_numbered_title(next_entry[0])
                ):
                    title = f"{current} {next_entry[0]}"
                    entries.append(
                        TocEntry(
                            index,
                            index + 1,
                            title,
                            next_entry[1],
                            _entry_depth(title),
                        )
                    )
                    index += 2
                    continue
            index += 1
    return entries


def _standalone_line(lines: list[str], index: int) -> bool:
    if not lines[index].strip() or len(lines[index].strip()) > 200:
        return False
    if lines[index].lstrip().startswith(("#", "- ", "* ", "+ ", "|", "![", "```")):
        return False
    before_blank = index == 0 or not lines[index - 1].strip()
    after_blank = index + 1 == len(lines) or not lines[index + 1].strip()
    return before_blank and after_blank


def _collect_headings(
    lines: list[str], ranges: list[tuple[int, int]], entries: list[TocEntry]
) -> list[Heading]:
    headings: list[Heading] = []
    for index, line in enumerate(lines):
        if _line_in_ranges(index, ranges):
            continue
        match = HEADING_RE.match(line)
        if match and not match.group(2).startswith("PDF pages "):
            headings.append(Heading(index, match.group(2).strip(), len(match.group(1))))

    entry_forms: dict[str, int] = {}
    for entry in entries:
        form = _normalized_title(entry.title)
        entry_forms[form] = entry_forms.get(form, 0) + 1
    existing_lines = {heading.line_index for heading in headings}
    for index, line in enumerate(lines):
        if index in existing_lines or _line_in_ranges(index, ranges) or not _standalone_line(lines, index):
            continue
        title = _strip_inline_markdown(line)
        form = _normalized_title(title)
        if form and entry_forms.get(form) == 1:
            headings.append(Heading(index, title, _heading_level(title, 2), promoted=True))
    return sorted(headings, key=lambda heading: heading.line_index)


def _match_entry(entry: TocEntry, headings: list[Heading]) -> Heading | None:
    entry_full = _normalized_title(entry.title)
    entry_prefix, entry_body = _prefix_and_body(entry.title)

    exact_full = [heading for heading in headings if _normalized_title(heading.title) == entry_full]
    if len(exact_full) == 1:
        return exact_full[0]

    exact_body = [
        heading
        for heading in headings
        if entry_body and _prefix_and_body(heading.title)[1] == entry_body
    ]
    if len(exact_body) == 1:
        return exact_body[0]

    scored: list[tuple[float, Heading]] = []
    for heading in headings:
        heading_prefix, heading_body = _prefix_and_body(heading.title)
        if not entry_body or not heading_body:
            continue
        if entry_prefix and heading_prefix and entry_prefix != heading_prefix:
            continue
        score = difflib.SequenceMatcher(None, entry_body, heading_body, autojunk=False).ratio()
        threshold = 0.82 if entry_prefix and entry_prefix == heading_prefix else 0.93
        if score >= threshold:
            scored.append((score, heading))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None
    return scored[0][1]


def _assign_anchors(headings: list[Heading]) -> None:
    """Assign compact anchors only to headings referenced by the source TOC."""
    for index, heading in enumerate(sorted(headings, key=lambda item: item.line_index), start=1):
        heading.anchor = str(index)


def _escape_link_text(title: str) -> str:
    return title.replace("[", "\\[").replace("]", "\\]")


def enhance_document_navigation(content: str) -> str:
    """Normalize source TOCs and link them to high-confidence document headings."""
    lines = content.splitlines()
    ranges = _toc_ranges(lines)
    if not ranges:
        return content

    entries = _toc_entries(lines, ranges)
    if len(entries) < 2:
        return content
    headings = _collect_headings(lines, ranges, entries)
    for entry in entries:
        entry.target = _match_entry(entry, headings)

    matched_headings = {id(entry.target): entry.target for entry in entries if entry.target}
    _assign_anchors(list(matched_headings.values()))
    entry_replacements: dict[int, list[str]] = {}
    removed_lines: set[int] = set()
    for entry in entries:
        title = _strip_inline_markdown(entry.target.title) if entry.target else entry.title
        if entry.target:
            label = f"[{_escape_link_text(title)}](#{entry.target.anchor})"
        else:
            label = title
        entry_replacements.setdefault(entry.line_start, []).append(f"{'  ' * entry.depth}- {label}")
        removed_lines.update(range(entry.line_start + 1, entry.line_end + 1))

    heading_replacements: dict[int, tuple[str, str]] = {}
    for heading in matched_headings.values():
        level = _heading_level(heading.title, heading.level)
        heading_replacements[heading.line_index] = (
            f'<a id="{heading.anchor}"></a>',
            f"{'#' * level} {heading.title}",
        )

    output: list[str] = []
    for index, line in enumerate(lines):
        if index in removed_lines:
            continue
        if index in entry_replacements:
            output.extend(entry_replacements[index])
            continue
        if index in heading_replacements:
            anchor, heading_line = heading_replacements[index]
            if not output or output[-1] != anchor:
                output.append(anchor)
            output.append(heading_line)
            continue
        output.append(line)
    result = "\n".join(output)
    return result + ("\n" if content.endswith("\n") else "")
