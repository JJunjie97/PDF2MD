"""Validated, compact evidence extracted from MinerU content-list-v2 pages.

The private ``_pdf2md.layout`` field is deliberately treated as untrusted
input.  A malformed block is retained as text evidence, but it never becomes
layout-model input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


MAX_TEXT_CHARS = 32_768
MAX_BLOCKS_PER_PAGE = 4_096
MAX_NAVIGATION_BLOCKS_PER_PAGE = 128
MAX_NAVIGATION_LINES_PER_BLOCK = 512
MAX_NAVIGATION_LINE_CHARS = 4_096
MAX_NAVIGATION_CHARS_PER_PAGE = 32_768
FEATURES_VERSION = "front-region-features-1"

_NAVIGATION_CANDIDATE_END = re.compile(
    r"(?:[ivxlcdm]+|[a-z]?\s*-?\d+(?:\s*[-\u2013]\s*\d+)?)\s*$",
    re.I,
)


@dataclass(frozen=True)
class LayoutEvidence:
    label: str
    score: float
    order: int
    bbox: tuple[float, float, float, float]


@dataclass
class BlockEvidence:
    block_type: str
    text: str
    bbox: tuple[float, float, float, float] | None = None
    layout: LayoutEvidence | None = None
    warnings: list[str] = field(default_factory=list)

    def compact(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.block_type}
        if self.text:
            result["text_chars"] = len(self.text)
            result["text_preview"] = self.text[:160]
        if self.bbox is not None:
            result["bbox"] = list(self.bbox)
        if self.layout is not None:
            result["layout"] = {
                "label": self.layout.label,
                "score": self.layout.score,
                "order": self.layout.order,
                "bbox": list(self.layout.bbox),
            }
        if self.warnings:
            result["warnings"] = list(self.warnings)
        return result


@dataclass
class PageEvidence:
    page: int
    blocks: list[BlockEvidence]
    text: str
    navigation_candidate_blocks: list[list[str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid_layout_blocks(self) -> list[LayoutEvidence]:
        return [block.layout for block in self.blocks if block.layout is not None]


@dataclass
class EvidenceDocument:
    pages: list[PageEvidence]
    input_page_count: int
    selected_page_count: int
    sha256: str
    warnings: list[str] = field(default_factory=list)


def extract_region_evidence(
    source: Any, *, start_page: int = 1, max_pages: int = 64
) -> EvidenceDocument:
    """Decode content-list-v2 and validate the selected pages and metadata."""
    warnings: list[str] = []
    if not isinstance(start_page, int) or isinstance(start_page, bool) or start_page < 1:
        warnings.append("invalid_start_page")
        start_page = 1
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 0:
        warnings.append("invalid_max_pages")
        max_pages = 64
    decoded = _decode(source, warnings)
    raw_pages = _page_list(decoded, warnings)
    selected = raw_pages[:max_pages]
    canonical = json.dumps(
        selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    pages = [
        _page_evidence(raw, start_page + offset)
        for offset, raw in enumerate(selected)
    ]
    return EvidenceDocument(
        pages=pages,
        input_page_count=len(raw_pages),
        selected_page_count=len(selected),
        sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings,
    )


def layout_features(page: PageEvidence) -> dict[str, float]:
    """Return bounded page aggregates suitable for a small linear head."""
    result: dict[str, float] = {
        "page.log1p": math.log1p(max(0, page.page)),
        "blocks.log1p": math.log1p(len(page.blocks)),
        "text.log1p_chars": math.log1p(len(page.text)),
    }
    valid = page.valid_layout_blocks
    result["layout.valid_fraction"] = len(valid) / max(1, len(page.blocks))
    if not valid:
        return result
    result["layout.mean_score"] = sum(item.score for item in valid) / len(valid)
    result["layout.min_score"] = min(item.score for item in valid)
    result["layout.max_score"] = max(item.score for item in valid)
    page_boxes = [block.bbox for block in page.blocks if block.bbox is not None]
    page_width = max((max(box[0], box[2]) for box in page_boxes), default=1.0)
    page_height = max((max(box[1], box[3]) for box in page_boxes), default=1.0)
    page_width = max(1.0, page_width)
    page_height = max(1.0, page_height)
    for item in valid:
        key = _feature_token(item.label)
        x0, y0, x1, y1 = item.bbox
        nx0 = min(1.0, max(0.0, x0 / page_width))
        ny0 = min(1.0, max(0.0, y0 / page_height))
        nx1 = min(1.0, max(0.0, x1 / page_width))
        ny1 = min(1.0, max(0.0, y1 / page_height))
        center_x = (nx0 + nx1) / 2.0
        center_y = (ny0 + ny1) / 2.0
        area = max(0.0, nx1 - nx0) * max(0.0, ny1 - ny0)
        row = min(3, max(0, int(center_y * 4.0)))
        column = min(3, max(0, int(center_x * 4.0)))
        result[f"layout.count.{key}"] = result.get(f"layout.count.{key}", 0.0) + 1.0
        result[f"layout.score.{key}"] = result.get(f"layout.score.{key}", 0.0) + item.score
        result[f"layout.area.{key}"] = result.get(f"layout.area.{key}", 0.0) + area
        result[f"layout.center_x.{key}"] = result.get(f"layout.center_x.{key}", 0.0) + center_x
        result[f"layout.center_y.{key}"] = result.get(f"layout.center_y.{key}", 0.0) + center_y
        grid_key = f"layout.grid.{row}.{column}.{key}"
        result[grid_key] = result.get(grid_key, 0.0) + 1.0
    scale = float(len(valid))
    for key in list(result):
        if key.startswith((
            "layout.count.", "layout.score.", "layout.area.",
            "layout.center_x.", "layout.center_y.", "layout.grid.",
        )):
            result[key] /= scale
    return result


def hashed_text_features(page: PageEvidence, dimensions: int = 512) -> dict[str, float]:
    """Deterministic sparse character/word hashing without Python's salted hash."""
    if not isinstance(dimensions, int) or dimensions < 16 or dimensions > 65_536:
        dimensions = 512
    text = page.text.casefold()[:MAX_TEXT_CHARS]
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text)
    compact = re.sub(r"\s+", " ", text).strip()[:8192]
    features: dict[str, float] = {}

    def add(token: str) -> None:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        slot = int.from_bytes(digest, "little") % dimensions
        sign = -1.0 if digest[0] & 1 else 1.0
        key = f"text.hash.{slot}"
        features[key] = features.get(key, 0.0) + sign

    for token in words[:8192]:
        add("w:" + token)
    bounded = "^" + compact + "$"
    for size in range(2, 6):
        for offset in range(max(0, len(bounded) - size + 1)):
            add(f"c{size}:" + bounded[offset:offset + size])
    norm = math.sqrt(sum(value * value for value in features.values())) or 1.0
    for key in list(features):
        features[key] /= norm
    features["text.log1p_chars"] = math.log1p(len(text))
    features["text.token_count_log1p"] = math.log1p(len(words))
    return features


