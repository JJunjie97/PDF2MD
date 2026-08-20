from __future__ import annotations

import difflib
import io
import math
import threading
import time
import unicodedata
from collections.abc import Callable
from numbers import Integral, Real
from pathlib import Path
from typing import Any


_PAGE_INDEX_ATTR = "_pdf2md_page_index"
_HYBRID_INDEX_ATTR = "_pdf2md_original_index"
_LAYOUT_BLOCK_ATTR = "_pdf2md_layout"
_LAYOUT_OUTPUT_FIELD = "_pdf2md.layout"
_READER_LOCK = threading.Lock()
_READER_CACHE: tuple[object, Any] | None = None
_PRELOAD_LOCK = threading.Lock()


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


def install_hybrid_index_patch() -> None:
    """Expose MinerU layout evidence without changing content-list-v2 fields."""
    import importlib

    from mineru.backend.hybrid import hybrid_magic_model as hybrid
    from mineru.backend.pipeline import pipeline_middle_json_mkcontent as mkcontent
    from mineru.utils.enum_class import BlockType, ContentTypeV2

    try:
        pipeline_magic = importlib.import_module(
            "mineru.backend.pipeline.pipeline_magic_model"
        )
    except ModuleNotFoundError:
        pipeline_magic = None

    try:
        hybrid_analyze = importlib.import_module(
            "mineru.backend.hybrid.hybrid_analyze"
        )
    except ModuleNotFoundError:
        hybrid_analyze = None

    try:
        vlmcontent = importlib.import_module(
            "mineru.backend.vlm.vlm_middle_json_mkcontent"
        )
    except ModuleNotFoundError:
        # Older/minimal Pipeline-only MinerU builds do not ship the VLM
        # content builder. The Pipeline wrapper remains useful and Hybrid
        # startup must not fail merely because the optional module is absent.
        vlmcontent = None

    try:
        vlm_magic = importlib.import_module("mineru.backend.vlm.vlm_magic_model")
    except ModuleNotFoundError:
        vlm_magic = None

    def valid_label(value: Any) -> str | None:
        if not isinstance(value, str):
            value = getattr(value, "value", None)
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    def valid_score(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, Real):
            return None
        score = float(value)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            return None
        return score

    def valid_position(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, Integral):
            return None
        position = int(value)
        return position if position >= 0 else None

    def layout_metadata(
        *sources: Any,
        fallback_label: Any = None,
    ) -> dict[str, Any]:
        mappings = [source for source in sources if isinstance(source, dict)]
        embedded = [
            mapping.get(key)
            for mapping in mappings
            for key in (_LAYOUT_BLOCK_ATTR, _LAYOUT_OUTPUT_FIELD)
            if isinstance(mapping.get(key), dict)
        ]
        candidates = [*embedded, *mappings]
        metadata: dict[str, Any] = {}

        labels = [candidate.get("label") for candidate in candidates]
        labels.append(fallback_label)
        for value in labels:
            label = valid_label(value)
            if label is not None:
                metadata["label"] = label
                break

        for key, validator in (
            ("score", valid_score),
            ("index", valid_position),
            ("order", valid_position),
        ):
            for candidate in candidates:
                value = validator(candidate.get(key))
                if value is not None:
                    metadata[key] = value
                    break
        return metadata

    def preserve_block_layout(
        raw_block_type: Any,
        block_info: dict[str, Any],
        block: dict[str, Any],
    ) -> None:
        metadata = layout_metadata(
            block_info,
            block,
            fallback_label=raw_block_type,
        )
        if metadata:
            block[_LAYOUT_BLOCK_ATTR] = metadata

    def attach_output_layout(para_block: Any, result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        metadata = layout_metadata(result, para_block)
        if metadata:
            result[_LAYOUT_OUTPUT_FIELD] = metadata
        return result

    def convert_index_result(
        para_block: dict[str, Any],
        result: Any,
        merge_text: Callable[[dict[str, Any]], str],
    ) -> Any:
        if not para_block.get(_HYBRID_INDEX_ATTR) or not isinstance(result, dict):
            return result
        if result.get("type") == ContentTypeV2.INDEX:
            return result
        texts: list[str] = []
        for line in para_block.get("lines", []):
            value = merge_text({"type": BlockType.TEXT, "lines": [line]})
            texts.extend(part.strip() for part in value.splitlines() if part.strip())
        if not texts:
            content = result.get("content")
            spans = content.get("paragraph_content") if isinstance(content, dict) else None
            if isinstance(spans, list):
                value = "".join(
                    str(span.get("content", ""))
                    for span in spans
                    if isinstance(span, dict)
                )
                texts.extend(part.strip() for part in value.splitlines() if part.strip())
        converted = {
            "type": ContentTypeV2.INDEX,
            "content": {
                "list_type": ContentTypeV2.LIST_TEXT,
                "list_items": [
                    {
                        "item_type": ContentTypeV2.SPAN_TEXT,
                        "item_content": [
                            {"type": ContentTypeV2.SPAN_TEXT, "content": text}
                        ],
                    }
                    for text in texts
                ],
            },
        }
        if "bbox" in result:
            converted["bbox"] = result["bbox"]
        return converted

    def install_raw_block_patch(module: Any, *, expose_index: bool) -> None:
        if module is None or getattr(module, "_pdf2md_layout_patch", False):
            return
        original_copy = module._copy_raw_text_block_metadata

        def copy_layout_metadata(
            raw_block_type: Any,
            block_info: dict[str, Any],
            block: dict[str, Any],
        ) -> None:
            original_copy(raw_block_type, block_info, block)
            preserve_block_layout(raw_block_type, block_info, block)
            if expose_index and raw_block_type == BlockType.INDEX:
                block[_HYBRID_INDEX_ATTR] = True

        module._copy_raw_text_block_metadata = copy_layout_metadata
        module._pdf2md_index_patch = True
        module._pdf2md_layout_patch = True

    install_raw_block_patch(hybrid, expose_index=True)
    install_raw_block_patch(vlm_magic, expose_index=False)

    if hybrid_analyze is not None and not getattr(
        hybrid_analyze, "_pdf2md_layout_block_patch", False
    ):
        original_build_medium_blocks = (
            hybrid_analyze._build_medium_vlm_layout_blocks
        )

        def build_medium_blocks_with_layout(
            layout_dets: Any,
            page_width: Any,
            page_height: Any,
        ) -> Any:
            blocks = original_build_medium_blocks(
                layout_dets,
                page_width,
                page_height,
            )
            if not isinstance(blocks, list) or not isinstance(layout_dets, list):
                return blocks

            # Hybrid medium converts PP-DocLayoutV2 detections to ContentBlock
            # dictionaries before OCR.  That conversion historically dropped
            # the detector score.  Match by normalized bbox and mapped type so
            # skipped/invalid detections cannot shift metadata onto a neighbour.
            remaining = [item for item in layout_dets if isinstance(item, dict)]
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                block_bbox = block.get("bbox")
                block_type = block.get("type")
                match_index = None
                for index, detection in enumerate(remaining):
                    label = valid_label(detection.get("label"))
                    if label is None:
                        continue
                    try:
                        expected_type = (
                            hybrid_analyze._vlm_type_for_medium_layout_label(label)
                        )
                        expected_bbox = hybrid_analyze._layout_det_bbox_to_unit(
                            detection,
                            page_width,
                            page_height,
                        )
                    except Exception:
                        continue
                    if expected_type == block_type and expected_bbox == block_bbox:
                        match_index = index
                        break
                if match_index is None:
                    continue
                detection = remaining.pop(match_index)
                metadata = layout_metadata(detection)
                if metadata:
                    block[_LAYOUT_BLOCK_ATTR] = metadata
            return blocks

        hybrid_analyze._build_medium_vlm_layout_blocks = (
            build_medium_blocks_with_layout
        )
        hybrid_analyze._pdf2md_layout_block_patch = True

    if pipeline_magic is not None and not getattr(
        pipeline_magic, "_pdf2md_layout_patch", False
    ):
        magic_model = pipeline_magic.MagicModel
        copy_name = "_MagicModel__copy_block_fields"
        original_copy_fields = getattr(magic_model, copy_name)

        def copy_block_fields_with_layout(
            block: dict[str, Any],
            **overrides: Any,
        ) -> dict[str, Any]:
            copied = original_copy_fields(block, **overrides)
            metadata = layout_metadata(block)
            if metadata:
                copied[_LAYOUT_BLOCK_ATTR] = metadata
            return copied

        setattr(magic_model, copy_name, staticmethod(copy_block_fields_with_layout))
        pipeline_magic._pdf2md_layout_patch = True

    def install_content_builder_patch(module: Any) -> None:
        if module is None or getattr(module, "_pdf2md_layout_patch", False):
            return
        original_make_v2 = module.make_blocks_to_content_list_v2

        def make_v2_with_index(
            para_block: dict[str, Any],
            img_bucket_path: str,
            page_size: Any,
        ) -> Any:
            result = convert_index_result(
                para_block,
                original_make_v2(para_block, img_bucket_path, page_size),
                module.merge_para_with_text,
            )
            return attach_output_layout(para_block, result)

        module.make_blocks_to_content_list_v2 = make_v2_with_index
        module._pdf2md_index_patch = True
        module._pdf2md_layout_patch = True

    install_content_builder_patch(mkcontent)
    install_content_builder_patch(vlmcontent)


def _gpu_memory_snapshot() -> dict[str, object]:
    try:
        import torch

        if not torch.cuda.is_available():
            return {"device": "cpu"}
        device_index = torch.cuda.current_device()
        return {
            "device": torch.cuda.get_device_name(device_index),
            "allocated_mb": round(torch.cuda.memory_allocated(device_index) / 1024**2, 1),
            "reserved_mb": round(torch.cuda.memory_reserved(device_index) / 1024**2, 1),
        }
    except Exception:
        return {"device": "unknown"}


def preload_backend(backend: str, method: str, language: str) -> dict[str, object]:
    """Initialize exactly the model singletons used by a later conversion."""
    if backend not in {"pipeline", "hybrid-engine"}:
        raise ValueError("backend must be pipeline or hybrid-engine")
    if method not in {"auto", "txt", "ocr"}:
        raise ValueError("method must be auto, txt, or ocr")
    if not language or len(language) > 32 or not all(
        character.isalnum() or character in "_+-" for character in language
    ):
        raise ValueError("invalid OCR language")

    started = time.monotonic()
    loaded: list[str] = []
    with _PRELOAD_LOCK:
        if backend == "pipeline":
            from mineru.backend.pipeline.pipeline_analyze import ModelSingleton

            # Pipeline inference always asks for this exact singleton key; OCR
            # language selection happens later inside the shared model.
            ModelSingleton().get_model(
                lang=None,
                formula_enable=True,
                table_enable=True,
            )
            loaded.append("pipeline")
        else:
            from mineru.backend.pipeline.model_init import HybridModelSingleton
            from mineru.cli.vlm_preload import preload_vlm_model

            # OCR-only Hybrid skips the small formula recognizer; text/auto
            # uses it. This matches hybrid_analyze's runtime key.
            HybridModelSingleton().get_model(
                lang=None,
                formula_enable=method != "ocr",
            )
            loaded.append("hybrid-layout")
            loaded.append(f"vlm:{preload_vlm_model()}")

    return {
        "ok": True,
        "backend": backend,
        "method": method,
        "language": language,
        "loaded": loaded,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "gpu": _gpu_memory_snapshot(),
    }


def install_preload_route() -> None:
    from fastapi import Body, HTTPException
    from mineru.cli.fast_api import app

    if getattr(app.state, "pdf2md_preload_route", False):
        return

    @app.post("/pdf2md/preload", include_in_schema=False)
    def pdf2md_preload(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        try:
            return preload_backend(
                str(payload.get("backend", "")),
                str(payload.get("method", "")),
                str(payload.get("language", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"model preload failed: {exc}") from exc

    app.state.pdf2md_preload_route = True


def main() -> None:
    install_hybrid_index_patch()
    install_span_repair_patch()
    install_preload_route()
    from mineru.cli.fast_api import main as api_main

    api_main()


if __name__ == "__main__":
    main()
