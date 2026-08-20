from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdf2md_frontmatter import (
    FrontMatterEntry,
    NavKind,
    PAGE_LABEL,
    PAGE_ONLY_RE,
    canonical_identifier,
    entry_identifier,
    explicit_entry_kind,
    extract_front_matter,
    navigation_kind,
    normalized_text,
    parse_entry_lines,
    split_title_page,
    strip_inline_markdown,
)


HEADING_RE = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<title>.+?)\s*$")
ANCHOR_RE = re.compile(
    r'^\s*<a\s+id=["\'](?P<id>[^"\']+)["\']'
    r'(?:\s+data-pdf2md-nav=["\'](?P<nav>section|target)["\'])?'
    r'(?:\s+data-pdf2md-heading=["\'](?P<heading>generated)["\'])?\s*></a>\s*$',
    re.I,
)
ANY_ID_RE = re.compile(r'<[A-Za-z][^>]*\bid=["\'](?P<id>[^"\']+)["\'][^>]*>', re.I)
BACKLINK_RE = re.compile(
    r"^\s*\[↑\s+[^]]+\]\(#(?:toc|list-of-(?:figures-and-tables|figures|tables))"
    r"(?:-\d+)?\)\s*$"
)
BULLET_RE = re.compile(
    r"^\s*[-*+]\s+(?:\[(?P<link_title>(?:\\.|[^\\\]])+)\]\(#[^)]+\)|(?P<title>.+?))\s*$"
)
NUMBERED_PREFIX_RE = re.compile(
    r"^\s*(?P<prefix>"
    r"第\s*[0-9一二三四五六七八九十百零〇两]+\s*[章节篇部卷]|"
    r"附录\s*[A-Za-z0-9一二三四五六七八九十]+|"
    r"(?:chapter|section|part|appendix)\s+[A-Za-z0-9一二三四五六七八九十]+|"
    r"(?:\d+|[IVXLCDM]+|[A-Z])(?:[.．\-–—]\d+)*(?:[.．、)]|(?=\s))|"
    r"[一二三四五六七八九十百]+[.．、)]"
    r")\s*",
    re.IGNORECASE,
)
CAPTION_RE = re.compile(
    r"^\s*(?P<type>Figure|Fig\.?|Table|图|表)\s*"
    r"(?P<label>(?:\d+|[A-Z])(?:[.．\-–—]\d+)*)"
    r"(?![.．\-–—]\d)\s*"
    r"(?P<separator>[:：.．\-–—])\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
COMPACT_CAPTION_HEADING_RE = re.compile(
    r"^#{1,6}\s+(?P<type>Figure|Table|图|表)\s+"
    r"(?P<label>(?:\d+|[A-Z])(?:[.．\-–—]\d+)*)\s*$",
    re.IGNORECASE,
)
FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")
FRONT_BEFORE_TOC = {
    "abstract",
    "acknowledgement",
    "acknowledgements",
    "acknowledgment",
    "acknowledgments",
    "dedication",
    "foreword",
    "preface",
    "resume",
    "summary",
    "摘要",
    "致谢",
}
UNNUMBERED_EXACT = FRONT_BEFORE_TOC | {
    "bibliography",
    "conclusion",
    "glossary",
    "index",
    "introduction",
    "references",
    "参考文献",
}
CONTAINER_ONLY_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9一二三四五六七八九十百零〇两]+\s*[章节篇部卷]|"
    r"附录\s*[A-Za-z0-9一二三四五六七八九十]+|"
    r"(?:chapter|part|appendix)\s+[A-Za-z0-9一二三四五六七八九十]+"
    r")\s*[.．:：]?\s*$",
    re.IGNORECASE,
)
CONTEXTUAL_NAV_LEADER_RE = re.compile(
    r"(?:\s*[.\u00b7\u2022\u2026]\s*){2,}"
)
CONTEXTUAL_CONTENTS_ALIAS = "\u5185\u5bb9"
CONTEXTUAL_CONTENTS_MAX_LINE = 1000
CONTEXTUAL_CONTENTS_MIN_CONFIDENCE = 0.90


@dataclass(slots=True)
class NavEntry:
    kind: NavKind
    title: str
    page: str
    depth: int
    native: bool = False
    structured: bool = False
    target: "Target | None" = None


@dataclass(slots=True)
class NavSection:
    start: int
    end: int
    kind: NavKind
    title: str
    anchor: str = ""
    entries: list[NavEntry] = field(default_factory=list)
    sources: list["NavSection"] = field(default_factory=list)
    replace_debris: bool = False
    combined: bool = False
    owned_tail_start: int = -1
    owned_tail_records: list[tuple[int, int, str, str]] = field(default_factory=list)
    suppressed: bool = False


@dataclass(slots=True)
class Target:
    line_index: int
    kind: str
    title: str
    level: int
    label: str = ""
    caption_type: str = ""
    separator: str = ""
    context_prefix: str = ""
    existing_heading: bool = False
    navigation_section: NavSection | None = None
    anchor: str = ""
    sources: list[NavSection] = field(default_factory=list)


def _trusted_source_entry(entry: NavEntry) -> bool:
    return entry.native or entry.structured


def _is_continued(title: str) -> bool:
    value = unicodedata.normalize("NFKC", strip_inline_markdown(title)).casefold()
    return bool(re.search(r"(?:continued|cont\.?|续)\s*\)?\s*$", value))


def _line_in_sections(index: int, sections: list[NavSection]) -> bool:
    return any(section.start <= index < section.end for section in sections)


def _caption_kind(value: str) -> NavKind:
    lowered = value.casefold()
    return "tables" if lowered == "table" or value == "表" else "figures"


def _caption_heading_label(target: Target) -> str:
    if target.caption_type in {"图", "表"}:
        return f"{target.caption_type} {target.label}"
    label = "Table" if target.kind == "tables" else "Figure"
    return f"{label} {target.label}"


def _is_combined_navigation(title: str) -> bool:
    value = unicodedata.normalize("NFKC", strip_inline_markdown(title)).strip()
    value = re.sub(
        r"\s*(?:\(\s*(?:continued|cont\.?|\u7eed)\s*\)|"
        r"[-\u2013\u2014]\s*(?:continued|cont\.?|\u7eed)|"
        r"(?:continued|cont\.?|\u7eed))\s*$",
        "",
        value,
        flags=re.I,
    )
    return normalized_text(value) in {
        "图表目录",
        "listoffiguresandtables",
        "listoffigurestables",
        "listoftablesandfigures",
        "listoftablesfigures",
    }


def _fenced_line_indexes(lines: list[str]) -> set[int]:
    fenced: set[int] = set()
    marker = ""
    minimum_length = 0
    for index, line in enumerate(lines):
        if marker:
            fenced.add(index)
            if re.fullmatch(
                rf" {{0,3}}{re.escape(marker)}{{{minimum_length},}}[ \t]*",
                line,
            ):
                marker = ""
                minimum_length = 0
            continue
        opened = FENCE_OPEN_RE.match(line)
        if opened is None:
            continue
        fence = opened.group("fence")
        marker = fence[0]
        minimum_length = len(fence)
        fenced.add(index)
    return fenced


def _code_line_indexes(lines: list[str]) -> set[int]:
    ignored = _fenced_line_indexes(lines)
    in_html_code = False
    for index, line in enumerate(lines):
        if index in ignored:
            continue
        lowered = line.lstrip().casefold()
        if re.match(r"^<(?:pre|code)(?:\s|>)", lowered):
            in_html_code = True
        if in_html_code:
            ignored.add(index)
        if in_html_code and re.search(r"</(?:pre|code)>", lowered):
            in_html_code = False
    return ignored


def _strip_generated_navigation(lines: list[str]) -> list[str]:
    remove: set[int] = set()
    ignored = _code_line_indexes(lines)
    for index, line in enumerate(lines):
        if index in ignored:
            continue
        anchor = ANCHOR_RE.fullmatch(line)
        if anchor is None or anchor.group("nav") is None:
            continue
        remove.add(index)
        heading_index = index + 1
        if heading_index >= len(lines) or HEADING_RE.fullmatch(lines[heading_index]) is None:
            continue
        if (
            anchor.group("nav").casefold() == "target"
            and anchor.group("heading") == "generated"
        ):
            if COMPACT_CAPTION_HEADING_RE.fullmatch(lines[heading_index]):
                remove.add(heading_index)
        backlink_index = heading_index + 1
        while backlink_index < len(lines) and BACKLINK_RE.fullmatch(lines[backlink_index]):
            remove.add(backlink_index)
            backlink_index += 1
    return [line for index, line in enumerate(lines) if index not in remove]


def _clean_entry_line(line: str) -> str:
    heading = HEADING_RE.fullmatch(line)
    if heading:
        line = heading.group("title")
    bullet = BULLET_RE.fullmatch(line)
    if bullet:
        line = bullet.group("link_title") or bullet.group("title") or ""
    return strip_inline_markdown(line)


def _numeric_identifier_sequence(title: str) -> tuple[int, ...] | None:
    identifier = entry_identifier(title)
    if re.fullmatch(r"\d+(?:\.\d+)*", identifier) is None:
        return None
    return tuple(int(part) for part in identifier.split("."))


def _is_navigation_debris(line: str) -> bool:
    return len(line) > 500 and bool(
        re.search(r"(?:\s*[.．·•…⋯]\s*){5,}", line)
    )


_LEADER_SUFFIX_CHARS = frozenset(" .．·•…⋯\t")


def _recover_navigation_debris_title(line: str, kind: NavKind) -> str | None:
    """Recover a short numbered title followed only by a pathological leader run."""
    if len(line) <= 500:
        return None
    index = len(line)
    leader_count = 0
    while index and line[index - 1] in _LEADER_SUFFIX_CHARS:
        index -= 1
        if not line[index].isspace():
            leader_count += 1
    title = strip_inline_markdown(line[:index]).strip()
    if leader_count < 20 or not title or len(title) > 400:
        return None
    parsed = split_title_page(title, kind)
    if parsed is None or parsed[1] or not entry_identifier(parsed[0]):
        return None
    return parsed[0]


def _wrapped_entry_completion(
    lines: list[str], start: int, limit: int, kind: NavKind, ignored: set[int]
) -> tuple[int, str] | None:
    """Find a page-bearing completion for a two/three-line directory entry."""
    joined = _clean_entry_line(lines[start])
    if not joined or len(joined) > 400:
        return None
    for index in range(start + 1, min(start + 3, limit)):
        if index in ignored or not lines[index].strip() or HEADING_RE.fullmatch(lines[index]):
            break
        part = _clean_entry_line(lines[index])
        if not part or len(joined) + len(part) > 400:
            break
        separate = split_title_page(part, kind)
        if separate is not None and separate[1]:
            break
        joined = f"{joined} {part}".strip()
        completed = split_title_page(joined, kind)
        if completed is not None and completed[1]:
            return index, completed[0]
    return None


