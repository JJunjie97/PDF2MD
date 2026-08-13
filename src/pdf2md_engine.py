from __future__ import annotations

import difflib
import io
import threading
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any


_PAGE_INDEX_ATTR = "_pdf2md_page_index"
_READER_LOCK = threading.Lock()
_READER_CACHE: tuple[object, Any] | None = None


def _font_key(name: Any) -> str:
    value = str(name or "").lstrip("/")
    if "+" in value:
        value = value.split("+", 1)[1]
    for suffix in ("-Identity-H", "-Identity-V"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value


def _normalized_character(character: str) -> str:
    normalized = unicodedata.normalize("NFKC", character)
    return normalized if len(normalized) == 1 else character


def _reader_for(source: Any) -> Any:
    """Reuse one in-memory PyPDF reader without keeping uploaded files open."""
    global _READER_CACHE

    if isinstance(source, (bytes, bytearray, memoryview)):
        raw = bytes(source) if not isinstance(source, bytes) else source
        key: object = ("bytes", id(source), len(raw))
    else:
        path = Path(source)
        stat = path.stat()
        key = ("path", str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        raw = path.read_bytes()

    with _READER_LOCK:
        if _READER_CACHE is not None and _READER_CACHE[0] == key:
            return _READER_CACHE[1]

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw), strict=False)
        _READER_CACHE = (key, reader)
        return reader


def _alternate_text_by_font(source: Any, page_index: int) -> dict[str, str]:
    text_parts: dict[str, list[str]] = {}

    def collect(text: str, _cm: Any, _tm: Any, font: Any, _size: Any) -> None:
        if not text or not font:
            return
        key = _font_key(font.get("/BaseFont"))
        if key:
            text_parts.setdefault(key, []).append(text)

    page = _reader_for(source).pages[page_index]
    page.extract_text(visitor_text=collect)
    return {key: "".join(parts) for key, parts in text_parts.items()}


def _repair_font_characters(
    chars: list[dict[str, Any]],
    alternate_text_by_font: dict[str, str],
) -> int:
    """Safely fill PDFium U+FFFD slots from a second font-aware text map."""
    repaired = 0
    affected_fonts = {
        _font_key(char.get("font", {}).get("name"))
        for char in chars
        if "\ufffd" in char.get("char", "")
    }

    for font_key in affected_fonts:
        alternate_text = alternate_text_by_font.get(font_key)
        if not font_key or not alternate_text:
            continue

        native = [
            (_normalized_character(char.get("char", "")), index)
            for index, char in enumerate(chars)
            if _font_key(char.get("font", {}).get("name")) == font_key
            and char.get("char", "")
            and not char.get("char", "").isspace()
        ]
        alternate = [
            (_normalized_character(character), character)
            for character in alternate_text
            if not character.isspace()
        ]
        matcher = difflib.SequenceMatcher(
            None,
            [item[0] for item in native],
            [item[0] for item in alternate],
            autojunk=False,
        )

        for tag, native_start, native_end, alt_start, alt_end in matcher.get_opcodes():
            if tag != "replace" or native_end - native_start != alt_end - alt_start:
                continue
            pairs = list(
                zip(native[native_start:native_end], alternate[alt_start:alt_end])
            )
            if not all(
                native_char == "\ufffd" or native_char == alternate_char
                for (native_char, _index), (alternate_char, _original) in pairs
            ):
                continue
            for (native_char, index), (alternate_char, _original) in pairs:
                if native_char != "\ufffd" or alternate_char == "\ufffd":
                    continue
                chars[index]["char"] = alternate_char
                repaired += 1

    return repaired


def _tag_backend_pages() -> None:
    """Expose the physical page index to the shared span preprocessor."""
    from mineru.backend.hybrid import hybrid_model_output_to_middle_json as hybrid
    from mineru.backend.pipeline import model_json_to_middle_json as pipeline

    if not getattr(hybrid, "_pdf2md_page_tag_patch", False):
        original_hybrid = hybrid.blocks_to_page_info

        def tagged_hybrid(
            page_model_list: Any,
            image_dict: Any,
            page: Any,
            image_writer: Any,
            page_index: int,
            _ocr_enable: bool,
        ) -> Any:
            setattr(page, _PAGE_INDEX_ATTR, page_index)
            return original_hybrid(
                page_model_list,
                image_dict,
                page,
                image_writer,
                page_index,
                _ocr_enable,
            )

        hybrid.blocks_to_page_info = tagged_hybrid
        hybrid._pdf2md_page_tag_patch = True

    if not getattr(pipeline, "_pdf2md_page_tag_patch", False):
        original_pipeline = pipeline.page_model_info_to_page_info

        def tagged_pipeline(
            page_model_info: Any,
            image_dict: Any,
            page: Any,
            image_writer: Any,
            page_index: int,
            ocr_enable: bool = False,
        ) -> Any:
            setattr(page, _PAGE_INDEX_ATTR, page_index)
            return original_pipeline(
                page_model_info,
                image_dict,
                page,
                image_writer,
                page_index,
                ocr_enable,
            )

        pipeline.page_model_info_to_page_info = tagged_pipeline
        pipeline._pdf2md_page_tag_patch = True


def install_span_repair_patch() -> None:
    """Repair text maps first; OCR only unresolved replacement-glyph spans."""
    from mineru.utils import span_pre_proc

    if getattr(span_pre_proc, "_pdf2md_replacement_patch", False):
        return

    _tag_backend_pages()
    original_get_page_chars = span_pre_proc.get_page_chars
    original_signal: Callable[[Any], dict[str, Any]] = (
        span_pre_proc._get_private_use_text_signal
    )
    original_decision: Callable[[dict[str, Any]], bool] = (
        span_pre_proc._should_fallback_to_post_ocr_for_private_use_text
    )

    def get_page_chars_with_fallback(pdf_page: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_get_page_chars(pdf_page, *args, **kwargs)
        chars = result.get("chars", [])
        if not any("\ufffd" in char.get("char", "") for char in chars):
            return result

        page_index = getattr(pdf_page, _PAGE_INDEX_ATTR, None)
        source = getattr(getattr(pdf_page, "pdf", None), "_input", None)
        if page_index is None or source is None:
            return result
        try:
            _repair_font_characters(
                chars,
                _alternate_text_by_font(source, int(page_index)),
            )
        except Exception:
            # MinerU's existing post-OCR path remains the safe fallback.
            return result
        return result

    def signal_with_replacements(chars: Any) -> dict[str, Any]:
        signal = original_signal(chars)
        signal["replacement_count"] = sum(
            char.get("char", "").count("\ufffd") for char in chars
        )
        return signal

    def should_repair_span(signal: dict[str, Any]) -> bool:
        return bool(signal.get("replacement_count", 0)) or original_decision(signal)

    span_pre_proc.get_page_chars = get_page_chars_with_fallback
    span_pre_proc._get_private_use_text_signal = signal_with_replacements
    span_pre_proc._should_fallback_to_post_ocr_for_private_use_text = should_repair_span
    span_pre_proc._pdf2md_replacement_patch = True


def main() -> None:
    install_span_repair_patch()
    from mineru.cli.fast_api import main as api_main

    api_main()


if __name__ == "__main__":
    main()
