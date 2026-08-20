"""Conservative front-page classifier for MinerU content-list-v2 JSON."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

SCHEMA = "pdf2md.front-regions.v1"
RULES_VERSION = "front-region-rules-10"
MAX_INPUT_BYTES = 256 * 1024 * 1024
REGION_KINDS = (
    "cover", "legal", "revision_history", "preface", "abstract",
    "acknowledgements", "contents", "list_of_figures", "list_of_tables",
    "abbreviations", "nomenclature", "body_start", "other_front",
)
_NAV = {"contents", "list_of_figures", "list_of_tables"}
_IGNORED = {"equation_interline", "equation_inline", "image", "chart", "code", "algorithm"}
_FOOTNOTE_KINDS = {"footnote", "page_footnote"}
_ASIDE_KINDS = {"aside", "page_aside", "page_aside_text"}
_WORDS = (
    ("list_of_figures", (
        "list of figures", "table of figures", "list of illustrations", "figure index",
        "list of figures and tables", "list of tables and figures",
        "figures and tables", "tables and figures",
        "\u56fe\u76ee\u5f55", "\u5716\u76ee\u9304", "\u63d2\u56fe\u76ee\u5f55", "\u63d2\u5716\u76ee\u9304", "\u63d2\u56fe\u7d22\u5f15", "\u63d2\u5716\u7d22\u5f15",
        "\u56fe\u8868\u76ee\u5f55", "\u5716\u8868\u76ee\u9304", "\u63d2\u56fe\u4e0e\u8868\u683c\u76ee\u5f55", "\u63d2\u5716\u8207\u8868\u683c\u76ee\u9304",
        "\u63d2\u56fe\u548c\u8868\u683c\u76ee\u5f55", "\u63d2\u5716\u548c\u8868\u683c\u76ee\u9304",
    )),
    ("list_of_tables", (
        "list of tables", "table of tables", "table index",
        "\u8868\u76ee\u5f55", "\u8868\u76ee\u9304",
        "\u8868\u683c\u76ee\u5f55", "\u8868\u683c\u76ee\u9304", "\u8868\u683c\u7d22\u5f15",
    )),
    ("revision_history", ("revision history", "document history", "change history",
                          "change log", "record of revisions", "\u4fee\u8ba2\u8bb0\u5f55", "\u4fee\u8ba2\u5386\u53f2",
                          "\u4fee\u8a02\u8a18\u9304", "\u4fee\u8a02\u6b77\u53f2", "\u7248\u672c\u8bb0\u5f55", "\u7248\u672c\u5386\u53f2",
                          "\u7248\u672c\u8a18\u9304", "\u7248\u672c\u6b77\u53f2", "\u53d8\u66f4\u8bb0\u5f55", "\u8b8a\u66f4\u8a18\u9304", "\u66f4\u6539\u8bb0\u5f55")),
    ("acknowledgements", ("acknowledgements", "acknowledgments", "acknowledgement",
                          "acknowledgment", "\u81f4\u8c22", "\u81f4\u8b1d")),
    ("abbreviations", ("list of abbreviations", "abbreviations", "list of acronyms",
                       "acronyms", "\u7f29\u7565\u8bed", "\u7e2e\u7565\u8a9e", "\u7f29\u7565\u8bcd", "\u7e2e\u7565\u8a5e",
                       "\u7f29\u5199\u8868", "\u7e2e\u5beb\u8868")),
    ("nomenclature", ("nomenclature", "list of symbols", "symbols and notation",
                      "notation", "\u7b26\u53f7\u8bf4\u660e", "\u7b26\u865f\u8aaa\u660e", "\u7b26\u53f7\u8868", "\u7b26\u865f\u8868",
                      "\u4e3b\u8981\u7b26\u53f7", "\u4e3b\u8981\u7b26\u865f", "\u672f\u8bed\u548c\u7b26\u53f7", "\u8853\u8a9e\u548c\u7b26\u865f")),
    ("contents", ("table of contents", "contents", "\u76ee\u5f55", "\u76ee\u9304", "\u76ee\u6b21")),
    ("abstract", (
        "abstract", "summary", "\u6458\u8981", "\u4e2d\u6587\u6458\u8981", "\u5185\u5bb9\u6458\u8981",
    )),
    ("preface", ("preface", "foreword", "prologue", "\u524d\u8a00", "\u5e8f\u8a00", "\u5e8f")),
    ("legal", ("copyright", "legal notice", "disclaimer", "terms of use",
               "\u7248\u6743\u58f0\u660e", "\u7248\u6b0a\u8072\u660e", "\u7248\u6743\u6240\u6709", "\u7248\u6b0a\u6240\u6709",
               "\u6cd5\u5f8b\u58f0\u660e", "\u6cd5\u5f8b\u8072\u660e", "\u514d\u8d23\u58f0\u660e", "\u514d\u8cac\u8072\u660e")),
)
# MinerU emits this exact Harvard-style page heading as a title.  Keep it out
# of the general keyword table: a prose paragraph or generic "Listing" must
# not acquire navigation semantics merely from this narrow alias.
_TITLE_ONLY_HEADINGS = {"listing of figures": "list_of_figures"}
_PAGE_HEADER_NAVIGATION_ALIASES = {
    # These shortened forms occur as running headings on generated list pages,
    # but are far too broad for titles or prose.  They are only used together
    # with the dense terminal-page-column guard below.
    "插图": "list_of_figures",
    "插圖": "list_of_figures",
}
_NAV_END = re.compile(
    r"(?:\.{2,}|\u2026{2,}|\s{2,}|\t)\s*(?:[ivxlcdm]+|[a-z]?\s*-?\d+(?:\s*[-\u2013]\s*\d+)?)\s*$",
    re.I,
)
_TERMINAL_PAGE_REFERENCE = re.compile(
    r"(?:^|\s)(?:[ivxlcdm]+|[a-z]?\s*-?\d+(?:\s*[-\u2013\u2014]\s*\d+)?)\s*$",
    re.I,
)
_MIXED_NAV_ENTRY_START = re.compile(
    r"(?<!\S)(?:"
    r"\d+(?:\.\d+)*|"
    r"[Rr]eferences|[Bb]ibliography|"
    r"[Aa]ppendi(?:x|ces)(?:\s+(?:[A-Z]|\d+))?|"
    r"[Aa]cknowledg(?:e)?ments?|[Ii]ndex|[Gg]lossary"
    r")(?=\s)"
)
_MIXED_NAV_PAGE_END = re.compile(
    r"(?:^|\s)(?:"
    r"[ivxlcdm]{1,8}|"
    r"[a-z]?\s*-?\d{1,3}(?:\s*[-\u2013\u2014]\s*\d{1,3})?"
    r")\s*$",
    re.I,
)
_MIXED_NAV_NUMBERED_START = re.compile(r"\d+(?:\.\d+)*")
_CHINESE_SECTION_TITLE = re.compile(
    r"^([一二三四五六七八九十]{1,3})[、.．]\s*(\S(?:.{0,79})?)$"
)
_BODY = re.compile(
    r"^(?:"
    r"chapter\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"(?:\s*[:\uff1a.\-\u2013\u2014]\s*.*|\s+.*)?|"
    r"part\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"(?:\s*[:\uff1a.\-\u2013\u2014]\s*.*|\s+.*)?|"
    r"1(?:\.0+)?(?:\s+|\s*[.\u3001:\uff1a\-\u2013\u2014]\s*)\S.*|"
    r"introduction|\u7eea\u8bba|\u7dd2\u8ad6|\u5f15\u8a00|"
    r"\u7b2c(?:[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u96f6\u3007]+|\d+|[ivxlcdm]+)\u7ae0(?:\s*[:\uff1a.\-\u2013\u2014]\s*.*|\s+.*|[\u3400-\u9fff].*)?"
    r")$",
    re.I,
)
_NUMBERED_TOP_LEVEL_BODY = re.compile(
    r"^(?:[1-9]\d*)(?!\.\d)(?:\s+|\s*[.:\u3001\uff1a\-\u2013\u2014]\s*)\S.*$",
    re.I,
)
_SPLIT_BODY_MARKER = re.compile(
    r"^(?:"
    r"chapter\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)|"
    r"part\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)|"
    r"\u7b2c(?:[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u96f6\u3007]+|\d+|[ivxlcdm]+)\u7ae0"
    r")$",
    re.I,
)

# These are exact, layout-title matches rather than substring keywords.  They
# are only considered after an accepted front navigation page and a separate
# datasheet signature, so a first-page ``GENERAL DESCRIPTION``/``概述`` cannot
# become the stopping boundary by itself.
_DATASHEET_BODY_HEADINGS = {
    "general description", "specifications", "概述", "技术规格",
}
_DATASHEET_FRONT_PRIMARY_HEADINGS = {"features", "特性"}
_DATASHEET_FRONT_SECONDARY_HEADINGS = {
    "applications", "application", "应用", "接口",
    "general description", "概述",
    "functional block diagram", "functional block diagrams", "功能框图",
}

# A datasheet may spend several physical pages on Features/Applications before
# placing its generated contents page.  Inspect only this fixed front window;
# later navigation-looking pages must never reverse an established body start.
_LEADING_NAVIGATION_WINDOW_PAGES = 8
_LEADING_BODY_MAX_OFFSET = 2
_POST_NAVIGATION_BODY_WINDOW_PAGES = 12


@dataclass
class _Signals:
    page: int
    titles: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    index_items: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    footers: list[str] = field(default_factory=list)
    page_numbers: list[str] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    asides: list[str] = field(default_factory=list)
    navigation_blocks: list[list[str]] = field(default_factory=list)
    structured_blocks: list[list[str]] = field(default_factory=list)
    split_body_heading: bool = False
    short_chinese_manual_body: bool = False
    semantic_blocks: int = 0

    @property
    def texts(self) -> list[str]:
        return self.titles + self.paragraphs + self.index_items

    @property
    def nav_lines(self) -> int:
        return sum(_nav_line(line) for text in self.texts for line in text.splitlines())

    @property
    def structured_nav_lines(self) -> int:
        return sum(
            _nav_line(line)
            for text in self.index_items
            for line in text.splitlines()
        )

    @property
    def text_chars(self) -> int:
        return sum(map(len, self.texts))


def classify_content_list_v2(
    source: Any,
    *,
    start_page: int = 1,
    max_pages: int = 64,
    stop_at_body: bool = True,
) -> dict[str, Any]:
    """Return a compact serializable classification; malformed input never raises."""
    warnings: list[str] = []
    start_page = _positive_int(start_page, 1, "invalid_start_page", warnings)
    max_pages = _nonnegative_int(max_pages, 64, "invalid_max_pages", warnings)
    if not isinstance(stop_at_body, bool):
        warnings.append("invalid_stop_at_body")
        stop_at_body = True
    all_pages = _pages(_decode(source, warnings), warnings)
    raw_pages = all_pages[:max_pages]
    signals = [_extract(page, start_page + offset) for offset, page in enumerate(raw_pages)]
    pages = _classify(signals)
    first_body_offset = next(
        (offset for offset, page in enumerate(pages) if page["kind"] == "body_start"),
        None,
    )
    body_offset = _body_boundary_offset(pages, signals, first_body_offset)
    stopped_at_body = stop_at_body and body_offset is not None
    if stopped_at_body:
        assert body_offset is not None
        pages = pages[:body_offset + 1]
        signals = signals[:body_offset + 1]
    limited_by_max_pages = len(all_pages) > len(raw_pages)
    stop_reason = (
        "body_boundary" if stopped_at_body
        else "page_limit" if limited_by_max_pages
        else "end"
    )
    navigation = {kind: [] for kind in sorted(_NAV)}
    for item, signal in zip(pages, signals):
        if item["kind"] in _NAV and item["confidence"] >= 0.68 and signal.navigation_blocks:
            navigation[item["kind"]].append(
                {"page": item["page"], "blocks": [list(block) for block in signal.navigation_blocks]}
            )
            continue
        mixed_navigation_kind, mixed_navigation_blocks = _mixed_page_navigation(
            signal
        )
        if mixed_navigation_kind is not None:
            navigation[mixed_navigation_kind].append(
                {"page": item["page"], "blocks": mixed_navigation_blocks}
            )
    return {
        "schema": SCHEMA,
        "start_page": start_page,
        "input_page_count": len(all_pages),
        "examined_page_count": len(raw_pages),
        "reported_page_count": len(pages),
        "stop_reason": stop_reason,
        "limited_by_max_pages": limited_by_max_pages,
        "stopped_at_body": stopped_at_body,
        # Compatibility aliases used by the first v1 report consumers.
        "total_pages": len(all_pages),
        "page_count": len(pages),
        "scanned_pages": len(pages),
        "truncated": len(all_pages) > len(pages),
        "body_start_page": next((p["page"] for p in pages if p["kind"] == "body_start"), None),
        "pages": pages,
        "regions": _merge(pages),
        "navigation": {kind: value for kind, value in navigation.items() if value},
        "warnings": warnings,
    }


def _body_boundary_offset(
    pages: list[dict[str, Any]],
    signals: list[_Signals],
    first_body_offset: int | None,
) -> int | None:
    """Find a body boundary, including two tightly bounded datasheet layouts.

    Some vendor documents put ``1 Features`` in the first few physical pages,
    then place a generated contents/index page later in the front section.  A
    leading body candidate is retained as a label but is not the stop boundary
    when high-confidence navigation appears within the fixed front window and
    a second strong body boundary follows shortly afterwards.

    Other datasheets place unnumbered Features/Applications before Contents and
    start their main material with an exact ``GENERAL DESCRIPTION``,
    ``SPECIFICATIONS``, ``概述``, or ``技术规格`` title.  That second pattern is
    enabled only for a physical-page-one scan with a datasheet front signature,
    after high-confidence navigation, and within the same bounded lookahead.
    """
    if not pages or not signals or signals[0].page != 1:
        return first_body_offset

    deferred_leading_body = bool(
        first_body_offset is not None
        and first_body_offset <= _LEADING_BODY_MAX_OFFSET
        and _datasheet_leading_body(signals[first_body_offset])
    )
    lookahead_end = min(len(pages), _LEADING_NAVIGATION_WINDOW_PAGES)
    navigation_start = first_body_offset + 1 if deferred_leading_body else 0
    navigation_offset = next(
        (
            offset
            for offset in range(navigation_start, lookahead_end)
            if _is_explicit_navigation(pages[offset])
        ),
        None,
    )
    if navigation_offset is None:
        return first_body_offset
    if (
        not deferred_leading_body
        and (
            (first_body_offset is not None and first_body_offset <= navigation_offset)
            or not _datasheet_front_signature(signals[:navigation_offset])
        )
    ):
        return first_body_offset

    boundary_search_end = min(
        len(pages), navigation_offset + _POST_NAVIGATION_BODY_WINDOW_PAGES + 1
    )
    for offset in range(navigation_offset + 1, boundary_search_end):
        item = pages[offset]
        if item["kind"] == "body_start":
            if deferred_leading_body:
                assert first_body_offset is not None
                _mark_deferred_leading_bodies(
                    pages, first_body_offset, navigation_offset
                )
            item["evidence"].append("post_navigation_body_boundary")
            return offset
        if item["kind"] in _NAV:
            continue
        confidence = _post_navigation_body_confidence(signals[offset])
        if confidence:
            item["kind"] = "body_start"
            item["confidence"] = confidence
            item["evidence"] = ["post_navigation_body_heading"]
            if _datasheet_body_heading(page=signals[offset]):
                item["evidence"].append("datasheet_exact_heading")
            if deferred_leading_body:
                assert first_body_offset is not None
                _mark_deferred_leading_bodies(
                    pages, first_body_offset, navigation_offset
                )
            return offset
    if deferred_leading_body and boundary_search_end == len(pages):
        # The selected input ended before a second body boundary was available.
        # Keep the explicit navigation already found; the caller can distinguish
        # a complete excerpt from max_pages truncation via stop_reason.
        assert first_body_offset is not None
        _mark_deferred_leading_bodies(
            pages, first_body_offset, navigation_offset
        )
        return None
    return first_body_offset


def _mark_deferred_leading_bodies(
    pages: list[dict[str, Any]], start: int, navigation_offset: int
) -> None:
    for item in pages[start:navigation_offset]:
        if (
            item.get("kind") == "body_start"
            and "front_navigation_lookahead" not in item.get("evidence", [])
        ):
            item["evidence"].append("front_navigation_lookahead")


def _datasheet_leading_body(page: _Signals) -> bool:
    for title_text in page.titles:
        normalized = _norm(title_text)
        if not normalized or _nav_line(title_text):
            continue
        if not _NUMBERED_TOP_LEVEL_BODY.fullmatch(normalized):
            continue
        if re.match(
            r"^1(?:\s+|\s*[.:\u3001\uff1a\-\u2013\u2014]\s*)"
            r"(?:introduction|\u7eea\u8bba|\u7dd2\u8ad6|\u5f15\u8a00)(?:\s|$)",
            normalized,
            re.I,
        ):
            continue
        return True
    return False


def _datasheet_front_signature(pages: list[_Signals]) -> bool:
    """Require two independent exact title signals before navigation."""
    headings = {
        normalized
        for page in pages
        for value in page.titles
        if (normalized := _norm(value))
    }
    return bool(
        headings & _DATASHEET_FRONT_PRIMARY_HEADINGS
        and headings & _DATASHEET_FRONT_SECONDARY_HEADINGS
    )


def _is_explicit_navigation(page: dict[str, Any]) -> bool:
    evidence = page.get("evidence")
    return bool(
        page.get("kind") in _NAV
        and isinstance(page.get("confidence"), (int, float))
        and not isinstance(page.get("confidence"), bool)
        and float(page["confidence"]) >= 0.91
        and isinstance(evidence, list)
        and any(
            value in {
                "explicit_title", "explicit_heading",
                "structured_index_navigation",
            }
            for value in evidence
        )
    )


def _strong_structured_navigation(page: _Signals) -> bool:
    """Treat a dense MinerU ``index`` block as explicit layout evidence.

    A short three-item index remains conservative.  Six or more structured
    entries, with most ending in page references, is specific enough to lock a
    navigation page even when OCR damages or localizes the heading itself.
    """
    item_count = len(page.index_items)
    return (
        item_count >= 6
        and page.structured_nav_lines >= 4
        and page.structured_nav_lines * 5 >= item_count * 3
    )


def _post_navigation_body_confidence(page: _Signals) -> float:
    if _datasheet_body_heading(page):
        return 0.93
    for value in page.titles:
        normalized = _norm(value)
        if normalized and not _nav_line(value) and _NUMBERED_TOP_LEVEL_BODY.fullmatch(normalized):
            return 0.9
    if not page.navigation_blocks:
        for value in page.paragraphs[:1]:
            normalized = _norm(value)
            if (
                normalized
                and not _nav_line(value)
                and _NUMBERED_TOP_LEVEL_BODY.fullmatch(normalized)
            ):
                return 0.74
    return 0.0


def _datasheet_body_heading(page: _Signals) -> bool:
    """Match only an early exact title block, never paragraph prose."""
    if page.navigation_blocks:
        return False
    return any(
        _norm(value) in _DATASHEET_BODY_HEADINGS and not _nav_line(value)
        for value in page.titles[:2]
    )


def _positive_int(
    value: Any, default: int, warning: str, warnings: list[str]
) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    warnings.append(warning)
    return default


def _nonnegative_int(
    value: Any, default: int, warning: str, warnings: list[str]
) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    warnings.append(warning)
    return default


def _decode(source: Any, warnings: list[str]) -> Any:
    if isinstance(source, Path):
        try:
            if source.stat().st_size > MAX_INPUT_BYTES:
                warnings.append("input_too_large")
                return []
            return json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            warnings.append("invalid_json")
            return []
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8-sig")
        except UnicodeError:
            warnings.append("invalid_json")
            return []
    if isinstance(source, str):
        if not source.lstrip().startswith(("[", "{")) and len(source) < 4096:
            try:
                path = Path(source)
                if path.is_file():
                    return _decode(path, warnings)
            except OSError:
                pass
        try:
            return json.loads(source)
        except json.JSONDecodeError:
            warnings.append("invalid_json")
            return []
    return source


def _pages(value: Any, warnings: list[str]) -> list[Any]:
    if isinstance(value, dict):
        value = next(
            (value[key] for key in ("pages", "content_list_v2", "content_list")
             if isinstance(value.get(key), list)),
            value,
        )
    if not isinstance(value, list):
        warnings.append("invalid_root")
        return []
    return value


def _extract(raw: Any, page: int) -> _Signals:
    out = _Signals(page)
    if isinstance(raw, dict):
        blocks = raw.get("content_list", raw.get("blocks", raw.get("items", [])))
        blocks = blocks if isinstance(blocks, list) else [raw]
    else:
        blocks = raw if isinstance(raw, list) else []
    out.split_body_heading = _has_split_body_heading(blocks)
    out.short_chinese_manual_body = _has_short_chinese_manual_body(blocks)
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type", "")).casefold()
        text = _block_text(block, kind)
        if kind == "index":
            items = _list_items(block)
            out.index_items.extend(items or ([text] if text else []))
            if items or text:
                structured = items or [text]
                out.navigation_blocks.append(structured)
                out.structured_blocks.append(structured)
            out.semantic_blocks += max(1, len(items))
        elif kind == "title":
            out.titles.extend([text] if text else [])
            out.semantic_blocks += 1
        elif kind in {"paragraph", "text", "abstract", "doc_title", "paragraph_title", "list"}:
            out.paragraphs.extend([text] if text else [])
            if kind == "list":
                items = _list_items(block)
                if items:
                    out.structured_blocks.append(items)
                candidates = [item for item in items if _nav_line(item)]
            else:
                candidates = _navigation_lines(text)
            if candidates:
                out.navigation_blocks.append(candidates)
            out.semantic_blocks += 1
        elif kind == "page_header":
            out.headers.extend([text] if text else [])
        elif kind == "page_footer":
            out.footers.extend([text] if text else [])
        elif kind == "page_number":
            out.page_numbers.extend([text] if text else [])
        elif kind in _FOOTNOTE_KINDS:
            out.footnotes.extend([text] if text else [])
        elif kind in _ASIDE_KINDS:
            out.asides.extend([text] if text else [])
        elif kind not in _IGNORED and text:
            out.paragraphs.append(text)
            if candidates := _navigation_lines(text):
                out.navigation_blocks.append(candidates)
            out.semantic_blocks += 1
    return out


def _has_short_chinese_manual_body(blocks: list[Any]) -> bool:
    """Recognize a narrow page-one Chinese quick-manual body layout.

    The real WCH guide starts with a document title and then consecutive
    ``一、...`` / ``二、...`` title-plus-prose pairs on physical page one.  Keep
    the ordered block check here rather than inferring from a bag of words: a
    cover, a prose reference to those labels, or a menu-like list must not
    become a body boundary.
    """
    prose_kinds = {"paragraph", "text"}
    semantic_kinds = prose_kinds | {"title", "doc_title", "paragraph_title"}
    semantic: list[tuple[str, str]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type", "")).casefold()
        if kind not in semantic_kinds:
            continue
        text = _block_text(block, kind)
        if text:
            semantic.append((kind, text))

    sections: list[tuple[int, int]] = []
    for offset, (kind, text) in enumerate(semantic):
        if kind != "title":
            continue
        number = _chinese_section_number(text)
        if number is not None:
            sections.append((offset, number))
    if (
        len(sections) < 2
        or sections[0][1] != 1
        or sections[1][1] != 2
        or any(
            current[1] <= previous[1]
            for previous, current in zip(sections, sections[1:])
        )
    ):
        return False

    first_section = sections[0][0]
    if not any(
        offset < first_section and kind in {"title", "doc_title"}
        for offset, (kind, _text_value) in enumerate(semantic)
    ):
        return False

    prose_lengths: list[int] = []
    for offset, _number in sections[:2]:
        if offset + 1 >= len(semantic) or semantic[offset + 1][0] not in prose_kinds:
            return False
        length = len(_norm(semantic[offset + 1][1]))
        if length < 12:
            return False
        prose_lengths.append(length)
    return sum(prose_lengths) >= 50 and max(prose_lengths) >= 30


def _chinese_section_number(text: str) -> int | None:
    value = re.sub(r"[\s\u3000]+", " ", text).strip()
    match = _CHINESE_SECTION_TITLE.fullmatch(value)
    if not match:
        return None
    numeral = match.group(1)
    digits = {
        char: number
        for number, char in enumerate("一二三四五六七八九", start=1)
    }
    if numeral == "十":
        return 10
    if "十" in numeral:
        if numeral.count("十") != 1:
            return None
        tens, ones = numeral.split("十", 1)
        if tens not in {"", *digits} or ones not in {"", *digits}:
            return None
        return (digits.get(tens, 1) * 10) + digits.get(ones, 0)
    return digits.get(numeral)


def _has_split_body_heading(blocks: list[Any]) -> bool:
    """Recognize a chapter marker split from its title by layout conversion.

    Hybrid layout occasionally emits a standalone Chapter 1 marker as a
    paragraph and the chapter name as the following title. This is only a
    strong body boundary when the pair is the first semantic content on the
    page, both blocks have usable geometry in the upper content band, and the
    marker is exact. The lower y bound excludes running headers, while the
    upper bound and adjacency exclude ordinary body references.
    """
    semantic: list[tuple[str, str, tuple[float, float, float, float]]] = []
    page_boxes: list[tuple[float, float, float, float]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        bbox = _valid_block_bbox(block.get("bbox"))
        if bbox is not None:
            page_boxes.append(bbox)
        kind = str(block.get("type", "")).casefold()
        if kind not in {
            "paragraph", "text", "abstract", "doc_title",
            "paragraph_title", "list", "index", "title",
        }:
            continue
        text = _block_text(block, kind)
        if text and bbox is not None:
            semantic.append((kind, text, bbox))

    if len(semantic) < 2:
        return False
    marker_kind, marker_text, marker_box = semantic[0]
    title_kind, title_text, title_box = semantic[1]
    normalized_marker = _norm(marker_text)
    normalized_title = _norm(title_text)
    if (
        marker_kind != "paragraph"
        or title_kind != "title"
        or "\n" in marker_text
        or len(normalized_marker) > 32
        or not _SPLIT_BODY_MARKER.fullmatch(normalized_marker)
        or not (2 <= len(normalized_title) <= 180)
        or _exact_title_keyword(title_text) is not None
        or _nav_line(title_text)
    ):
        return False

    page_height = _page_height(page_boxes)
    marker_y0, marker_y1 = marker_box[1] / page_height, marker_box[3] / page_height
    title_y0, title_y1 = title_box[1] / page_height, title_box[3] / page_height
    return (
        0.10 <= marker_y0 <= 0.34
        and marker_y1 <= 0.40
        and marker_y0 <= title_y0 <= 0.50
        and title_y1 <= 0.62
        and title_y0 - marker_y1 <= 0.18
    )


def _valid_block_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if not all(
        isinstance(item, (int, float))
        and not isinstance(item, bool)
        and float("-inf") < float(item) < float("inf")
        for item in value
    ):
        return None
    x0, y0, x1, y1 = map(float, value)
    if x0 > x1 or y0 > y1 or min(x0, y0) < 0:
        return None
    return x0, y0, x1, y1


def _page_height(boxes: list[tuple[float, float, float, float]]) -> float:
    bottom = max((bbox[3] for bbox in boxes), default=1.0)
    # Hybrid content-list coordinates are normally unit normalized. Preserve
    # that page scale even when the last visible block ends above the footer.
    return 1.0 if bottom <= 1.5 else max(1.0, bottom)


def _block_text(block: dict[str, Any], kind: str) -> str:
    content = block.get("content")
    key = {
        "title": "title_content", "paragraph": "paragraph_content",
        "page_header": "page_header_content", "page_footer": "page_footer_content",
        "page_number": "page_number_content",
        "aside": "aside_content", "page_aside": "page_aside_content",
        "page_aside_text": "page_aside_text_content",
        "footnote": "footnote_content", "page_footnote": "page_footnote_content",
    }.get(kind)
    if isinstance(content, dict) and key in content:
        return _text(content[key])
    if kind in {"index", "list"} and (items := _list_items(block)):
        return "\n".join(items)
    for key in ("text", "content", "value"):
        if key in block and (text := _text(block[key])):
            return text
    return ""


def _list_items(block: dict[str, Any]) -> list[str]:
    content = block.get("content", block)
    items = content.get("list_items", content.get("items", [])) if isinstance(content, dict) else []
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        value = (
            item.get("item_content", item.get("content", item.get("text", "")))
            if isinstance(item, dict) else item
        )
        if text := _text(value):
            result.append(text)
    return result


def _text(value: Any) -> str:
    if isinstance(value, str):
        lines = [re.sub(r"[^\S\r\n]+", " ", line).strip() for line in value.splitlines()]
        return "\n".join(line for line in lines if line)
    if isinstance(value, list):
        return " ".join(filter(None, map(_text, value))).strip()
    if isinstance(value, dict):
        for key in ("content", "text", "value", "item_content"):
            if key in value and (text := _text(value[key])):
                return text
    return ""


def _navigation_lines(text: str) -> list[str]:
    """Keep paragraph-derived TOC evidence separate from ordinary prose."""
    return [line.strip() for line in text.splitlines() if _nav_line(line)]


def _norm(text: str) -> str:
    text = text.casefold().replace("\u81fa", "\u53f0").replace("&", " and ")
    text = re.sub(r"[\s\u3000]+", " ", text).strip()
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    return text.strip(" .:\uff1a\u00b7-\u2014_[]()\uff08\uff09")


def _exact_keyword(text: str) -> str | None:
    value = _norm(text)
    if not value:
        return None
    for kind, words in _WORDS:
        if value in {_norm(word) for word in words}:
            return kind

    # A bilingual heading is accepted only when every component is an exact
    # synonym of the same region.  This keeps "Abstract Algebra" and
    # "\u76ee\u5f55\u7ed3\u6784" out while still accepting "\u76ee\u5f55 / Contents".
    parts = [
        _norm(part)
        for part in re.split(r"(?:[/\uff0f|\uff5c\u00b7\u2022]|\s[-\u2013\u2014]\s|[()\uff08\uff09\[\]\u3010\u3011])", value)
        if _norm(part)
    ]
    if len(parts) >= 2:
        kinds = [_exact_keyword(part) for part in parts]
        if kinds[0] is not None and all(kind == kinds[0] for kind in kinds):
            return kinds[0]

    # OCR frequently drops the separator between Chinese and English titles.
    for kind, words in _WORDS:
        aliases = {_norm(word) for word in words}
        for first in aliases:
            if value.startswith(first):
                remainder = value[len(first):].strip(" /\uff0f|\uff5c\u00b7\u2022-\u2013\u2014")
                if remainder and remainder in aliases:
                    return kind
    return None


def _exact_title_keyword(text: str) -> str | None:
    return _exact_keyword(text) or _TITLE_ONLY_HEADINGS.get(_norm(text))


def _exact_page_header_navigation(text: str) -> str | None:
    value = _norm(text)
    kind = _exact_title_keyword(text) or _PAGE_HEADER_NAVIGATION_ALIASES.get(value)
    return kind if kind in _NAV else None


def _keyword_values(
    values: list[str], confidence: float, reason: str
) -> tuple[str | None, float, str | None]:
    for value in values:
        if kind := _exact_keyword(value):
            return kind, confidence, reason
    return None, 0.0, None


def _title_keyword_values(
    values: list[str], confidence: float, reason: str
) -> tuple[str | None, float, str | None]:
    for value in values:
        if kind := _exact_title_keyword(value):
            return kind, confidence, reason
    return None, 0.0, None


def _body_values(values: list[str], confidence: float) -> tuple[bool, float]:
    for value in values:
        normalized = _norm(value)
        if normalized and not _nav_line(value) and _BODY.fullmatch(normalized):
            return True, confidence
    return False, 0.0


def _nav_line(line: str) -> bool:
    line = line.strip()
    return bool(
        4 <= len(line) <= 180
        and _NAV_END.search(line)
        and re.search(r"[A-Za-z\u3400-\u9fff]", _NAV_END.sub("", line))
    )


def _terminal_page_reference(line: str) -> bool:
    """Accept a structured entry with a final printed-page token.

    Generated lists often use column layout instead of dot leaders, so the
    flattened text contains only one space before the page number.  This looser
    shape is never sufficient by itself; it is consumed only by the exact
    page-header rule and only at high density.
    """
    value = line.strip()
    if not 4 <= len(value) <= 500:
        return False
    match = _TERMINAL_PAGE_REFERENCE.search(value)
    return bool(
        match
        and re.search(r"[A-Za-z\u3400-\u9fff]", value[:match.start()])
    )


def _dense_header_navigation(page: _Signals) -> tuple[str | None, list[str]]:
    kind = next(
        (
            candidate
            for header in page.headers
            if (candidate := _exact_page_header_navigation(header)) is not None
        ),
        None,
    )
    if kind is None:
        return None, []
    items = [
        line.strip()
        for block in page.structured_blocks
        for item in block
        for line in item.splitlines()
        if line.strip()
    ]
    terminal_items = [item for item in items if _terminal_page_reference(item)]
    if (
        len(items) < 6
        or len(terminal_items) < 6
        or len(terminal_items) * 5 < len(items) * 4
    ):
        return None, []
    return kind, terminal_items


def _only_unusable_navigation_debris(page: _Signals) -> bool:
    """Detect a pathological INDEX that contains leaders but no entries.

    Some layout outputs contain one enormous dotted-leader row under a valid
    Contents title. Counting the mere presence of that INDEX as supporting
    evidence used to raise the page from 0.97 to 0.98 and rule-lock it. Only
    suppress that promotion when every non-empty INDEX line is a very long
    leader run and none has a valid title-plus-terminal-page shape.
    """
    lines = [
        line.strip()
        for value in page.index_items
        for line in value.splitlines()
        if line.strip()
    ]
    return bool(lines) and not any(_nav_line(line) for line in lines) and all(
        len(line) > 500
        and bool(re.search(r"(?:\s*[.\uff0e\u00b7\u2022\u2026\u22ef]\s*){5,}", line))
        for line in lines
    )


def _mixed_page_navigation(
    page: _Signals,
) -> tuple[str | None, list[list[str]]]:
    """Return validated navigation embedded in a differently classified page.

    Some proceedings and lecture notes put an Abstract and a generated
    Contents list on the same physical page.  The page's primary region stays
    unchanged; this narrow rule only exports its independently strong
    navigation block.  Exact title layout, a single unambiguous navigation
    kind, and at least two distinct title-plus-page candidates are all
    required.  Prose mentions and damaged leader-only blocks therefore remain
    inert.
    """
    heading_kinds = {
        kind
        for title in page.titles
        if (kind := _exact_title_keyword(title)) in _NAV
    }
    if len(heading_kinds) != 1 or _only_unusable_navigation_debris(page):
        return None, []

    blocks: list[list[str]] = []
    seen: set[str] = set()
    for block in page.navigation_blocks:
        candidates = [
            line.strip()
            for value in block
            for line in value.splitlines()
            if _nav_line(line) and line.strip() not in seen
        ]
        if candidates:
            blocks.append(candidates)
            seen.update(candidates)
    for paragraph in page.paragraphs:
        candidates = [
            candidate
            for candidate in _mixed_paragraph_navigation_entries(paragraph)
            if candidate not in seen
        ]
        if candidates:
            blocks.append(candidates)
            seen.update(candidates)
    if len(seen) < 2:
        return None, []
    return next(iter(heading_kinds)), blocks


def _mixed_paragraph_navigation_entries(text: str) -> list[str]:
    """Split a complete single-space TOC paragraph into strict entries.

    MinerU sometimes flattens several generated TOC rows into one paragraph,
    removing their column gap or leaders. Every emitted row must begin with a
    structural section number or a conventional back-matter label, end in a
    plausible printed-page token, and participate in a segmentation covering
    the whole paragraph. Four-digit years and leading Received/ISO prose
    cannot satisfy this grammar.
    """
    value = re.sub(r"\s+", " ", text).strip()
    if not 4 <= len(value) <= 4000:
        return []
    starts = list(_MIXED_NAV_ENTRY_START.finditer(value))
    if not starts or starts[0].start() != 0:
        return []

    memo: dict[int, list[str] | None] = {}

    def best(index: int) -> list[str] | None:
        if index in memo:
            return memo[index]
        choices: list[list[str]] = []
        for next_index in range(index + 1, len(starts) + 1):
            end = (
                starts[next_index].start()
                if next_index < len(starts)
                else len(value)
            )
            candidate = value[starts[index].start():end].strip()
            if not _valid_mixed_navigation_entry(candidate, starts[index].group()):
                continue
            if next_index == len(starts):
                choices.append([candidate])
                continue
            suffix = best(next_index)
            if suffix is not None:
                choices.append([candidate, *suffix])
        memo[index] = max(choices, key=len) if choices else None
        return memo[index]

    return best(0) or []


def _valid_mixed_navigation_entry(candidate: str, prefix: str) -> bool:
    if not 4 <= len(candidate) <= 500:
        return False
    page_match = _MIXED_NAV_PAGE_END.search(candidate)
    if page_match is None:
        return False
    if _MIXED_NAV_NUMBERED_START.fullmatch(prefix):
        title = candidate[len(prefix):page_match.start()].strip()
        return bool(
            len(title) >= 2
            and re.search(r"[A-Za-z\u3400-\u9fff]", title)
        )
    return True


def _continuation(page: _Signals) -> bool:
    ratio = len(page.index_items) / max(1, page.semantic_blocks)
    navigation_items = sum(map(len, page.navigation_blocks))
    return (
        len(page.index_items) >= 2 and ratio >= 0.35
    ) or (
        navigation_items >= 2
        and navigation_items * 2 >= max(1, page.semantic_blocks)
    )


def _classify(signals: list[_Signals]) -> list[dict[str, Any]]:
    result = []
    prior_nav: str | None = None
    for page in signals:
        title_keyword = _title_keyword_values(page.titles, 0.97, "explicit_title")
        leading_keyword = _keyword_values(page.paragraphs[:3], 0.91, "explicit_heading")
        title_body, title_body_confidence = _body_values(page.titles, 0.9)
        paragraph_body, paragraph_body_confidence = _body_values(page.paragraphs[:2], 0.74)
        header_navigation_kind, header_navigation_items = _dense_header_navigation(page)
        continuation = _continuation(page)
        strong_structured_navigation = _strong_structured_navigation(page)
        unusable_navigation_debris = _only_unusable_navigation_debris(page)
        kind, confidence, reason = title_keyword
        evidence = [reason] if reason else []

        if kind is not None:
            pass
        elif title_body:
            kind, confidence, evidence = "body_start", title_body_confidence, ["body_heading"]
        elif page.split_body_heading:
            kind, confidence, evidence = (
                "body_start", 0.94, ["split_body_heading"]
            )
        elif (
            page.page == 1
            and page.short_chinese_manual_body
            and not page.navigation_blocks
        ):
            kind, confidence, evidence = (
                "body_start", 0.94, ["short_chinese_manual_body"]
            )
        elif header_navigation_kind is not None:
            kind, confidence, evidence = (
                header_navigation_kind,
                0.95,
                ["page_header_navigation", "dense_terminal_page_column"],
            )
            # ``list`` blocks with column-aligned page numbers do not satisfy
            # the dot-leader parser.  Export only the entries that supplied
            # this rule's terminal-page evidence.
            if not page.navigation_blocks:
                page.navigation_blocks.append(header_navigation_items)
        elif strong_structured_navigation:
            # A heading-free dense INDEX immediately after an accepted
            # navigation page is its continuation. In particular, do not
            # collapse multi-page figure/table lists back to generic contents.
            # prior_nav is cleared by every unrelated page below, so this
            # inheritance remains strictly adjacent and cannot cross a body or
            # other front-matter boundary.
            kind = prior_nav or "contents"
            confidence = 0.92
            evidence = ["structured_index_navigation"]
            if prior_nav:
                evidence.insert(0, "navigation_continuation")
        else:
            kind, confidence, reason = leading_keyword
            evidence = [reason] if reason else []

        if kind is not None:
            pass
        elif paragraph_body:
            kind, confidence, evidence = (
                "body_start", paragraph_body_confidence, ["body_heading"]
            )
        elif prior_nav and continuation:
            kind, confidence, evidence = (
                prior_nav, 0.78 if page.index_items else 0.69, ["navigation_continuation"]
            )
        elif continuation:
            kind, confidence, evidence = (
                "contents", 0.68 if page.index_items else 0.58,
                ["structured_index_without_heading"],
            )
        elif page.page == 1 and page.titles and page.semantic_blocks <= 8 and page.text_chars <= 1200:
            kind, confidence, evidence = "cover", 0.68, ["first_page_title_layout"]
        else:
            kind, confidence, evidence = "other_front", 0.35, ["no_strong_signal"]

        if kind in _NAV:
            if unusable_navigation_debris:
                # Keep the heading as a weak rule candidate for the cascade,
                # but neither lock nor export a directory made only of debris.
                confidence = min(confidence, 0.62)
                evidence.append("unusable_navigation_debris")
            elif page.index_items:
                if evidence and evidence[0] in {"explicit_title", "explicit_heading"}:
                    confidence = min(0.99, confidence + 0.01)
                evidence.append("index_blocks")
            elif page.navigation_blocks:
                evidence.append("paragraph_navigation_blocks")

        # Navigation inheritance is deliberately one-page-at-a-time. A mixed
        # Abstract+Contents page keeps its primary kind but may seed exactly
        # one adjacent continuation after passing the same strict export gate.
        # A blank, ordinary page, or mixed page without candidates still severs
        # the chain.
        mixed_navigation_kind, _ = _mixed_page_navigation(page)
        prior_nav = kind if kind in _NAV else mixed_navigation_kind
        if page.page_numbers:
            evidence.append("page_number_block")
        result.append({
            "page": page.page, "kind": kind, "confidence": round(confidence, 2),
            "evidence": evidence,
            "stats": {
                "titles": len(page.titles), "paragraphs": len(page.paragraphs),
                "index_items": len(page.index_items), "headers": len(page.headers),
                "navigation_blocks": len(page.navigation_blocks),
                "footers": len(page.footers), "page_numbers": len(page.page_numbers),
                "footnotes": len(page.footnotes), "asides": len(page.asides),
                "text_chars": page.text_chars,
            },
        })
    return result


def _merge(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    regions = []
    for page in pages:
        if regions and regions[-1]["kind"] == page["kind"] and regions[-1]["end_page"] + 1 == page["page"]:
            region, count = regions[-1], regions[-1]["_count"]
            region["end_page"] = page["page"]
            region["confidence"] = round(
                (region["confidence"] * count + page["confidence"]) / (count + 1), 2
            )
            region["_count"] += 1
        else:
            regions.append({
                "kind": page["kind"], "start_page": page["page"], "end_page": page["page"],
                "confidence": page["confidence"], "_count": 1,
            })
    for region in regions:
        region.pop("_count")
    return regions


__all__ = [
    "REGION_KINDS", "RULES_VERSION", "SCHEMA", "classify_content_list_v2",
]