def _has_trusted_page_less_contents_run(
    lines: list[str], start: int, limit: int, ignored: set[int]
) -> bool:
    """Require a long, monotonic front-matter run before accepting page-less entries."""
    entries = 0
    numbered = 0
    last_sequence: tuple[int, ...] | None = None
    inspected = 0
    page_only = 0
    page_bearing = 0
    leader_rows = 0
    for index in range(start, min(limit, start + 80)):
        if index in ignored or HEADING_RE.fullmatch(lines[index]):
            break
        cleaned = _clean_entry_line(lines[index])
        if not cleaned:
            continue
        if PAGE_ONLY_RE.fullmatch(cleaned):
            page_only += 1
            continue
        inspected += 1
        if inspected > 64:
            break
        if lines[index].startswith("    ") or lines[index].startswith("\t"):
            break
        if _is_navigation_debris(lines[index]):
            recovered = _recover_navigation_debris_title(lines[index], "contents")
            if recovered is None:
                break
            cleaned = recovered
            leader_rows += 1
        parsed = split_title_page(cleaned, "contents")
        if parsed is None:
            break
        if parsed[1]:
            page_bearing += 1
        if re.search(r"(?:\.\s*){3,}", lines[index]):
            leader_rows += 1
        sequence = _numeric_identifier_sequence(parsed[0])
        if sequence is not None:
            if last_sequence is not None and sequence <= last_sequence:
                break
            last_sequence = sequence
            numbered += 1
        entries += 1
    layout_evidence = page_only >= 3 or page_bearing >= 3 or leader_rows >= 3
    return entries >= 6 and numbered >= 3 and layout_evidence


def _trusted_contextual_contents_pages(
    report: Mapping[str, Any] | None,
    selected_physical_pages: Collection[int] | None,
) -> set[int]:
    """Return high-confidence V1 pages that also carry contents INDEX blocks."""
    if not isinstance(report, Mapping) or report.get("schema") != "pdf2md.front-regions.v1":
        return set()
    selected = _selected_page_filter(selected_physical_pages)
    if selected_physical_pages is not None and not selected:
        return set()

    navigation = report.get("navigation")
    raw_contents = navigation.get("contents") if isinstance(navigation, Mapping) else None
    if not isinstance(raw_contents, list):
        return set()
    navigation_pages: set[int] = set()
    for raw_page in raw_contents[:64]:
        if not isinstance(raw_page, Mapping):
            continue
        page = raw_page.get("page")
        blocks = raw_page.get("blocks")
        has_text_block = isinstance(blocks, list) and any(
            isinstance(block, list)
            and any(isinstance(value, str) and value.strip() for value in block[:512])
            for block in blocks[:128]
        )
        if (
            isinstance(page, int)
            and not isinstance(page, bool)
            and page > 0
            and has_text_block
            and (selected is None or page in selected)
        ):
            navigation_pages.add(page)

    trusted: set[int] = set()
    pages = report.get("pages")
    if not isinstance(pages, list):
        return trusted
    for raw_page in pages[:64]:
        if not isinstance(raw_page, Mapping):
            continue
        page = raw_page.get("page")
        confidence = raw_page.get("confidence")
        if (
            isinstance(page, int)
            and not isinstance(page, bool)
            and page in navigation_pages
            and raw_page.get("kind") == "contents"
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and float(confidence) >= CONTEXTUAL_CONTENTS_MIN_CONFIDENCE
        ):
            trusted.add(page)
    return trusted


def _is_contextual_contents_alias(
    lines: list[str],
    start: int,
    title: str,
    ignored: set[int],
    trusted_pages: set[int],
    structured_navigation: Collection[tuple[int, int, NavKind, NavEntry]],
) -> bool:
    """Accept ambiguous Chinese contents only with report and local INDEX evidence."""
    if (
        normalized_text(title) != normalized_text(CONTEXTUAL_CONTENTS_ALIAS)
        or start >= CONTEXTUAL_CONTENTS_MAX_LINE
        or not trusted_pages
    ):
        return False

    structured_titles = {
        normalized_text(entry.title)
        for page, _order, source_kind, entry in structured_navigation
        if page in trusted_pages and source_kind == "contents"
    }
    structured_titles.discard("")
    matched_titles: set[str] = set()
    leader_page_rows = 0
    inspected = 0
    for index in range(start + 1, min(len(lines), start + 65)):
        if index in ignored or HEADING_RE.fullmatch(lines[index]) is not None:
            break
        cleaned = _clean_entry_line(lines[index])
        if not cleaned:
            continue
        inspected += 1
        if inspected > 16:
            break
        parsed = split_title_page(cleaned, "contents")
        if (
            parsed is not None
            and parsed[1]
            and CONTEXTUAL_NAV_LEADER_RE.search(lines[index]) is not None
        ):
            leader_page_rows += 1
        local_title = parsed[0] if parsed is not None else cleaned
        key = normalized_text(local_title)
        if key and key in structured_titles:
            matched_titles.add(key)
        if leader_page_rows >= 3 or len(matched_titles) >= 3:
            return True
    return False


def _section_ranges(
    lines: list[str],
    *,
    front_regions: Mapping[str, Any] | None = None,
    selected_physical_pages: Collection[int] | None = None,
    structured_navigation: Collection[tuple[int, int, NavKind, NavEntry]] = (),
) -> list[NavSection]:
    fenced = _code_line_indexes(lines)
    trusted_contents_pages = _trusted_contextual_contents_pages(
        front_regions,
        selected_physical_pages,
    )
    starts: list[tuple[int, NavKind, str, bool]] = []
    for index, line in enumerate(lines):
        if index in fenced:
            continue
        heading = HEADING_RE.fullmatch(line)
        if not heading:
            continue
        title = heading.group("title")
        kind = navigation_kind(title)
        if kind is None and _is_contextual_contents_alias(
            lines,
            index,
            title,
            fenced,
            trusted_contents_pages,
            structured_navigation,
        ):
            kind = "contents"
        if kind is not None:
            title = title.strip()
            starts.append((index, kind, title, _is_combined_navigation(title)))

    sections: list[NavSection] = []
    for position, (start, kind, title, combined) in enumerate(starts):
        limit = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        next_heading = next(
            (
                index
                for index in range(start + 1, limit)
                if index not in fenced and HEADING_RE.fullmatch(lines[index]) is not None
            ),
            None,
        )
        trust_limit = next_heading if next_heading is not None else limit
        end = limit
        seen_entries = 0
        last_numeric_sequence: dict[NavKind, tuple[int, ...]] = {}
        wrapped_until = -1
        saw_corrupt_debris = False
        trusted_page_less_contents = False
        can_trust_page_less_contents = (
            kind == "contents"
            and start < 1000
            and trust_limit < len(lines)
            and trust_limit - start < 1500
        )
        for index in range(start + 1, limit):
            if index <= wrapped_until:
                continue
            if index in fenced:
                end = index
                break
            heading = HEADING_RE.fullmatch(lines[index])
            if heading:
                parsed = split_title_page(heading.group("title"), kind)
                leader = re.search(r"(?:\s*[.．·•…⋯]\s*){3,}", heading.group("title"))
                if parsed and parsed[1] and leader:
                    seen_entries += 1
                    continue
                end = index
                break
            cleaned = _clean_entry_line(lines[index])
            if not cleaned:
                continue
            if PAGE_ONLY_RE.fullmatch(cleaned):
                continue
            rendered_bullet = BULLET_RE.fullmatch(lines[index])
            if rendered_bullet is not None:
                # A previously published navigation bullet is authoritative even if
                # OCR text at its end resembles a page number or restarts numbering.
                # Re-rendering the whole bullet block prevents legacy links from being
                # stranded after their generated anchors are removed.
                bullet_title = strip_inline_markdown(
                    rendered_bullet.group("link_title")
                    or rendered_bullet.group("title")
                    or ""
                )
                bullet_kind = explicit_entry_kind(bullet_title) or kind
                bullet_sequence = _numeric_identifier_sequence(bullet_title)
                if bullet_sequence is not None:
                    last_numeric_sequence[bullet_kind] = bullet_sequence
                seen_entries += 1
                continue
            if _is_navigation_debris(lines[index]):
                saw_corrupt_debris = True
                continue
            if saw_corrupt_debris:
                if not trusted_page_less_contents:
                    end = index
                    break
                saw_corrupt_debris = False
            previous_blank = index > start + 1 and not lines[index - 1].strip()
            if previous_blank and (
                lines[index].startswith("    ") or lines[index].startswith("\t")
            ):
                end = index
                break
            parsed = split_title_page(cleaned, kind)
            if parsed:
                parsed_title, page = parsed
                entry_kind = explicit_entry_kind(parsed_title) or kind
                sequence = _numeric_identifier_sequence(parsed_title)
                if page and seen_entries >= 1:
                    next_index = index + 1
                    while next_index < limit and not lines[next_index].strip():
                        next_index += 1
                    next_is_boundary = next_index < limit and (
                        next_index in fenced
                        or HEADING_RE.fullmatch(lines[next_index]) is not None
                    )
                    next_parsed = (
                        split_title_page(_clean_entry_line(lines[next_index]), kind)
                        if next_index < limit and not next_is_boundary
                        else None
                    )
                    restarted = (
                        sequence is not None
                        and entry_kind in last_numeric_sequence
                        and sequence <= last_numeric_sequence[entry_kind]
                    )
                    if restarted or (
                        next_index < limit and not next_is_boundary and next_parsed is None
                    ):
                        end = index
                        break
                if not page and BULLET_RE.fullmatch(lines[index]) is None:
                    completion = _wrapped_entry_completion(lines, index, limit, kind, fenced)
                    if completion is not None:
                        wrapped_until, completed_title = completion
                        completed_kind = explicit_entry_kind(completed_title) or kind
                        completed_sequence = _numeric_identifier_sequence(completed_title)
                        if completed_sequence is not None:
                            last_numeric_sequence[completed_kind] = completed_sequence
                        seen_entries += 1
                        continue
                    if (
                        not trusted_page_less_contents
                        and can_trust_page_less_contents
                        and _has_trusted_page_less_contents_run(
                            lines, index, trust_limit, fenced
                        )
                    ):
                        trusted_page_less_contents = True
                    if trusted_page_less_contents:
                        if (
                            sequence is not None
                            and entry_kind in last_numeric_sequence
                            and sequence <= last_numeric_sequence[entry_kind]
                        ):
                            end = index
                            break
                        if sequence is not None:
                            last_numeric_sequence[entry_kind] = sequence
                        seen_entries += 1
                        continue
                    if seen_entries >= 1:
                        end = index
                        break
                if sequence is not None:
                    last_numeric_sequence[entry_kind] = sequence
                seen_entries += 1
                continue
            if seen_entries >= 1:
                end = index
                break
        sections.append(
            NavSection(start=start, end=end, kind=kind, title=title, combined=combined)
        )

    merged: list[NavSection] = []
    for section in sections:
        if (
            merged
            and merged[-1].kind == section.kind
            and merged[-1].combined == section.combined
            and merged[-1].end == section.start
            and _is_continued(section.title)
        ):
            merged[-1].end = section.end
            continue
        merged.append(section)
    return merged


