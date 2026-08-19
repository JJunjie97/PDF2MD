from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(slots=True)
class NavEntry:
    kind: NavKind
    title: str
    page: str
    depth: int
    native: bool = False
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


def _section_ranges(lines: list[str]) -> list[NavSection]:
    fenced = _code_line_indexes(lines)
    starts: list[tuple[int, NavKind, str, bool]] = []
    for index, line in enumerate(lines):
        if index in fenced:
            continue
        heading = HEADING_RE.fullmatch(line)
        if not heading:
            continue
        kind = navigation_kind(heading.group("title"))
        if kind is not None:
            title = heading.group("title").strip()
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

    if (
        rendered_bullets
        and any(bullet.group("link_title") for bullet in rendered_bullets)
        and not has_raw_content
    ):
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


def _populate_entries(
    lines: list[str],
    sections: list[NavSection],
    source: Path | None,
    frontmatter_cache: Path | None,
    force_frontmatter: bool = False,
) -> None:
    native = (
        extract_front_matter(
            source,
            cache_path=frontmatter_cache,
            force=force_frontmatter,
        )
        if source is not None and source.is_file()
        else {}
    )
    used_native: set[NavKind] = set()
    for section in sections:
        parsed = _entries_from_markdown(lines, section)
        native_section = native.get(section.kind)
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


RAW_TRAILING_PAGE_RE = re.compile(
    rf"^(?P<title>.+?\S)\s+(?P<page>{PAGE_LABEL})\s*$",
    re.IGNORECASE,
)


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
        if entry.native and entry.page
    }
    native_identifiers = {
        (entry.kind, entry_identifier(entry.title), entry.page.casefold())
        for entry in section.entries
        if entry.native and entry_identifier(entry.title) and entry.page
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
        if not any(entry.native for entry in section.entries):
            continue
        limit = sections[position + 1].start if position + 1 < len(sections) else len(lines)
        cursor = section.end
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
            section.end = match_end
            cursor = match_end


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


def _heading_score(entry: NavEntry, target: Target) -> float:
    entry_full = normalized_text(entry.title)
    target_full = normalized_text(target.title)
    entry_prefix, entry_body = _prefix_and_body(entry.title)
    target_prefix, target_body = _prefix_and_body(target.title, allow_single_letter=False)
    entry_key = _prefix_key(entry.title)
    target_key = _prefix_key(target.title, allow_single_letter=False) or target.context_prefix
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


def _direction_for_section(section: NavSection, targets: list[Target]) -> str:
    before = 0
    after = 0
    for entry in section.entries:
        for target in targets:
            if _heading_score(entry, target) < 0.95:
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
) -> Target | None:
    if direction == "after":
        allowed = [target for target in candidates if target.line_index > cursor]
    else:
        allowed = [target for target in candidates if cursor < target.line_index < section.start]
    ranked = sorted(
        ((score, target) for target in allowed if (score := _heading_score(entry, target)) >= 0),
        key=lambda item: (-item[0], item[1].line_index),
    )
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
        return None
    return ranked[0][1]


def _match_contents(sections: list[NavSection], targets: list[Target]) -> None:
    used: set[int] = set()
    for section in (item for item in sections if item.kind == "contents"):
        direction = _direction_for_section(section, targets)
        cursor = -1 if direction == "before" else section.end - 1
        for entry in section.entries:
            available = [target for target in targets if target.line_index not in used]
            front_entry = direction == "after" and _is_front_before_entry(entry)
            if front_entry:
                before = [target for target in available if target.line_index < section.start]
                ranked = sorted(
                    ((score, target) for target in before if (score := _heading_score(entry, target)) >= 0.95),
                    key=lambda item: (-item[0], -item[1].line_index),
                )
                target = (
                    ranked[0][1]
                    if ranked
                    and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.08)
                    else None
                )
                if target is None:
                    target = _choose_heading(entry, available, cursor, direction, section)
            else:
                target = _choose_heading(entry, available, cursor, direction, section)
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
        if section.entries and any(entry.native for entry in section.entries):
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
        is_debris = any(
            len(line) > 500 or re.search(r"(?:\s*[.．·•…⋯]\s*){5,}", line)
            for line in block
        )
        if (
            cleaned
            and not is_page_noise
            and not is_navigation
            and not is_debris
            and not parse_entry_lines(cleaned, section.kind)
        ):
            if preserved:
                preserved.append("")
            preserved.extend(block)
        block = []

    for line in lines[section.start + 1 : section.end]:
        if line.strip():
            block.append(line)
        else:
            flush()
    flush()
    return preserved


def _render_section(section: NavSection, lines: list[str]) -> list[str]:
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
) -> str:
    """Rebuild front-matter lists and create strict heading-to-list navigation."""
    had_trailing_newline = content.endswith("\n")
    lines = _strip_generated_navigation(content.splitlines())
    sections = _section_ranges(lines)
    if not sections:
        return content
    _populate_entries(
        lines,
        sections,
        source,
        frontmatter_cache,
        force_frontmatter=force_frontmatter,
    )
    _extend_native_section_ranges(lines, sections)
    caption_targets = _collect_caption_targets(lines, sections)
    _rebuild_corrupt_caption_lists(lines, sections, caption_targets)
    sections = [section for section in sections if section.entries or section.replace_debris]
    if not sections:
        return content

    used_anchors = _existing_anchor_ids(lines)
    _assign_section_anchors(sections, used_anchors)
    heading_targets = _collect_heading_targets(lines, sections)
    heading_targets.extend(_collect_navigation_targets(lines, sections))
    _match_contents(sections, heading_targets)
    _match_captions(lines, sections, caption_targets)

    targets = _assign_target_anchors(sections, used_anchors)
    rendered = "\n".join(_render_document(lines, sections, targets)).rstrip()
    return rendered + ("\n" if had_trailing_newline else "")