def _decode(source: Any, warnings: list[str]) -> Any:
    if isinstance(source, Path):
        try:
            if source.stat().st_size > 256 * 1024 * 1024:
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


def _page_list(value: Any, warnings: list[str]) -> list[Any]:
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


def _page_evidence(raw: Any, page_number: int) -> PageEvidence:
    warnings: list[str] = []
    if isinstance(raw, dict):
        blocks = raw.get("content_list", raw.get("blocks", raw.get("items", [])))
        blocks = blocks if isinstance(blocks, list) else [raw]
    else:
        blocks = raw if isinstance(raw, list) else []
    if len(blocks) > MAX_BLOCKS_PER_PAGE:
        warnings.append("blocks_truncated")
        blocks = blocks[:MAX_BLOCKS_PER_PAGE]
    raw_blocks = [block for block in blocks if isinstance(block, dict)]
    parsed = [_block_evidence(block) for block in raw_blocks]
    text = "\n".join(block.text for block in parsed if block.text)[:MAX_TEXT_CHARS]
    return PageEvidence(
        page=page_number,
        blocks=parsed,
        text=text,
        navigation_candidate_blocks=_navigation_candidate_blocks(raw_blocks),
        warnings=warnings,
    )


def _navigation_candidate_blocks(
    blocks: list[dict[str, Any]],
) -> list[list[str]]:
    """Keep bounded per-page INDEX/list/text candidates independent of labels.

    The region classifier decides whether these candidates are a contents,
    figure-list, or table-list page. Candidate extraction therefore must not
    inherit the v1 rule classifier's provisional page kind.
    """
    result: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    remaining_chars = MAX_NAVIGATION_CHARS_PER_PAGE
    for block in blocks:
        kind = str(block.get("type", "")).casefold()
        if kind in {"index", "list"}:
            values = _list_item_texts(block)
            if not values:
                value = _block_text(block)
                values = [value] if value else []
            lines = _expanded_candidate_lines(values)
        elif kind in {"paragraph", "text", "abstract", "paragraph_title"}:
            value = _block_text(block)
            lines = _expanded_candidate_lines([value] if value else [])
            if not any(_looks_like_navigation_candidate(line) for line in lines):
                continue
        else:
            continue
        if not lines:
            continue
        bounded_lines: list[str] = []
        for line in lines:
            if remaining_chars <= 0:
                break
            clipped = line[:remaining_chars]
            if clipped:
                bounded_lines.append(clipped)
                remaining_chars -= len(clipped)
        lines = bounded_lines
        if not lines:
            break
        key = tuple(lines)
        if key in seen:
            continue
        seen.add(key)
        result.append(lines)
        if (
            len(result) >= MAX_NAVIGATION_BLOCKS_PER_PAGE
            or remaining_chars <= 0
        ):
            break
    return result