def _entry_depth(title: str, kind: NavKind) -> int:
    if kind != "contents":
        return 0
    cleaned = unicodedata.normalize("NFKC", strip_inline_markdown(title))
    match = NUMBERED_PREFIX_RE.match(cleaned)
    if not match:
        return 0
    prefix = match.group("prefix")
    identifier = re.search(r"(?:\d+|[A-Z])(?:[.\-]\d+)*", prefix, flags=re.I)
    if not identifier:
        return 0
    return min(5, len(re.findall(r"[.\-]", identifier.group(0))))


def _is_repeated_navigation_header(value: str, kind: NavKind) -> bool:
    if navigation_kind(value) == kind:
        return True
    parsed = split_title_page(value, kind)
    return bool(parsed and parsed[1] and navigation_kind(parsed[0]) == kind)


def _pair_parallel_page_column(values: list[str], kind: NavKind) -> list[str]:
    """Pair a strict T^n/P^n OCR layout without guessing unequal columns."""
    compact = [value for value in values if value]
    title_count = 0
    while title_count < len(compact):
        value = compact[title_count]
        if PAGE_ONLY_RE.fullmatch(value):
            break
        parsed = split_title_page(value, kind)
        if parsed is None or parsed[1]:
            break
        title_count += 1
    if title_count < 3:
        return values

    page_end = title_count
    while page_end < len(compact) and PAGE_ONLY_RE.fullmatch(compact[page_end]):
        page_end += 1
    if page_end - title_count != title_count:
        return values

    paired = [
        f"{compact[index]} {compact[title_count + index]}"
        for index in range(title_count)
    ]
    return paired + compact[page_end:]


def _entries_from_markdown(lines: list[str], section: NavSection) -> list[NavEntry]:
    fenced = _code_line_indexes(lines)
    rendered_bullets: list[re.Match[str]] = []
    has_raw_content = False
    for index, line in enumerate(
        lines[section.start + 1 : section.end], section.start + 1
    ):
        if index in fenced:
            continue
        value = _clean_entry_line(line)
        if not value or _is_repeated_navigation_header(value, section.kind):
            continue
        if PAGE_ONLY_RE.fullmatch(value):
            continue
        bullet = BULLET_RE.fullmatch(line)
        if bullet is None:
            has_raw_content = True
        else:
            rendered_bullets.append(bullet)

    if rendered_bullets and not has_raw_content:
        entries: list[NavEntry] = []
        for bullet in rendered_bullets:
            title = strip_inline_markdown(
                bullet.group("link_title") or bullet.group("title") or ""
            )
            if not title or navigation_kind(title) == section.kind:
                continue
            entry_kind = explicit_entry_kind(title) or section.kind
            entries.append(
                NavEntry(
                    kind=entry_kind,
                    title=title,
                    page="",
                    depth=_entry_depth(title, section.kind),
                )
            )
        return entries

    cleaned: list[str] = []
    for index, line in enumerate(lines[section.start + 1 : section.end], section.start + 1):
        if index in fenced:
            continue
        value = (
            _recover_navigation_debris_title(line, section.kind)
            if _is_navigation_debris(line)
            else _clean_entry_line(line)
        )
        if not value or _is_repeated_navigation_header(value, section.kind):
            cleaned.append("")
            continue
        if PAGE_ONLY_RE.fullmatch(value):
            cleaned.append("")
            continue
        cleaned.append(value)
    cleaned = _recover_body_aligned_entry_lines(cleaned, lines, section)
    cleaned = _pair_parallel_page_column(cleaned, section.kind)
    return [
        NavEntry(
            kind=entry.kind,
            title=entry.title,
            page=entry.page,
            depth=_entry_depth(entry.title, entry.kind),
        )
        for entry in parse_entry_lines(cleaned, section.kind)
    ]


def _native_entries(entries: tuple[FrontMatterEntry, ...]) -> list[NavEntry]:
    return [
        NavEntry(
            kind=entry.kind,
            title=entry.title,
            page=entry.page,
            depth=_entry_depth(entry.title, entry.kind),
            native=True,
        )
        for entry in entries
    ]


StructuredEntryRecord = tuple[int, int, NavKind, NavEntry]
_STRUCTURED_NAV_KINDS: dict[str, NavKind] = {
    "contents": "contents",
    "list_of_figures": "figures",
    "list_of_tables": "tables",
}


@dataclass(slots=True)
class StructuredNavigationRun:
    """One physical-page run opened by an explicit navigation heading."""

    kind: NavKind
    start_page: int
    records: list[StructuredEntryRecord] = field(default_factory=list)


def _selected_page_filter(
    selected_physical_pages: Collection[int] | None,
) -> set[int] | None:
    if selected_physical_pages is None:
        return None
    selected: set[int] = set()
    for index, value in enumerate(selected_physical_pages):
        if index >= 4096:
            break
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            selected.add(value)
    return selected


def _trusted_partial_native_kinds(
    report: Mapping[str, Any] | None,
    selected_physical_pages: Collection[int] | None,
) -> set[NavKind]:
    """Gate native-text repair to identified navigation inside one page run."""
    selected = _selected_page_filter(selected_physical_pages)
    if not selected:
        return set()
    ordered = sorted(selected)
    if ordered != list(range(ordered[0], ordered[-1] + 1)):
        return set()
    if not isinstance(report, Mapping) or report.get("schema") != "pdf2md.front-regions.v1":
        return set()

    trusted: set[NavKind] = set()
    raw_pages = report.get("pages")
    if isinstance(raw_pages, list):
        for raw_page in raw_pages[:64]:
            if not isinstance(raw_page, Mapping):
                continue
            page = raw_page.get("page")
            confidence = raw_page.get("confidence")
            kind = _STRUCTURED_NAV_KINDS.get(str(raw_page.get("kind")))
            if (
                kind is not None
                and isinstance(page, int)
                and not isinstance(page, bool)
                and page in selected
                and isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and float(confidence) >= 0.68
            ):
                trusted.add(kind)

    # A damaged list can be confidently identified by its explicit heading
    # even when its unusable entry block correctly makes the classifier
    # abstain.  This metadata never supplies text; it only permits pypdf to
    # recover entries from the same selected physical page.
    recovery_pages = report.get("native_recovery_pages")
    if isinstance(recovery_pages, list):
        for item in recovery_pages[:64]:
            if not isinstance(item, Mapping):
                continue
            page = item.get("page")
            confidence = item.get("confidence")
            kind = _STRUCTURED_NAV_KINDS.get(str(item.get("kind")))
            evidence = item.get("evidence")
            evidence_set = {
                value for value in evidence if isinstance(value, str)
            } if isinstance(evidence, list) else set()
            if (
                kind is not None
                and isinstance(page, int)
                and not isinstance(page, bool)
                and page in selected
                and isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and float(confidence) >= 0.60
                and {"explicit_title", "unusable_navigation_debris"}
                <= evidence_set
            ):
                trusted.add(kind)
    return trusted


def _structured_navigation_entries(
    report: Mapping[str, Any] | None,
    selected_physical_pages: Collection[int] | None,
) -> list[StructuredEntryRecord]:
    """Parse bounded MinerU INDEX evidence; never create a link target."""
    if not isinstance(report, Mapping) or report.get("schema") != "pdf2md.front-regions.v1":
        return []
    selected = _selected_page_filter(selected_physical_pages)
    if selected_physical_pages is not None and not selected:
        return []

    page_evidence: dict[tuple[int, str], float] = {}
    pages = report.get("pages")
    if isinstance(pages, list):
        for raw_page in pages[:64]:
            if not isinstance(raw_page, Mapping):
                continue
            page = raw_page.get("page")
            kind = raw_page.get("kind")
            confidence = raw_page.get("confidence")
            if (
                isinstance(page, int)
                and not isinstance(page, bool)
                and page > 0
                and kind in _STRUCTURED_NAV_KINDS
                and isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and float(confidence) >= 0.68
            ):
                page_evidence[(page, str(kind))] = float(confidence)

    navigation = report.get("navigation")
    if not isinstance(navigation, Mapping):
        return []
    records: list[StructuredEntryRecord] = []
    order = 0
    for report_kind, entry_kind in _STRUCTURED_NAV_KINDS.items():
        raw_pages = navigation.get(report_kind)
        if not isinstance(raw_pages, list):
            continue
        for raw_page in raw_pages[:64]:
            if not isinstance(raw_page, Mapping):
                continue
            page = raw_page.get("page")
            if (
                not isinstance(page, int)
                or isinstance(page, bool)
                or page <= 0
                or (selected is not None and page not in selected)
                or (page, report_kind) not in page_evidence
            ):
                continue
            blocks = raw_page.get("blocks")
            if not isinstance(blocks, list):
                continue
            for raw_block in blocks[:128]:
                if not isinstance(raw_block, list):
                    continue
                texts = [
                    value.strip()
                    for value in raw_block[:512]
                    if isinstance(value, str) and 0 < len(value.strip()) <= 4096
                ]
                if not texts:
                    continue
                for parsed in parse_entry_lines(texts, entry_kind):
                    records.append(
                        (
                            page,
                            order,
                            entry_kind,
                            NavEntry(
                                kind=parsed.kind,
                                title=parsed.title,
                                page=parsed.page,
                                depth=_entry_depth(parsed.title, parsed.kind),
                                structured=True,
                            ),
                        )
                    )
                    order += 1
    records.sort(key=lambda item: (item[0], item[1]))
    return records


def _structured_navigation_runs(
    report: Mapping[str, Any] | None,
    selected_physical_pages: Collection[int] | None,
    records: Collection[StructuredEntryRecord],
) -> dict[NavKind, list[StructuredNavigationRun]]:
    """Partition trusted records at explicit physical-page navigation headings."""
    if not isinstance(report, Mapping) or report.get("schema") != "pdf2md.front-regions.v1":
        return {}
    selected = _selected_page_filter(selected_physical_pages)
    if selected_physical_pages is not None and not selected:
        return {}

    by_page: dict[tuple[NavKind, int], list[StructuredEntryRecord]] = {}
    for record in records:
        page, _order, kind, _entry = record
        by_page.setdefault((kind, page), []).append(record)

    page_runs: dict[NavKind, list[tuple[int, bool]]] = {}
    seen: set[tuple[NavKind, int]] = set()
    pages = report.get("pages")
    if not isinstance(pages, list):
        return {}
    for raw_page in pages[:64]:
        if not isinstance(raw_page, Mapping):
            continue
        page = raw_page.get("page")
        report_kind = raw_page.get("kind")
        confidence = raw_page.get("confidence")
        kind = _STRUCTURED_NAV_KINDS.get(str(report_kind))
        if (
            kind is None
            or not isinstance(page, int)
            or isinstance(page, bool)
            or page <= 0
            or (selected is not None and page not in selected)
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or float(confidence) < 0.68
            or (kind, page) in seen
        ):
            continue
        seen.add((kind, page))
        evidence = raw_page.get("evidence")
        explicit = isinstance(evidence, list) and any(
            value in {"explicit_title", "explicit_heading"}
            for value in evidence
            if isinstance(value, str)
        )
        page_runs.setdefault(kind, []).append((page, explicit))

    result: dict[NavKind, list[StructuredNavigationRun]] = {}
    for kind, page_evidence in page_runs.items():
        active: StructuredNavigationRun | None = None
        runs: list[StructuredNavigationRun] = []
        for page, explicit in sorted(page_evidence):
            if explicit and active is not None:
                if active.records:
                    runs.append(active)
                active = None
            if active is None:
                active = StructuredNavigationRun(kind=kind, start_page=page)
            active.records.extend(by_page.get((kind, page), ()))
        if active is not None and active.records:
            runs.append(active)
        if runs:
            result[kind] = runs
    return result


def _run_entries(run: StructuredNavigationRun) -> list[NavEntry]:
    return [
        entry
        for _page, _order, source_kind, entry in run.records
        if entry.kind == source_kind
    ]


def _exact_entry_overlap(left: Collection[NavEntry], right: Collection[NavEntry]) -> int:
    """Count exact normalized titles; numeric identifiers alone are not evidence."""
    left_titles = {
        (entry.kind, normalized_text(entry.title))
        for entry in left
        if normalized_text(entry.title)
    }
    right_titles = {
        (entry.kind, normalized_text(entry.title))
        for entry in right
        if normalized_text(entry.title)
    }
    return len(left_titles & right_titles)


def _monotonic_run_alignment_supported(
    sections: Collection[NavSection],
    parsed: Mapping[int, list[NavEntry]],
    run_entries: Collection[list[NavEntry]],
) -> bool:
    """Require each Markdown section to match its same-position physical run."""
    section_list = list(sections)
    entry_runs = list(run_entries)
    if len(section_list) != len(entry_runs):
        return False
    scores = [
        [
            _exact_entry_overlap(parsed.get(section.start, ()), entries)
            for entries in entry_runs
        ]
        for section in section_list
    ]
    for index, row in enumerate(scores):
        chosen = row[index]
        alternatives = row[:index] + row[index + 1 :]
        if chosen <= 0 or (alternatives and chosen <= max(alternatives)):
            return False
    return True


def _partitioned_structured_entries(
    sections: Collection[NavSection],
    parsed: Mapping[int, list[NavEntry]],
    runs: Mapping[NavKind, list[StructuredNavigationRun]],
) -> tuple[dict[int, list[NavEntry]], set[NavKind]]:
    """Monotonically align multiple same-kind sections to distinct page runs.

    Partitioning is deliberately withheld unless every section has a strictly
    stronger exact-title overlap with its same-position physical run.  This
    uses the local raw order plus report boundaries and cannot guess from a
    Chinese/English language label alone.
    """
    assigned: dict[int, list[NavEntry]] = {}
    partitioned: set[NavKind] = set()
    for kind, kind_runs in runs.items():
        kind_sections = [
            section
            for section in sections
            if not section.combined and section.kind == kind
        ]
        if len(kind_sections) < 2 or len(kind_runs) != len(kind_sections):
            continue
        run_entries = [_run_entries(run) for run in kind_runs]
        if not _monotonic_run_alignment_supported(
            kind_sections, parsed, run_entries
        ):
            continue
        for section, entries in zip(kind_sections, run_entries):
            assigned[section.start] = entries
        partitioned.add(kind)
    return assigned, partitioned


def _collapse_repeated_structured_sections(
    lines: list[str],
    sections: list[NavSection],
    runs: Mapping[NavKind, list[StructuredNavigationRun]],
    report: Mapping[str, Any] | None,
) -> None:
    """Fold a repeated same-title page header into the preceding list.

    Some datasheets repeat ``TABLE OF CONTENTS`` at the top of every physical
    directory page. MinerU consequently emits several Markdown sections. We
    collapse only a proven one-to-one structured-page alignment, consecutive
    physical runs, adjacent navigation sections, and an exact normalized title.
    Different bilingual headings therefore remain independent.
    """
    body_start = (
        report.get("body_start_page") if isinstance(report, Mapping) else None
    )
    if (
        isinstance(body_start, bool)
        or not isinstance(body_start, int)
        or body_start <= 0
    ):
        body_start = None
    positions = {id(section): index for index, section in enumerate(sections)}
    parsed = {section.start: section.entries for section in sections}
    for kind, kind_runs in runs.items():
        kind_sections = [
            section
            for section in sections
            if not section.combined and section.kind == kind
        ]
        if len(kind_sections) < 2 or len(kind_runs) != len(kind_sections):
            continue
        run_entries = [_run_entries(run) for run in kind_runs]
        if not _monotonic_run_alignment_supported(
            kind_sections, parsed, run_entries
        ):
            continue

        keeper = kind_sections[0]
        previous_section = kind_sections[0]
        previous_run = kind_runs[0]
        for section, run in zip(kind_sections[1:], kind_runs[1:]):
            previous_pages = [page for page, *_rest in previous_run.records]
            current_pages = [page for page, *_rest in run.records]
            adjacent_pages = bool(
                previous_pages
                and current_pages
                and max(previous_pages) + 1 == min(current_pages)
            )
            adjacent_sections = (
                positions[id(section)] == positions[id(previous_section)] + 1
            )
            same_title = bool(
                normalized_text(keeper.title)
                and normalized_text(keeper.title) == normalized_text(section.title)
            )
            before_body = (
                body_start is None
                or min(current_pages, default=body_start) < body_start
            )
            interstitial_headings = [
                match.group("title")
                for line in lines[previous_section.end : section.start]
                if (match := HEADING_RE.fullmatch(line)) is not None
            ]
            frontmatter_only_gap = all(
                normalized_text(title)
                in {
                    "revisionhistory",
                    "revisions",
                    "changehistory",
                    "documenthistory",
                    "recordofchanges",
                }
                or re.search(r"\b(?:rev(?:ision)?s?)\.?\b", title, re.I)
                is not None
                for title in interstitial_headings
            )
            if (
                adjacent_pages
                and adjacent_sections
                and same_title
                and before_body
                and frontmatter_only_gap
            ):
                keeper.entries = _merge_entry_sequences(keeper.entries, section.entries)
                section.suppressed = True
            else:
                keeper = section
            previous_section = section
            previous_run = run


def _populate_entries(
    lines: list[str],
    sections: list[NavSection],
    source: Path | None,
    frontmatter_cache: Path | None,
    force_frontmatter: bool = False,
    front_regions: Mapping[str, Any] | None = None,
    selected_physical_pages: Collection[int] | None = None,
    structured_navigation: Collection[StructuredEntryRecord] | None = None,
) -> None:
    partial_native_kinds = (
        _trusted_partial_native_kinds(front_regions, selected_physical_pages)
        if selected_physical_pages is not None
        else None
    )
    native = {}
    if (
        source is not None
        and source.is_file()
        and (partial_native_kinds is None or partial_native_kinds)
    ):
        native = extract_front_matter(
            source,
            cache_path=frontmatter_cache,
            force=force_frontmatter,
            physical_pages=selected_physical_pages,
        )
        if partial_native_kinds is not None:
            native = {
                kind: section
                for kind, section in native.items()
                if kind in partial_native_kinds
            }
    structured = list(structured_navigation) if structured_navigation is not None else (
        _structured_navigation_entries(
            front_regions,
            selected_physical_pages,
        )
    )
    parsed_by_section = {
        section.start: _entries_from_markdown(lines, section)
        for section in sections
    }
    runs = _structured_navigation_runs(
        front_regions,
        selected_physical_pages,
        structured,
    )
    partitioned_entries, partitioned_kinds = _partitioned_structured_entries(
        sections,
        parsed_by_section,
        runs,
    )
    used_native: set[NavKind] = set()
    used_structured: set[NavKind] = set(partitioned_kinds)
    for section in sections:
        parsed = parsed_by_section[section.start]
        native_section = (
            None if section.kind in partitioned_kinds else native.get(section.kind)
        )
        if native_section and section.kind not in used_native:
            native_entries = _native_entries(native_section.entries)
            native_pages = sum(bool(entry.page) for entry in native_entries)
            native_ratio = native_pages / max(1, len(native_entries))
            if native_ratio >= 0.5 and len(native_entries) >= len(parsed):
                section.entries = native_entries
                used_native.add(section.kind)
            else:
                section.entries = parsed
        else:
            section.entries = parsed
        assigned_entries = partitioned_entries.get(section.start)
        if assigned_entries is not None:
            section.entries = _merge_entry_sequences(
                section.entries,
                assigned_entries,
            )
            continue
        accepted_sources: set[NavKind] = (
            {"figures", "tables"} if section.combined else {section.kind}
        )
        available_sources = accepted_sources - used_structured
        structured_entries = [
            entry
            for _page, _order, source_kind, entry in structured
            if source_kind in available_sources
            and (
                section.combined
                or entry.kind == section.kind
            )
        ]
        if structured_entries:
            section.entries = _merge_entry_sequences(
                section.entries,
                structured_entries,
            )
            used_structured.update(available_sources)


RAW_TRAILING_PAGE_RE = re.compile(
    rf"^(?P<title>.+?\S)\s+(?P<page>{PAGE_LABEL})\s*$",
    re.IGNORECASE,
)


def _body_heading_targets_for_section(
    lines: list[str], section: NavSection
) -> dict[str, list[Target]]:
    """Index explicit body headings outside the owned navigation block."""
    indexed: dict[str, list[Target]] = {}
    for target in _collect_heading_targets(lines, [section]):
        key = normalized_text(target.title)
        if key:
            indexed.setdefault(key, []).append(target)
        prefix, body = _prefix_and_body(target.title, allow_single_letter=False)
        if prefix and body and body != key:
            indexed.setdefault(body, []).append(target)
    return indexed


def _body_aligned_source_entry(
    value: str,
    kind: NavKind,
    body_targets: dict[str, list[Target]],
) -> tuple[str, str] | None:
    """Recover one damaged list row from a unique explicit body heading."""
    if kind != "contents":
        return None
    fallback = RAW_TRAILING_PAGE_RE.fullmatch(_clean_entry_line(value))
    if fallback is None:
        return None
    source_title = re.sub(r"(?:\s*\.\s*){2,}", " ", fallback.group("title"))
    source_title = re.sub(r"\s+", " ", source_title).strip(" .")
    if not source_title or _prefix_key(source_title, allow_single_letter=False):
        return None
    candidates = body_targets.get(normalized_text(source_title), [])
    if len(candidates) != 1:
        return None
    target = candidates[0]
    title = strip_inline_markdown(target.title)
    if target.context_prefix and not _prefix_key(title, allow_single_letter=False):
        title = f"{target.context_prefix} {title}".strip()
    return title, fallback.group("page")