def _list_item_texts(block: dict[str, Any]) -> list[str]:
    content = block.get("content", block)
    if not isinstance(content, dict):
        return []
    items = content.get("list_items", content.get("items", []))
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items[:MAX_NAVIGATION_LINES_PER_BLOCK]:
        value = (
            item.get("item_content", item.get("content", item.get("text", "")))
            if isinstance(item, dict)
            else item
        )
        if text := _text(value):
            result.append(text)
    return result


def _expanded_candidate_lines(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for raw_line in value.splitlines() or [value]:
            line = re.sub(r"[^\S\r\n]+", " ", raw_line).strip()
            if not line:
                continue
            result.append(line[:MAX_NAVIGATION_LINE_CHARS])
            if len(result) >= MAX_NAVIGATION_LINES_PER_BLOCK:
                return result
    return result


def _looks_like_navigation_candidate(value: str) -> bool:
    value = value.strip()
    match = _NAVIGATION_CANDIDATE_END.search(value)
    return bool(
        match
        and 2 <= len(value) <= MAX_NAVIGATION_LINE_CHARS
        and re.search(r"[A-Za-z\u3400-\u9fff]", value[:match.start()])
    )


def _block_evidence(block: dict[str, Any]) -> BlockEvidence:
    warnings: list[str] = []
    block_type = str(block.get("type", "unknown")).casefold()[:80]
    text = _block_text(block)[:MAX_TEXT_CHARS]
    bbox = _valid_bbox(block.get("bbox"))
    if "bbox" in block and bbox is None:
        warnings.append("invalid_bbox")
    private = block.get("_pdf2md")
    layout_raw = private.get("layout") if isinstance(private, dict) else None
    if layout_raw is None:
        layout_raw = block.get("_pdf2md.layout")
    layout = _valid_layout(layout_raw, bbox)
    if layout_raw is not None and layout is None:
        warnings.append("invalid_layout_metadata")
    return BlockEvidence(block_type, text, bbox, layout, warnings)


def _valid_layout(value: Any, fallback_bbox: tuple[float, float, float, float] | None) -> LayoutEvidence | None:
    if not isinstance(value, dict):
        return None
    label = value.get("label")
    score = value.get("score")
    order = value.get("order", value.get("index"))
    bbox = _valid_bbox(value.get("bbox")) or fallback_bbox
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        return None
    if not _finite_number(score) or not 0.0 <= float(score) <= 1.0:
        return None
    if not isinstance(order, int) or isinstance(order, bool) or order < 0 or order > 1_000_000:
        return None
    if bbox is None:
        return None
    return LayoutEvidence(label.strip().casefold(), float(score), order, bbox)


def _valid_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if not all(_finite_number(item) for item in value):
        return None
    x0, y0, x1, y1 = map(float, value)
    if x0 > x1 or y0 > y1 or max(map(abs, (x0, y0, x1, y1))) > 10_000_000:
        return None
    return x0, y0, x1, y1


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _block_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, dict):
        for key in (
            "title_content", "paragraph_content", "page_header_content",
            "page_footer_content", "page_number_content", "aside_content",
            "page_aside_content", "page_aside_text_content", "footnote_content",
            "page_footnote_content", "list_items", "items",
        ):
            if key in content and (value := _text(content[key])):
                return value
    for key in ("text", "content", "value"):
        if key in block and (value := _text(block[key])):
            return value
    return ""


def _text(value: Any) -> str:
    if isinstance(value, str):
        return "\n".join(
            line for line in (re.sub(r"[^\S\r\n]+", " ", item).strip() for item in value.splitlines())
            if line
        )
    if isinstance(value, list):
        return " ".join(filter(None, (_text(item) for item in value))).strip()
    if isinstance(value, dict):
        for key in ("item_content", "content", "text", "value"):
            if key in value and (text := _text(value[key])):
                return text
    return ""


def _feature_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9_]+", "_", value.casefold()).strip("_")
    return token[:80] or "unknown"


__all__ = [
    "BlockEvidence", "EvidenceDocument", "FEATURES_VERSION", "LayoutEvidence", "PageEvidence",
    "extract_region_evidence", "hashed_text_features", "layout_features",
]