def _recover_body_aligned_entry_lines(
    values: list[str], lines: list[str], section: NavSection
) -> list[str]:
    """Repair only otherwise-unparseable rows from the body heading index."""
    if section.kind != "contents":
        return values
    body_targets = _body_heading_targets_for_section(lines, section)
    recovered: list[str] = []
    for value in values:
        if not value:
            recovered.append(value)
            continue
        aligned = _body_aligned_source_entry(value, section.kind, body_targets)
        parsed = split_title_page(value, section.kind)
        should_restore_context = bool(
            aligned
            and parsed
            and parsed[1]
            and not _prefix_key(parsed[0], allow_single_letter=False)
            and _prefix_key(aligned[0], allow_single_letter=False)
        )
        if parsed is None or should_restore_context:
            recovered.append(f"{aligned[0]} {aligned[1]}" if aligned else value)
        else:
            recovered.append(value)
    return recovered


def _native_source_candidate(value: str, kind: NavKind) -> tuple[str, str] | None:
    cleaned = _clean_entry_line(value)
    parsed = split_title_page(cleaned, kind)
    if parsed is not None and parsed[1]:
        return parsed
    fallback = RAW_TRAILING_PAGE_RE.fullmatch(cleaned)
    if fallback is None:
        return None
    title = re.sub(r"(?:\s*[.．·•…⋯]\s*){2,}", " ", fallback.group("title"))
    return re.sub(r"\s+", " ", title).strip(), fallback.group("page")


def _native_source_match_end(
    lines: list[str],
    start: int,
    limit: int,
    section: NavSection,
    ignored: set[int],
) -> int | None:
    native_title_pages = {
        (normalized_text(entry.title), entry.page.casefold())
        for entry in section.entries
        if _trusted_source_entry(entry) and entry.page
    }
    native_identifiers = {
        (entry.kind, entry_identifier(entry.title), entry.page.casefold())
        for entry in section.entries
        if _trusted_source_entry(entry) and entry_identifier(entry.title) and entry.page
    }
    joined = ""
    for index in range(start, min(start + 3, limit)):
        if index in ignored or HEADING_RE.fullmatch(lines[index]) or not lines[index].strip():
            break
        if index == start and (
            lines[index].startswith("    ") or lines[index].startswith("\t")
        ):
            break
        part = _clean_entry_line(lines[index])
        joined = f"{joined} {part}".strip()
        if len(joined) > 500 or CAPTION_RE.fullmatch(joined):
            break
        candidate = _native_source_candidate(joined, section.kind)
        if candidate is None:
            continue
        title, page = candidate
        candidate_kind = explicit_entry_kind(title) or section.kind
        identifier = entry_identifier(title)
        if (normalized_text(title), page.casefold()) in native_title_pages or (
            identifier
            and (candidate_kind, identifier, page.casefold()) in native_identifiers
        ):
            return index + 1
    return None


def _extend_native_section_ranges(lines: list[str], sections: list[NavSection]) -> None:
    """Consume only source-list remnants corroborated by native front-matter entries."""
    ignored = _code_line_indexes(lines)
    for position, section in enumerate(sections):
        if not any(_trusted_source_entry(entry) for entry in section.entries):
            continue
        limit = sections[position + 1].start if position + 1 < len(sections) else len(lines)
        cursor = section.end
        claimed_start = cursor
        confirmed = False
        while cursor < limit:
            line = lines[cursor]
            if not line.strip():
                section.end = cursor + 1
                cursor += 1
                continue
            if cursor in ignored or HEADING_RE.fullmatch(line):
                break
            if _is_navigation_debris(line):
                section.end = cursor + 1
                cursor += 1
                continue
            match_end = _native_source_match_end(lines, cursor, limit, section, ignored)
            if match_end is None:
                break
            confirmed = True
            section.end = match_end
            cursor = match_end
        if confirmed:
            section.owned_tail_start = (
                claimed_start
                if section.owned_tail_start < 0
                else min(section.owned_tail_start, claimed_start)
            )


TailRecord = tuple[int, int, str, str]


def _next_heading_index(lines: list[str], start: int, ignored: set[int]) -> int | None:
    for index in range(start, len(lines)):
        if index not in ignored and HEADING_RE.fullmatch(lines[index]) is not None:
            return index
    return None


def _scan_structural_navigation_tail(
    lines: list[str], start: int, limit: int, kind: NavKind, ignored: set[int]
) -> tuple[list[TailRecord], int, bool]:
    """Scan a continuous list-shaped tail without making link decisions."""
    records: list[TailRecord] = []
    boundary = start
    index = start
    stopped_on_text = False
    last_sequence: tuple[int, ...] | None = None
    while index < limit:
        line = lines[index]
        cleaned = _clean_entry_line(line)
        if not cleaned:
            boundary = index + 1
            index += 1
            continue
        if (
            index in ignored
            or HEADING_RE.fullmatch(line) is not None
            or line.startswith(("    ", "\t"))
        ):
            break
        if PAGE_ONLY_RE.fullmatch(cleaned):
            boundary = index + 1
            index += 1
            continue
        if _is_navigation_debris(line):
            recovered = _recover_navigation_debris_title(line, kind)
            if recovered is None:
                stopped_on_text = True
                break
            sequence = _numeric_identifier_sequence(recovered)
            if sequence is not None and last_sequence is not None and sequence <= last_sequence:
                stopped_on_text = True
                break
            if sequence is not None:
                last_sequence = sequence
            records.append((index, index + 1, recovered, ""))
            boundary = index + 1
            index += 1
            continue

        parsed = split_title_page(cleaned, kind)
        if parsed is None or not parsed[1]:
            fallback = RAW_TRAILING_PAGE_RE.fullmatch(cleaned)
            if fallback is not None:
                title = re.sub(
                    r"(?:\s*\.\s*){2,}", " ", fallback.group("title")
                )
                title = re.sub(r"\s+", " ", title).strip(" .")
                parsed = (title, fallback.group("page")) if title else None
        if parsed is not None and parsed[1]:
            sequence = _numeric_identifier_sequence(parsed[0])
            if sequence is not None and last_sequence is not None and sequence <= last_sequence:
                stopped_on_text = True
                break
            if sequence is not None:
                last_sequence = sequence
            records.append((index, index + 1, parsed[0], parsed[1]))
            boundary = index + 1
            index += 1
            continue

        completion = _wrapped_entry_completion(lines, index, limit, kind, ignored)
        if completion is not None:
            completion_end, title = completion
            joined = " ".join(
                _clean_entry_line(lines[item])
                for item in range(index, completion_end + 1)
            )
            joined_entry = split_title_page(joined, kind)
            page = joined_entry[1] if joined_entry is not None else ""
            sequence = _numeric_identifier_sequence(title)
            if sequence is not None and last_sequence is not None and sequence <= last_sequence:
                stopped_on_text = True
                break
            if sequence is not None:
                last_sequence = sequence
            records.append((index, completion_end + 1, title, page))
            boundary = completion_end + 1
            index = completion_end + 1
            continue

        stopped_on_text = True
        break
    return records, boundary, stopped_on_text


_STRICT_LEADER_ROW_RE = re.compile(r"(?:\s*[.．·•…⋯]\s*){3,}")


def _is_strict_inter_navigation_tail(
    lines: list[str],
    records: list[TailRecord],
    *,
    boundary: int,
    limit: int,
    next_navigation: int | None,
    stopped_on_text: bool,
) -> bool:
    """Trust only a complete dot-leader run immediately before another list.

    This covers back-matter rows that a structured extractor may merge into one
    record (for example acknowledgement/profile/authorization rows).  Requiring
    every row to carry an explicit numeric page, a strong leader, monotonic page
    order, and the next navigation heading as the exact boundary keeps ordinary
    body prose outside the owned block.
    """
    if (
        stopped_on_text
        or next_navigation is None
        or limit != next_navigation
        or boundary != limit
        or len(records) < 3
    ):
        return False
    pages: list[int] = []
    for start, end, title, page in records:
        if (
            end != start + 1
            or not title
            or len(title) > 200
            or re.fullmatch(r"[0-9]+", page) is None
            or _STRICT_LEADER_ROW_RE.search(lines[start]) is None
        ):
            return False
        pages.append(int(page))
    return all(left <= right for left, right in zip(pages, pages[1:]))


def _reference_supports_tail_record(record: TailRecord, entries: list[NavEntry]) -> bool:
    _start, _end, title, page = record
    full = normalized_text(title)
    identifier = _prefix_key(title, allow_single_letter=False)
    _prefix, body = _prefix_and_body(title, allow_single_letter=False)
    body_matches: list[NavEntry] = []
    for entry in entries:
        if entry.kind != "contents":
            continue
        entry_full = normalized_text(entry.title)
        entry_identifier_value = _prefix_key(
            entry.title, allow_single_letter=False
        )
        _entry_prefix, entry_body = _prefix_and_body(
            entry.title, allow_single_letter=False
        )
        page_compatible = not page or not entry.page or page == entry.page
        if full and full == entry_full and page_compatible:
            return True
        if identifier and entry_identifier_value == identifier and page_compatible:
            return True
        if body and body == entry_body and _trusted_source_entry(entry) and page == entry.page:
            body_matches.append(entry)
    return len(body_matches) == 1


def _body_supports_tail_record(record: TailRecord, targets: list[Target]) -> bool:
    _start, _end, title, _page = record
    entry = NavEntry(kind="contents", title=title, page="", depth=0)
    if any(_heading_score(entry, target) >= 0.84 for target in targets):
        return True
    normalized = normalized_text(title)
    return any(
        normalized and normalized == normalized_text(target.title)
        for target in targets
    )


def _extend_body_backed_section_ranges(
    lines: list[str], sections: list[NavSection]
) -> None:
    """Atomically own a list-shaped TOC tail when body/reference evidence proves it."""
    ignored = _code_line_indexes(lines)
    for position, section in enumerate(sections):
        if section.kind != "contents" or section.start >= 1000:
            continue
        next_navigation = (
            sections[position + 1].start if position + 1 < len(sections) else None
        )
        next_heading = _next_heading_index(lines, section.end, ignored)
        boundaries = [
            boundary
            for boundary in (next_navigation, next_heading)
            if boundary is not None
        ]
        limit = min(boundaries) if boundaries else None
        if limit is None or limit <= section.end or limit - section.start > 1500:
            continue
        records, boundary, stopped_on_text = _scan_structural_navigation_tail(
            lines, section.end, limit, section.kind, ignored
        )
        if len(records) < 3:
            continue

        strict_inter_navigation_tail = _is_strict_inter_navigation_tail(
            lines,
            records,
            boundary=boundary,
            limit=limit,
            next_navigation=next_navigation,
            stopped_on_text=stopped_on_text,
        )

        probe = NavSection(
            start=section.start,
            end=limit,
            kind=section.kind,
            title=section.title,
            combined=section.combined,
        )
        target_map = _body_heading_targets_for_section(lines, probe)
        targets = list(
            {
                target.line_index: target
                for values in target_map.values()
                for target in values
            }.values()
        )
        support = [
            _reference_supports_tail_record(record, section.entries)
            or _body_supports_tail_record(record, targets)
            for record in records
        ]
        if stopped_on_text:
            while records and not support[-1]:
                records.pop()
                support.pop()
            if records:
                boundary = records[-1][1]
                while boundary < limit and not lines[boundary].strip():
                    boundary += 1
        if len(records) < 3:
            continue
        supported = sum(support)
        required_support = len(records) if len(records) <= 4 else 3
        if (
            not strict_inter_navigation_tail
            and (supported < required_support or supported / len(records) < 0.75)
        ):
            continue
        if not strict_inter_navigation_tail and any(
            not is_supported
            and (
                index == 0
                or index == len(support) - 1
                or not support[index - 1]
                or not support[index + 1]
            )
            for index, is_supported in enumerate(support)
        ):
            continue
        section.owned_tail_start = (
            section.end
            if section.owned_tail_start < 0
            else min(section.owned_tail_start, section.end)
        )
        section.owned_tail_records = records
        section.end = boundary


def _entries_can_overlap(left: NavEntry, right: NavEntry) -> bool:
    if left.kind != right.kind:
        return False
    if left.page and right.page and left.page != right.page:
        return False
    left_full = normalized_text(left.title)
    right_full = normalized_text(right.title)
    if left_full and left_full == right_full:
        return True
    left_key = _prefix_key(left.title, allow_single_letter=False)
    right_key = _prefix_key(right.title, allow_single_letter=False)
    if left_key and right_key:
        return left_key == right_key
    _left_prefix, left_body = _prefix_and_body(
        left.title, allow_single_letter=False
    )
    _right_prefix, right_body = _prefix_and_body(
        right.title, allow_single_letter=False
    )
    return bool(
        left_body
        and left_body == right_body
        and left.page
        and right.page
        and left.page == right.page
    )


def _merge_entry_sequences(
    prefix: list[NavEntry], tail: list[NavEntry]
) -> list[NavEntry]:
    rows = len(prefix)
    columns = len(tail)
    lcs = [[0] * (columns + 1) for _ in range(rows + 1)]
    for row in range(rows):
        for column in range(columns):
            if _entries_can_overlap(prefix[row], tail[column]):
                lcs[row + 1][column + 1] = lcs[row][column] + 1
            else:
                lcs[row + 1][column + 1] = max(
                    lcs[row][column + 1], lcs[row + 1][column]
                )

    matches: list[tuple[int, int]] = []
    row = rows
    column = columns
    while row and column:
        if _entries_can_overlap(prefix[row - 1], tail[column - 1]):
            matches.append((row - 1, column - 1))
            row -= 1
            column -= 1
        elif lcs[row - 1][column] > lcs[row][column - 1]:
            row -= 1
        else:
            column -= 1
    matches.reverse()

    merged: list[NavEntry] = []
    prefix_cursor = 0
    tail_cursor = 0
    for prefix_index, tail_index in matches:
        merged.extend(prefix[prefix_cursor:prefix_index])
        merged.extend(tail[tail_cursor:tail_index])
        left = prefix[prefix_index]
        right = tail[tail_index]
        left_rank = 2 if left.native else (1 if left.structured else 0)
        right_rank = 2 if right.native else (1 if right.structured else 0)
        merged.append(right if right_rank > left_rank else left)
        prefix_cursor = prefix_index + 1
        tail_cursor = tail_index + 1
    merged.extend(prefix[prefix_cursor:])
    merged.extend(tail[tail_cursor:])
    return merged


def _refresh_section_entries(lines: list[str], sections: list[NavSection]) -> None:
    for section in sections:
        if section.owned_tail_start < 0 or not section.owned_tail_records:
            if not any(_trusted_source_entry(entry) for entry in section.entries):
                section.entries = _entries_from_markdown(lines, section)
            continue

        if any(_trusted_source_entry(entry) for entry in section.entries):
            prefix_entries = section.entries
        else:
            prefix = NavSection(
                start=section.start,
                end=section.owned_tail_start,
                kind=section.kind,
                title=section.title,
                combined=section.combined,
            )
            prefix_entries = _entries_from_markdown(lines, prefix)
        target_map = _body_heading_targets_for_section(lines, section)
        tail_entries: list[NavEntry] = []
        for _start, _end, title, page in section.owned_tail_records:
            aligned = _body_aligned_source_entry(
                f"{title} {page}".strip(), section.kind, target_map
            )
            resolved_title = aligned[0] if aligned is not None else title
            entry_kind = explicit_entry_kind(resolved_title) or section.kind
            tail_entries.append(
                NavEntry(
                    kind=entry_kind,
                    title=resolved_title,
                    page=page,
                    depth=_entry_depth(resolved_title, entry_kind),
                )
            )
        section.entries = _merge_entry_sequences(prefix_entries, tail_entries)


def _prefix_and_body(title: str, *, allow_single_letter: bool = True) -> tuple[str, str]:
    cleaned = unicodedata.normalize("NFKC", strip_inline_markdown(title))
    cleaned = re.sub(
        r"^(\s*\d+(?:[.．\-–—]\d+)+)(?=[^\W\d_])",
        r"\1 ",
        cleaned,
        count=1,
    )
    match = NUMBERED_PREFIX_RE.match(cleaned)
    if match is None:
        return "", normalized_text(cleaned)
    matched_prefix = match.group("prefix").strip()
    prefix = matched_prefix.rstrip(".．、)")
    explicit_single = bool(re.fullmatch(r"[A-Z][.．、)]", matched_prefix, flags=re.I))
    if (
        not allow_single_letter
        and re.fullmatch(r"[A-Z]", prefix, flags=re.I)
        and not explicit_single
    ):
        return "", normalized_text(cleaned)
    return normalized_text(match.group("prefix")), normalized_text(cleaned[match.end() :])


def _prefix_key(title: str, *, allow_single_letter: bool = True) -> str:
    cleaned = unicodedata.normalize("NFKC", strip_inline_markdown(title))
    cleaned = re.sub(
        r"^(\s*\d+(?:[.．\-–—]\d+)+)(?=[^\W\d_])",
        r"\1 ",
        cleaned,
        count=1,
    )
    match = NUMBERED_PREFIX_RE.match(cleaned)
    if match is None:
        return ""
    matched_prefix = match.group("prefix").strip()
    raw_prefix = matched_prefix.rstrip(".．、)")
    explicit_single = bool(re.fullmatch(r"[A-Z][.．、)]", matched_prefix, flags=re.I))
    if (
        not allow_single_letter
        and re.fullmatch(r"[A-Z]", raw_prefix, flags=re.I)
        and not explicit_single
    ):
        return ""
    lettered_identifier = re.search(
        r"(?<![A-Z0-9])([A-Z]\s*(?:[.\-\u2013\u2014]\s*\d+)+)\s*$",
        raw_prefix,
        flags=re.I,
    )
    if lettered_identifier:
        return canonical_identifier(re.sub(r"\s+", "", lettered_identifier.group(1)))
    identifier = re.search(r"\d+(?:[.．\-–—]\d+)*", raw_prefix)
    if identifier:
        return canonical_identifier(identifier.group(0))
    letter = re.search(
        r"(?:^|\b(?:chapter|section|part|appendix)\s+|附录\s*)([A-Z])$",
        raw_prefix,
        flags=re.I,
    )
    if letter:
        return letter.group(1).upper()
    return normalized_text(raw_prefix)


def _collect_heading_targets(lines: list[str], sections: list[NavSection]) -> list[Target]:
    targets: list[Target] = []
    previous_heading: Target | None = None
    fenced = _code_line_indexes(lines)
    for index, line in enumerate(lines):
        if index in fenced or _line_in_sections(index, sections):
            continue
        match = HEADING_RE.fullmatch(line)
        if not match or match.group("title").startswith("PDF pages "):
            continue
        target = Target(
            line_index=index,
            kind="heading",
            title=match.group("title").strip(),
            level=len(match.group("marks")),
        )
        if not _prefix_key(target.title, allow_single_letter=False):
            context_index = index - 1
            while context_index >= 0 and not lines[context_index].strip():
                context_index -= 1
            if context_index >= 0 and HEADING_RE.fullmatch(lines[context_index]) is None:
                _context_prefix, context_body = _prefix_and_body(lines[context_index])
                if not context_body:
                    target.context_prefix = _prefix_key(lines[context_index])
            if not target.context_prefix and previous_heading is not None:
                between = lines[previous_heading.line_index + 1 : index]
                _previous_prefix, previous_body = _prefix_and_body(previous_heading.title)
                if not previous_body and all(not value.strip() for value in between):
                    target.context_prefix = _prefix_key(previous_heading.title)
        targets.append(target)
        previous_heading = target
    return targets


def _collect_navigation_targets(lines: list[str], sections: list[NavSection]) -> list[Target]:
    targets: list[Target] = []
    for section in sections:
        heading = HEADING_RE.fullmatch(lines[section.start])
        if heading is None:
            continue
        targets.append(
            Target(
                line_index=section.start,
                kind="navigation",
                title=heading.group("title").strip(),
                level=len(heading.group("marks")),
                anchor=section.anchor,
                navigation_section=section,
            )
        )
    return targets


def _collect_caption_targets(lines: list[str], sections: list[NavSection]) -> list[Target]:
    targets: list[Target] = []
    fenced = _code_line_indexes(lines)
    claimed_caption_lines: set[int] = set()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if index in fenced:
            continue
        if _line_in_sections(index, sections):
            continue
        if line.startswith(("    ", "\t")):
            continue
        if index in claimed_caption_lines:
            continue
        heading = HEADING_RE.fullmatch(line)
        compact = COMPACT_CAPTION_HEADING_RE.fullmatch(line) if heading else None
        if compact is not None:
            caption_index = index + 1
            while caption_index < len(lines) and not lines[caption_index].strip():
                caption_index += 1
            caption = CAPTION_RE.fullmatch(lines[caption_index]) if caption_index < len(lines) else None
            compact_kind = _caption_kind(compact.group("type"))
            if (
                caption is not None
                and compact_kind == _caption_kind(caption.group("type"))
                and canonical_identifier(compact.group("label"))
                == canonical_identifier(caption.group("label"))
            ):
                targets.append(
                    Target(
                        line_index=index,
                        kind=compact_kind,
                        title=caption.group("title").strip(),
                        level=len(heading.group("marks")),
                        label=canonical_identifier(compact.group("label")),
                        caption_type=compact.group("type"),
                        separator=caption.group("separator"),
                        existing_heading=True,
                    )
                )
                claimed_caption_lines.add(caption_index)
                continue
        candidate = heading.group("title") if heading else line
        if not heading and stripped.startswith(("- ", "* ", "+ ", "|", ">")):
            continue
        match = CAPTION_RE.fullmatch(candidate)
        if not match:
            continue
        kind = _caption_kind(match.group("type"))
        targets.append(
            Target(
                line_index=index,
                kind=kind,
                title=match.group("title").strip(),
                level=6,
                label=canonical_identifier(match.group("label")),
                caption_type=match.group("type"),
                separator=match.group("separator"),
                existing_heading=heading is not None,
            )
        )
    return targets


@dataclass(frozen=True, slots=True)
class HeadingTarget:
    """One body heading with canonical values computed exactly once."""

    target: Target = field(repr=False, compare=False)
    position: int
    canonical_full: str
    canonical_prefix: str
    canonical_body: str
    canonical_key: str

    @classmethod
    def build(cls, target: Target, position: int) -> "HeadingTarget":
        prefix, body = _prefix_and_body(
            target.title, allow_single_letter=False
        )
        return cls(
            target=target,
            position=position,
            canonical_full=normalized_text(target.title),
            canonical_prefix=prefix,
            canonical_body=body,
            canonical_key=(
                _prefix_key(target.title, allow_single_letter=False)
                or target.context_prefix
            ),
        )


@dataclass(frozen=True, slots=True)
class _HeadingEntry:
    """Canonical entry values shared by direction and target selection."""

    canonical_full: str
    canonical_prefix: str
    canonical_body: str
    canonical_key: str
    single_prefix: str
    single_body: str
    single_key: str

    @classmethod
    def build(cls, entry: NavEntry) -> "_HeadingEntry":
        prefix, body = _prefix_and_body(
            entry.title, allow_single_letter=False
        )
        single_prefix, single_body = _prefix_and_body(entry.title)
        return cls(
            canonical_full=normalized_text(entry.title),
            canonical_prefix=prefix,
            canonical_body=body,
            canonical_key=_prefix_key(entry.title, allow_single_letter=False),
            single_prefix=single_prefix,
            single_body=single_body,
            single_key=_prefix_key(entry.title),
        )


def _heading_score(
    entry: NavEntry,
    target: Target,
    *,
    entry_features: _HeadingEntry | None = None,
    target_features: HeadingTarget | None = None,
) -> float:
    entry_values = entry_features or _HeadingEntry.build(entry)
    target_values = target_features or HeadingTarget.build(target, -1)
    entry_full = entry_values.canonical_full
    target_full = target_values.canonical_full
    target_prefix = target_values.canonical_prefix
    target_body = target_values.canonical_body
    target_key = target_values.canonical_key
    entry_prefix = entry_values.canonical_prefix
    entry_body = entry_values.canonical_body
    entry_key = entry_values.canonical_key
    if target_key and entry_values.single_key == target_key:
        entry_prefix = entry_values.single_prefix
        entry_body = entry_values.single_body
        entry_key = entry_values.single_key
    if entry_key and target_key and entry_key != target_key:
        return -1.0
    if entry_full == target_full:
        if entry_key != target_key and entry_full not in UNNUMBERED_EXACT:
            return -1.0
        return 1.0
    if entry_key and not target_key:
        return -1.0
    if not entry_key and target_key:
        return -1.0
    if entry_body and entry_body == target_body:
        if entry_prefix and target_prefix:
            return 0.99
        return 0.95
    if (
        entry_key
        and entry_key == target_key
        and not entry_body
        and target_body
        and CONTAINER_ONLY_RE.fullmatch(strip_inline_markdown(entry.title))
    ):
        return 0.98
    if not entry_body or not target_body:
        return -1.0
    ratio = difflib.SequenceMatcher(None, entry_body, target_body, autojunk=False).ratio()
    threshold = 0.84 if entry_prefix and entry_prefix == target_prefix else 0.94
    return ratio if ratio >= threshold else -1.0


@dataclass(slots=True)
class _HeadingCandidateIndex:
    targets: list[Target]
    canonical_targets: dict[int, HeadingTarget]
    by_identifier: dict[str, list[Target]]
    by_full: dict[str, list[Target]]
    positions: dict[int, int]
    entry_features: dict[int, _HeadingEntry]
    candidate_cache: dict[int, list[Target]]
    score_cache: dict[tuple[int, int], float]

    @classmethod
    def build(cls, targets: list[Target]) -> "_HeadingCandidateIndex":
        canonical_targets: dict[int, HeadingTarget] = {}
        by_identifier: dict[str, list[Target]] = {}
        by_full: dict[str, list[Target]] = {}
        positions: dict[int, int] = {}
        for position, target in enumerate(targets):
            identity = id(target)
            canonical = HeadingTarget.build(target, position)
            canonical_targets[identity] = canonical
            if canonical.canonical_key:
                by_identifier.setdefault(canonical.canonical_key, []).append(target)
            if canonical.canonical_full:
                by_full.setdefault(canonical.canonical_full, []).append(target)
            positions[identity] = position
        return cls(
            targets,
            canonical_targets,
            by_identifier,
            by_full,
            positions,
            {},
            {},
            {},
        )

    def features(self, entry: NavEntry) -> _HeadingEntry:
        cache_key = id(entry)
        cached = self.entry_features.get(cache_key)
        if cached is None:
            cached = _HeadingEntry.build(entry)
            self.entry_features[cache_key] = cached
        return cached

    def candidates(self, entry: NavEntry) -> list[Target]:
        cache_key = id(entry)
        cached = self.candidate_cache.get(cache_key)
        if cached is not None:
            return cached
        features = self.features(entry)

        # A numbered identifier is a complete index: _heading_score rejects every
        # target carrying a different (or missing) identifier.  Exact candidates
        # are merged first so lookup remains O(1) even when a context heading
        # supplied the target identifier.
        #
        # An unnumbered entry has no such completeness guarantee.  A near-exact
        # body heading can still pass the fuzzy threshold and can also trigger the
        # 0.08 ambiguity gate, so retain the original full scan in that case.
        if not features.canonical_key:
            self.candidate_cache[cache_key] = self.targets
            return self.targets

        merged: list[Target] = []
        seen: set[int] = set()
        for bucket in (
            self.by_full.get(features.canonical_full, ()),
            self.by_identifier.get(features.canonical_key, ()),
        ):
            for target in bucket:
                identity = id(target)
                if identity in seen:
                    continue
                seen.add(identity)
                merged.append(target)
        merged.sort(key=lambda target: self.positions[id(target)])
        self.candidate_cache[cache_key] = merged
        return merged

    def score(self, entry: NavEntry, target: Target) -> float:
        key = (id(entry), id(target))
        if key not in self.score_cache:
            self.score_cache[key] = _heading_score(
                entry,
                target,
                entry_features=self.features(entry),
                target_features=self.canonical_targets[id(target)],
            )
        return self.score_cache[key]


def _direction_for_section(
    section: NavSection, heading_index: _HeadingCandidateIndex
) -> str:
    before = 0
    after = 0
    for entry in section.entries:
        for target in heading_index.candidates(entry):
            if heading_index.score(entry, target) < 0.95:
                continue
            if target.line_index < section.start:
                before += 1
            elif target.line_index >= section.end:
                after += 1
    return "before" if before > after else "after"


def _is_front_before_entry(entry: NavEntry) -> bool:
    prefix, body = _prefix_and_body(entry.title)
    return normalized_text(entry.title) in FRONT_BEFORE_TOC or (
        not prefix and body in FRONT_BEFORE_TOC
    )


def _choose_heading(
    entry: NavEntry,
    candidates: list[Target],
    cursor: int,
    direction: str,
    section: NavSection,
    heading_index: _HeadingCandidateIndex,
) -> Target | None:
    if direction == "after":
        allowed = [target for target in candidates if target.line_index > cursor]
    else:
        allowed = [target for target in candidates if cursor < target.line_index < section.start]
    ranked = sorted(
        (
            (score, target)
            for target in allowed
            if (score := heading_index.score(entry, target)) >= 0
        ),
        key=lambda item: (-item[0], item[1].line_index),
    )
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
        return None
    return ranked[0][1]


def _match_contents(sections: list[NavSection], targets: list[Target]) -> None:
    used: set[int] = set()
    heading_index = _HeadingCandidateIndex.build(targets)
    for section in (item for item in sections if item.kind == "contents"):
        direction = _direction_for_section(section, heading_index)
        cursor = -1 if direction == "before" else section.end - 1
        for entry in section.entries:
            available = [
                target
                for target in heading_index.candidates(entry)
                if target.line_index not in used
            ]
            front_entry = direction == "after" and _is_front_before_entry(entry)
            if front_entry:
                before = [target for target in available if target.line_index < section.start]
                ranked = sorted(
                    (
                        (score, target)
                        for target in before
                        if (score := heading_index.score(entry, target)) >= 0.95
                    ),
                    key=lambda item: (-item[0], -item[1].line_index),
                )
                target = (
                    ranked[0][1]
                    if ranked
                    and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.08)
                    else None
                )
                if target is None:
                    target = _choose_heading(
                        entry,
                        available,
                        cursor,
                        direction,
                        section,
                        heading_index,
                    )
            else:
                target = _choose_heading(
                    entry,
                    available,
                    cursor,
                    direction,
                    section,
                    heading_index,
                )
            if target is None:
                continue
            entry.target = target
            used.add(target.line_index)
            if not (front_entry and target.line_index < section.start):
                cursor = target.line_index


def _caption_title_score(entry: NavEntry, target: Target) -> float:
    _prefix, entry_body = _prefix_and_body(entry.title)
    target_body = normalized_text(target.title)
    if not entry_body or not target_body:
        return 0.0
    if target_body.startswith(entry_body) or entry_body.startswith(target_body):
        return 1.0
    return difflib.SequenceMatcher(None, entry_body, target_body, autojunk=False).ratio()


def _has_caption_continuation_evidence(
    lines: list[str], candidates: list[Target], identifier: str
) -> bool:
    if len(candidates) < 2:
        return False
    first = min(target.line_index for target in candidates)
    last = max(target.line_index for target in candidates)
    for line in lines[max(0, first - 2) : min(len(lines), last + 3)]:
        value = strip_inline_markdown(line)
        if not re.search(r"\bcontinued\b|续", value, flags=re.I):
            continue
        match = re.match(
            r"^\s*(?:Figure|Fig\.?|Table|图|表)\s*"
            r"(?P<label>(?:\d+|[A-Z])(?:[.．\-–—]\d+)*)",
            value,
            flags=re.I,
        )
        if match and canonical_identifier(match.group("label")) == identifier:
            return True
    return False


def _match_captions(
    lines: list[str], sections: list[NavSection], targets: list[Target]
) -> None:
    used: set[int] = set()
    for section in (item for item in sections if item.kind in {"figures", "tables"}):
        for entry in section.entries:
            identifier = entry_identifier(entry.title)
            if not identifier or (entry.kind != section.kind and not section.combined):
                continue
            candidates = [
                target
                for target in targets
                if target.kind == entry.kind
                and target.label == identifier
                and target.line_index not in used
            ]
            if not candidates:
                continue
            ranked = sorted(
                candidates,
                key=lambda target: (
                    -_caption_title_score(entry, target),
                    0 if target.separator in {":", "："} else 1,
                    target.line_index,
                ),
            )
            if len(ranked) > 1:
                first_score = _caption_title_score(entry, ranked[0])
                second_score = _caption_title_score(entry, ranked[1])
                if first_score < 0.90 or first_score - second_score < 0.08:
                    tied = [
                        target
                        for target in ranked
                        if abs(_caption_title_score(entry, target) - first_score) < 0.01
                    ]
                    same_title = len(
                        {normalized_text(target.title) for target in tied}
                    ) == 1
                    if not (
                        first_score >= 0.90
                        and same_title
                        and _has_caption_continuation_evidence(
                            lines, tied, identifier
                        )
                    ):
                        continue
            target = ranked[0]
            entry.target = target
            used.add(target.line_index)


def _rebuild_corrupt_caption_lists(
    lines: list[str], sections: list[NavSection], targets: list[Target]
) -> None:
    for section in sections:
        if section.kind not in {"figures", "tables"}:
            continue
        debris = any(
            len(lines[index]) > 500
            and re.search(r"(?:\s*[.．·•…⋯]\s*){5,}", lines[index])
            for index in range(section.start + 1, section.end)
        )
        if not debris:
            continue
        if section.entries and any(_trusted_source_entry(entry) for entry in section.entries):
            continue
        section.replace_debris = True
        section.entries = []
        by_label: dict[tuple[str, str], list[Target]] = {}
        for target in targets:
            if (target.kind == section.kind or section.combined) and target.label:
                by_label.setdefault((target.kind, target.label), []).append(target)
        for (target_kind, label), candidates in by_label.items():
            useful = [
                candidate
                for candidate in candidates
                if not normalized_text(candidate.title).startswith("continued")
            ]
            target = (useful or candidates)[0]
            merged_label = re.match(r"^(\d+)\s+", strip_inline_markdown(target.title))
            if merged_label and (target_kind, f"{label}.{merged_label.group(1)}") in by_label:
                continue
            title = re.sub(r"\s+", " ", strip_inline_markdown(target.title)).strip()
            if len(title) > 240:
                shortened = title[:240]
                title = shortened.rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
            section.entries.append(
                NavEntry(
                    kind=target.kind,
                    title=f"{label} {title}".strip(),
                    page="",
                    depth=0,
                )
            )


def _escape_link_text(title: str) -> str:
    return re.sub(r"(?<!\\)([\[\]])", r"\\\1", title)


def _display_entry_title(entry: NavEntry) -> str:
    if entry.target is None:
        return strip_inline_markdown(entry.title)
    if entry.kind in {"figures", "tables"}:
        return strip_inline_markdown(entry.title)
    if entry.target.kind != "heading":
        return strip_inline_markdown(entry.title)
    source = unicodedata.normalize("NFKC", strip_inline_markdown(entry.title))
    source_prefix = NUMBERED_PREFIX_RE.match(source)
    target = strip_inline_markdown(entry.target.title)
    if normalized_text(source) == normalized_text(target):
        return target
    target_prefix = NUMBERED_PREFIX_RE.match(unicodedata.normalize("NFKC", target))
    if source_prefix and not target_prefix:
        return f"{source_prefix.group('prefix').strip()} {target}".strip()
    return target


def _existing_anchor_ids(lines: list[str]) -> set[str]:
    return {
        match.group("id")
        for line in lines
        for match in ANY_ID_RE.finditer(line)
    }


def _assign_section_anchors(sections: list[NavSection], used: set[str]) -> None:
    bases = {"contents": "toc", "figures": "list-of-figures", "tables": "list-of-tables"}
    for section in sections:
        base = "list-of-figures-and-tables" if section.combined else bases[section.kind]
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        section.anchor = candidate
        used.add(candidate)


def _assign_target_anchors(sections: list[NavSection], used: set[str]) -> list[Target]:
    matched: dict[int, Target] = {}
    for section in sections:
        for entry in section.entries:
            if entry.target is None:
                continue
            requested = entry.target
            target = matched.setdefault(requested.line_index, requested)
            entry.target = target
            if all(source.anchor != section.anchor for source in target.sources):
                target.sources.append(section)
            navigation_section = requested.navigation_section or target.navigation_section
            if navigation_section is not None and all(
                source.anchor != section.anchor for source in navigation_section.sources
            ):
                navigation_section.sources.append(section)
    next_anchor = 1
    rendered_targets = [target for target in matched.values() if target.kind != "navigation"]
    for target in sorted(rendered_targets, key=lambda item: (item.line_index, item.kind)):
        while str(next_anchor) in used:
            next_anchor += 1
        target.anchor = str(next_anchor)
        used.add(target.anchor)
        next_anchor += 1
    return rendered_targets


def _preserved_section_blocks(lines: list[str], section: NavSection) -> list[str]:
    """Keep prose notes inside a list block while discarding source list debris."""
    preserved: list[str] = []
    block: list[str] = []

    def flush() -> None:
        nonlocal block
        if not block:
            return
        cleaned = [_clean_entry_line(line) for line in block if line.strip()]
        is_page_noise = cleaned and all(
            re.fullmatch(r"(?:Page|页码|[ivxlcdmIVXLCDM]+|\d{1,4})", value)
            for value in cleaned
        )
        is_navigation = any(navigation_kind(value) is not None for value in cleaned)
        is_rendered_navigation = any(BULLET_RE.fullmatch(line) for line in block)
        is_debris = any(
            len(line) > 500 or re.search(r"(?:\s*[.．·•…⋯]\s*){5,}", line)
            for line in block
        )
        if (
            cleaned
            and not is_page_noise
            and not is_navigation
            and not is_rendered_navigation
            and not is_debris
            and not parse_entry_lines(cleaned, section.kind)
        ):
            if preserved:
                preserved.append("")
            preserved.extend(block)
        block = []

    content_end = (
        section.owned_tail_start
        if section.owned_tail_start >= 0
        else section.end
    )
    for line in lines[section.start + 1 : content_end]:
        if line.strip():
            block.append(line)
        else:
            flush()
    flush()
    return preserved


def _render_section(section: NavSection, lines: list[str]) -> list[str]:
    if section.suppressed:
        return []
    rendered = [
        f'<a id="{section.anchor}" data-pdf2md-nav="section"></a>',
        lines[section.start],
    ]
    for source in section.sources:
        rendered.append(f"[↑ {source.title}](#{source.anchor})")
    rendered.append("")
    notes = _preserved_section_blocks(lines, section)
    if notes:
        rendered.extend(notes)
        rendered.append("")
    for entry in section.entries:
        label = _escape_link_text(_display_entry_title(entry))
        if entry.target and entry.target.anchor:
            label = f"[{label}](#{entry.target.anchor})"
        rendered.append(f"{'  ' * entry.depth}- {label}")
    rendered.append("")
    return rendered


def _render_document(lines: list[str], sections: list[NavSection], targets: list[Target]) -> list[str]:
    section_by_start = {section.start: section for section in sections}
    section_for_line = {
        index: section
        for section in sections
        for index in range(section.start, section.end)
    }
    target_by_line = {target.line_index: target for target in targets}
    output: list[str] = []
    index = 0
    while index < len(lines):
        section = section_by_start.get(index)
        if section is not None:
            output.extend(_render_section(section, lines))
            index = section.end
            continue
        if index in section_for_line:
            index += 1
            continue
        target = target_by_line.get(index)
        if target is not None:
            generated_heading = (
                ' data-pdf2md-heading="generated"'
                if target.kind != "heading" and not target.existing_heading
                else ""
            )
            output.append(
                f'<a id="{target.anchor}" data-pdf2md-nav="target"{generated_heading}></a>'
            )
            if target.kind == "heading":
                output.append(f"{'#' * target.level} {target.title}")
            elif target.existing_heading:
                output.append(lines[index])
            else:
                output.append(f"{'#' * target.level} {_caption_heading_label(target)}")
            for source in target.sources:
                output.append(f"[↑ {source.title}](#{source.anchor})")
            if target.kind != "heading" and not target.existing_heading:
                output.append(lines[index])
            index += 1
            continue
        output.append(lines[index])
        index += 1
    return output


def enhance_document_navigation(
    content: str,
    source: Path | None = None,
    frontmatter_cache: Path | None = None,
    force_frontmatter: bool = False,
    front_regions: Mapping[str, Any] | None = None,
    selected_physical_pages: Collection[int] | None = None,
) -> str:
    """Rebuild front-matter lists and create strict heading-to-list navigation."""
    had_trailing_newline = content.endswith("\n")
    lines = _strip_generated_navigation(content.splitlines())
    structured_navigation = _structured_navigation_entries(
        front_regions,
        selected_physical_pages,
    )
    sections = _section_ranges(
        lines,
        front_regions=front_regions,
        selected_physical_pages=selected_physical_pages,
        structured_navigation=structured_navigation,
    )
    if not sections:
        return content
    _populate_entries(
        lines,
        sections,
        source,
        frontmatter_cache,
        force_frontmatter=force_frontmatter,
        front_regions=front_regions,
        selected_physical_pages=selected_physical_pages,
        structured_navigation=structured_navigation,
    )
    _extend_native_section_ranges(lines, sections)
    _extend_body_backed_section_ranges(lines, sections)
    _refresh_section_entries(lines, sections)
    structured_runs = _structured_navigation_runs(
        front_regions,
        selected_physical_pages,
        structured_navigation,
    )
    _collapse_repeated_structured_sections(
        lines, sections, structured_runs, front_regions
    )
    caption_targets = _collect_caption_targets(lines, sections)
    _rebuild_corrupt_caption_lists(lines, sections, caption_targets)
    sections = [section for section in sections if section.entries or section.replace_debris]
    if not sections:
        return content

    used_anchors = _existing_anchor_ids(lines)
    active_sections = [section for section in sections if not section.suppressed]
    _assign_section_anchors(active_sections, used_anchors)
    heading_targets = _collect_heading_targets(lines, sections)
    heading_targets.extend(_collect_navigation_targets(lines, active_sections))
    _match_contents(active_sections, heading_targets)
    _match_captions(lines, active_sections, caption_targets)

    targets = _assign_target_anchors(active_sections, used_anchors)
    rendered = "\n".join(_render_document(lines, sections, targets)).rstrip()
    return rendered + ("\n" if had_trailing_newline else "")
