from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pdf2md_cli as cli  # noqa: E402
import pdf2md_core as core  # noqa: E402
import pdf2md_engine as engine  # noqa: E402


def _result(source: Path, output: Path | None = None) -> core.RunResult:
    root = output or source.with_name(f"{source.stem}.pdf2md")
    return core.RunResult(
        markdown=root / f"{source.stem}.md",
        images=root / "images",
        output=root,
        pages="all",
        profile="balanced",
        cache="miss",
        elapsed_seconds=1.25,
    )


class _FakeOCRService:
    instances: list["_FakeOCRService"] = []
    fail_start = False

    def __init__(self, paths: object, emit: object, cancel_event: threading.Event) -> None:
        self.paths = paths
        self.emit = emit
        self.cancel_event = cancel_event
        self.running = False
        self.start_calls: list[float] = []
        self.preload_calls: list[tuple[str, str, str, float]] = []
        self.stop_calls = 0
        type(self).instances.append(self)

    def start(self, timeout: float = 120.0) -> None:
        self.start_calls.append(timeout)
        if self.fail_start:
            raise core.ConversionError("startup failed")
        self.running = True

    def preload(
        self,
        profile: str,
        method: str,
        language: str,
        timeout: float = 600.0,
    ) -> dict[str, object]:
        self.preload_calls.append((profile, method, language, timeout))
        return {"ok": True, "loaded": [profile], "elapsed_seconds": 0.5}

    def stop(self) -> None:
        self.stop_calls += 1
        self.running = False


class ConversionSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeOCRService.instances.clear()
        _FakeOCRService.fail_start = False

    def test_start_preloads_once_and_reuses_one_service_for_multiple_conversions(self) -> None:
        first = core.ConversionOptions(source=Path("first.pdf"))
        second = core.ConversionOptions(source=Path("second.pdf"))
        expected_first = _result(first.source)
        expected_second = _result(second.source)

        with (
            patch.object(core, "validate_runtime", return_value=object()) as validate,
            patch.object(core, "OCRService", _FakeOCRService),
            patch.object(
                core,
                "run_conversion",
                side_effect=[expected_first, expected_second],
            ) as convert,
        ):
            session = core.ConversionSession(startup_timeout=321)
            self.assertIs(session.start(), session)
            self.assertIs(session.start(), session)
            self.assertIs(session.convert(first), expected_first)
            self.assertIs(session.convert(second), expected_second)
            service = session.service
            session.close()
            session.close()

        self.assertIsNotNone(service)
        assert service is not None
        validate.assert_called_once_with()
        self.assertEqual(len(_FakeOCRService.instances), 1)
        self.assertEqual(service.start_calls, [321])
        self.assertEqual(service.preload_calls, [("balanced", "auto", "ch", 321)])
        self.assertEqual(service.stop_calls, 1)
        self.assertFalse(session.running)
        self.assertEqual(convert.call_count, 2)
        self.assertIs(convert.call_args_list[0].kwargs["ocr_service"], service)
        self.assertIs(convert.call_args_list[1].kwargs["ocr_service"], service)

    def test_preload_can_be_disabled_without_changing_service_reuse(self) -> None:
        options = core.ConversionOptions(source=Path("paper.pdf"))
        with (
            patch.object(core, "validate_runtime", return_value=object()),
            patch.object(core, "OCRService", _FakeOCRService),
            patch.object(core, "run_conversion", return_value=_result(options.source)),
        ):
            with core.ConversionSession(preload_model=False) as session:
                session.convert(options)
                session.convert(options)
                service = session.service

        assert service is not None
        self.assertEqual(service.start_calls, [600.0])
        self.assertEqual(service.preload_calls, [])
        self.assertEqual(service.stop_calls, 1)

    def test_context_exit_releases_service_even_when_conversion_raises(self) -> None:
        options = core.ConversionOptions(source=Path("broken.pdf"))
        with (
            patch.object(core, "validate_runtime", return_value=object()),
            patch.object(core, "OCRService", _FakeOCRService),
            patch.object(core, "run_conversion", side_effect=RuntimeError("broken")),
        ):
            with self.assertRaisesRegex(RuntimeError, "broken"):
                with core.ConversionSession() as session:
                    session.convert(options)

        self.assertEqual(len(_FakeOCRService.instances), 1)
        self.assertEqual(_FakeOCRService.instances[0].stop_calls, 1)
        self.assertIsNone(session.service)

    def test_startup_failure_stops_partial_service(self) -> None:
        _FakeOCRService.fail_start = True
        with (
            patch.object(core, "validate_runtime", return_value=object()),
            patch.object(core, "OCRService", _FakeOCRService),
        ):
            session = core.ConversionSession()
            with self.assertRaisesRegex(core.ConversionError, "startup failed"):
                session.start()

        self.assertEqual(len(_FakeOCRService.instances), 1)
        self.assertEqual(_FakeOCRService.instances[0].stop_calls, 1)
        self.assertIsNone(session.service)

    def test_restart_cleans_stale_service_before_replacement(self) -> None:
        with (
            patch.object(core, "validate_runtime", return_value=object()),
            patch.object(core, "OCRService", _FakeOCRService),
        ):
            session = core.ConversionSession(preload_model=False)
            session.start()
            stale = session.service
            assert stale is not None
            stale.running = False

            session.start()
            replacement = session.service
            session.close()

        self.assertIsNot(stale, replacement)
        self.assertEqual(stale.stop_calls, 1)
        assert replacement is not None
        self.assertEqual(replacement.stop_calls, 1)

    def test_settings_mismatch_is_rejected_before_engine_start(self) -> None:
        options = core.ConversionOptions(source=Path("paper.pdf"), method="ocr")
        with (
            patch.object(core, "validate_runtime") as validate,
            patch.object(core, "OCRService", _FakeOCRService),
        ):
            session = core.ConversionSession(method="auto")
            with self.assertRaisesRegex(core.ConversionError, "必须固定"):
                session.convert(options)

        validate.assert_not_called()
        self.assertEqual(_FakeOCRService.instances, [])


class OCRServicePreloadClientTests(unittest.TestCase):
    def test_preload_posts_backend_matching_profile_and_returns_payload(self) -> None:
        service = object.__new__(core.OCRService)
        service.cancel_event = threading.Event()
        service.process = MagicMock()
        service.process.poll.return_value = None
        service.base_url = "http://127.0.0.1:43210"
        service.emit = MagicMock()
        response = MagicMock()
        response.json.return_value = {
            "ok": True,
            "backend": "hybrid-engine",
            "loaded": ["hybrid-layout", "vlm:test"],
        }
        service.session = MagicMock()
        service.session.post.return_value = response

        payload = service.preload("balanced", "txt", "ch", timeout=42)

        self.assertTrue(payload["ok"])
        service.session.post.assert_called_once_with(
            "http://127.0.0.1:43210/pdf2md/preload",
            json={"backend": "hybrid-engine", "method": "txt", "language": "ch"},
            timeout=(30, 42),
        )
        response.raise_for_status.assert_called_once_with()
        self.assertEqual(
            service.emit.call_args_list,
            [call("message", "加载模型 balanced"), call("message", "模型已加载")],
        )

    def test_preload_rejects_request_before_service_start(self) -> None:
        service = object.__new__(core.OCRService)
        service.cancel_event = threading.Event()
        service.process = None

        with self.assertRaisesRegex(core.ConversionError, "尚未启动"):
            service.preload("fast", "auto", "en")


def _mineru_module_stubs() -> dict[str, types.ModuleType]:
    mineru = types.ModuleType("mineru")
    mineru.__path__ = []  # type: ignore[attr-defined]
    backend = types.ModuleType("mineru.backend")
    backend.__path__ = []  # type: ignore[attr-defined]
    pipeline = types.ModuleType("mineru.backend.pipeline")
    pipeline.__path__ = []  # type: ignore[attr-defined]
    cli_module = types.ModuleType("mineru.cli")
    cli_module.__path__ = []  # type: ignore[attr-defined]
    return {
        "mineru": mineru,
        "mineru.backend": backend,
        "mineru.backend.pipeline": pipeline,
        "mineru.cli": cli_module,
    }


class EnginePreloadTests(unittest.TestCase):
    def test_pipeline_preload_initializes_exact_shared_singleton_key(self) -> None:
        modules = _mineru_module_stubs()
        analyze = types.ModuleType("mineru.backend.pipeline.pipeline_analyze")
        singleton = MagicMock()
        analyze.ModelSingleton = MagicMock(return_value=singleton)  # type: ignore[attr-defined]
        modules[analyze.__name__] = analyze

        with (
            patch.dict(sys.modules, modules),
            patch.object(engine, "_gpu_memory_snapshot", return_value={"device": "mock"}),
        ):
            payload = engine.preload_backend("pipeline", "auto", "en")

        analyze.ModelSingleton.assert_called_once_with()  # type: ignore[attr-defined]
        singleton.get_model.assert_called_once_with(
            lang=None,
            formula_enable=True,
            table_enable=True,
        )
        self.assertEqual(payload["loaded"], ["pipeline"])
        self.assertEqual(payload["gpu"], {"device": "mock"})

    def test_hybrid_preload_matches_formula_key_and_loads_vlm(self) -> None:
        for method, formula_enable in (("auto", True), ("ocr", False)):
            with self.subTest(method=method):
                modules = _mineru_module_stubs()
                model_init = types.ModuleType("mineru.backend.pipeline.model_init")
                singleton = MagicMock()
                model_init.HybridModelSingleton = MagicMock(  # type: ignore[attr-defined]
                    return_value=singleton
                )
                vlm = types.ModuleType("mineru.cli.vlm_preload")
                vlm.preload_vlm_model = MagicMock(return_value="mock-vlm")  # type: ignore[attr-defined]
                modules[model_init.__name__] = model_init
                modules[vlm.__name__] = vlm

                with (
                    patch.dict(sys.modules, modules),
                    patch.object(engine, "_gpu_memory_snapshot", return_value={"device": "mock"}),
                ):
                    payload = engine.preload_backend("hybrid-engine", method, "ch")

                singleton.get_model.assert_called_once_with(
                    lang=None,
                    formula_enable=formula_enable,
                )
                vlm.preload_vlm_model.assert_called_once_with()  # type: ignore[attr-defined]
                self.assertEqual(payload["loaded"], ["hybrid-layout", "vlm:mock-vlm"])

    def test_preload_route_is_installed_once_and_maps_validation_errors(self) -> None:
        modules = _mineru_module_stubs()

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail: str) -> None:
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail

        fastapi = types.ModuleType("fastapi")
        fastapi.Body = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        fastapi.HTTPException = HTTPException  # type: ignore[attr-defined]
        routes: dict[str, object] = {}
        app = SimpleNamespace(state=SimpleNamespace())

        def post(path: str, **_kwargs: object):
            def decorator(function: object) -> object:
                routes[path] = function
                return function

            return decorator

        app.post = post
        fast_api = types.ModuleType("mineru.cli.fast_api")
        fast_api.app = app  # type: ignore[attr-defined]
        modules["fastapi"] = fastapi
        modules[fast_api.__name__] = fast_api

        with patch.dict(sys.modules, modules):
            engine.install_preload_route()
            engine.install_preload_route()

            self.assertEqual(list(routes), ["/pdf2md/preload"])
            route = routes["/pdf2md/preload"]
            assert callable(route)
            with patch.object(engine, "preload_backend", side_effect=ValueError("bad backend")):
                with self.assertRaises(HTTPException) as raised:
                    route({"backend": "bad", "method": "auto", "language": "ch"})

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "bad backend")


class EngineHybridIndexPatchTests(unittest.TestCase):
    def test_real_medium_layout_builder_keeps_detector_score(self) -> None:
        from mineru.backend.hybrid import hybrid_analyze

        engine.install_hybrid_index_patch()
        installed = hybrid_analyze._build_medium_vlm_layout_blocks
        engine.install_hybrid_index_patch()
        self.assertIs(
            hybrid_analyze._build_medium_vlm_layout_blocks,
            installed,
        )

        blocks = installed(
            [
                {
                    "label": "content",
                    "bbox": [100, 200, 500, 600],
                    "score": 0.93,
                    "index": 7,
                    "order": 2,
                },
                {
                    "label": "not-a-layout-label",
                    "bbox": [0, 0, 10, 10],
                    "score": 0.99,
                },
            ],
            1000,
            1000,
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "index")
        self.assertEqual(
            blocks[0][engine._LAYOUT_BLOCK_ATTR],
            {
                "label": "content",
                "score": 0.93,
                "index": 7,
                "order": 2,
            },
        )

    def test_raw_index_marker_converts_only_v2_output_and_install_is_idempotent(
        self,
    ) -> None:
        modules = _mineru_module_stubs()
        hybrid_package = types.ModuleType("mineru.backend.hybrid")
        hybrid_package.__path__ = []  # type: ignore[attr-defined]
        vlm_package = types.ModuleType("mineru.backend.vlm")
        vlm_package.__path__ = []  # type: ignore[attr-defined]
        utils_package = types.ModuleType("mineru.utils")
        utils_package.__path__ = []  # type: ignore[attr-defined]
        hybrid = types.ModuleType("mineru.backend.hybrid.hybrid_magic_model")
        pipeline_magic = types.ModuleType(
            "mineru.backend.pipeline.pipeline_magic_model"
        )
        vlm_magic = types.ModuleType("mineru.backend.vlm.vlm_magic_model")
        mkcontent = types.ModuleType(
            "mineru.backend.pipeline.pipeline_middle_json_mkcontent"
        )
        vlmcontent = types.ModuleType(
            "mineru.backend.vlm.vlm_middle_json_mkcontent"
        )
        enum_class = types.ModuleType("mineru.utils.enum_class")

        class BlockType:
            INDEX = "index"
            TEXT = "text"

        class ContentTypeV2:
            INDEX = "index"
            LIST_TEXT = "text_list"
            SPAN_TEXT = "text"

        copy_calls: list[tuple[object, dict[str, object], dict[str, object]]] = []
        vlm_copy_calls: list[
            tuple[object, dict[str, object], dict[str, object]]
        ] = []
        make_calls: dict[
            str,
            list[tuple[dict[str, object], str, object]],
        ] = {"pipeline": [], "vlm": []}
        merge_calls: dict[str, list[dict[str, object]]] = {
            "pipeline": [],
            "vlm": [],
        }

        def copy_raw_text_block_metadata(
            raw_block_type: object,
            block_info: dict[str, object],
            block: dict[str, object],
        ) -> None:
            copy_calls.append((raw_block_type, block_info, block))
            block["copied"] = block_info["copied"]

        def vlm_copy_raw_text_block_metadata(
            raw_block_type: object,
            block_info: dict[str, object],
            block: dict[str, object],
        ) -> None:
            vlm_copy_calls.append((raw_block_type, block_info, block))

        class MagicModel:
            @staticmethod
            def __copy_block_fields(
                block: dict[str, object],
                **overrides: object,
            ) -> dict[str, object]:
                copied = {
                    key: value
                    for key, value in block.items()
                    if key not in {"cls_id", "label"}
                }
                copied.update(overrides)
                return copied

        ordinary_results = {
            "pipeline": {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [{"type": "text", "content": "pipeline body"}]
                },
            },
            "vlm": {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [{"type": "text", "content": "vlm body"}]
                },
            },
        }

        def make_builder(
            name: str,
            bbox: list[int],
        ) -> object:
            def make_blocks_to_content_list_v2(
                para_block: dict[str, object],
                img_bucket_path: str,
                page_size: object,
            ) -> dict[str, object]:
                make_calls[name].append((para_block, img_bucket_path, page_size))
                if para_block.get("name") == "ordinary":
                    return ordinary_results[name]
                return {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [
                            {"type": "text", "content": "fallback index text"}
                        ]
                    },
                    "bbox": bbox,
                }

            return make_blocks_to_content_list_v2

        def merge_builder(name: str) -> object:
            def merge_para_with_text(para_block: dict[str, object]) -> str:
                merge_calls[name].append(para_block)
                lines = para_block["lines"]
                assert isinstance(lines, list)
                line = lines[0]
                assert isinstance(line, dict)
                return str(line[f"{name}_text"])

            return merge_para_with_text

        hybrid._copy_raw_text_block_metadata = (  # type: ignore[attr-defined]
            copy_raw_text_block_metadata
        )
        vlm_magic._copy_raw_text_block_metadata = (  # type: ignore[attr-defined]
            vlm_copy_raw_text_block_metadata
        )
        pipeline_magic.MagicModel = MagicModel  # type: ignore[attr-defined]
        mkcontent.make_blocks_to_content_list_v2 = (  # type: ignore[attr-defined]
            make_builder("pipeline", [0, 1, 90, 20])
        )
        mkcontent.merge_para_with_text = merge_builder("pipeline")  # type: ignore[attr-defined]
        vlmcontent.make_blocks_to_content_list_v2 = (  # type: ignore[attr-defined]
            make_builder("vlm", [10, 11, 80, 30])
        )
        vlmcontent.merge_para_with_text = merge_builder("vlm")  # type: ignore[attr-defined]
        enum_class.BlockType = BlockType  # type: ignore[attr-defined]
        enum_class.ContentTypeV2 = ContentTypeV2  # type: ignore[attr-defined]
        modules.update(
            {
                hybrid_package.__name__: hybrid_package,
                vlm_package.__name__: vlm_package,
                utils_package.__name__: utils_package,
                hybrid.__name__: hybrid,
                pipeline_magic.__name__: pipeline_magic,
                vlm_magic.__name__: vlm_magic,
                mkcontent.__name__: mkcontent,
                vlmcontent.__name__: vlmcontent,
                enum_class.__name__: enum_class,
            }
        )

        with patch.dict(sys.modules, modules):
            engine.install_hybrid_index_patch()
            installed_copy = hybrid._copy_raw_text_block_metadata  # type: ignore[attr-defined]
            installed_vlm_copy = (  # type: ignore[attr-defined]
                vlm_magic._copy_raw_text_block_metadata
            )
            installed_pipeline_copy = (  # type: ignore[attr-defined]
                pipeline_magic.MagicModel._MagicModel__copy_block_fields
            )
            installed_make_v2 = (  # type: ignore[attr-defined]
                mkcontent.make_blocks_to_content_list_v2
            )
            installed_vlm_make_v2 = (  # type: ignore[attr-defined]
                vlmcontent.make_blocks_to_content_list_v2
            )

            engine.install_hybrid_index_patch()

            self.assertIs(  # type: ignore[attr-defined]
                hybrid._copy_raw_text_block_metadata,
                installed_copy,
            )
            self.assertIs(  # type: ignore[attr-defined]
                vlm_magic._copy_raw_text_block_metadata,
                installed_vlm_copy,
            )
            self.assertIs(  # type: ignore[attr-defined]
                pipeline_magic.MagicModel._MagicModel__copy_block_fields,
                installed_pipeline_copy,
            )
            self.assertIs(  # type: ignore[attr-defined]
                mkcontent.make_blocks_to_content_list_v2,
                installed_make_v2,
            )
            self.assertIs(  # type: ignore[attr-defined]
                vlmcontent.make_blocks_to_content_list_v2,
                installed_vlm_make_v2,
            )

            index_block: dict[str, object] = {
                "type": BlockType.TEXT,
                "name": "index",
                "lines": [
                    {
                        "pipeline_text": "1 Introduction\n  2 Methods  ",
                        "vlm_text": "一、概述\n二、方法",
                    },
                    {
                        "pipeline_text": "\n3 Results\n",
                        "vlm_text": "三、结果",
                    },
                ],
            }
            text_block: dict[str, object] = {
                "type": BlockType.TEXT,
                "name": "ordinary",
            }
            hybrid._copy_raw_text_block_metadata(  # type: ignore[attr-defined]
                BlockType.INDEX,
                {
                    "copied": "index metadata",
                    "label": "content",
                    "score": 0.94,
                    "index": 7,
                    "order": 3,
                },
                index_block,
            )
            hybrid._copy_raw_text_block_metadata(  # type: ignore[attr-defined]
                BlockType.TEXT,
                {
                    "copied": "text metadata",
                    "label": "text",
                    "score": 0.88,
                    "index": 8,
                },
                text_block,
            )

            self.assertTrue(index_block[engine._HYBRID_INDEX_ATTR])
            self.assertNotIn(engine._HYBRID_INDEX_ATTR, text_block)
            self.assertEqual(
                index_block[engine._LAYOUT_BLOCK_ATTR],
                {"label": "content", "score": 0.94, "index": 7, "order": 3},
            )
            self.assertEqual(
                text_block[engine._LAYOUT_BLOCK_ATTR],
                {"label": "text", "score": 0.88, "index": 8},
            )

            pipeline_block = (
                pipeline_magic.MagicModel._MagicModel__copy_block_fields(  # type: ignore[attr-defined]
                    {
                        "label": "paragraph_title",
                        "score": 0.91,
                        "index": 4,
                        "order": 2,
                        "cls_id": 9,
                    },
                    type="title",
                )
            )
            self.assertNotIn("label", pipeline_block)
            self.assertNotIn("cls_id", pipeline_block)
            self.assertEqual(
                pipeline_block[engine._LAYOUT_BLOCK_ATTR],
                {
                    "label": "paragraph_title",
                    "score": 0.91,
                    "index": 4,
                    "order": 2,
                },
            )
            invalid_pipeline_block = (
                pipeline_magic.MagicModel._MagicModel__copy_block_fields(  # type: ignore[attr-defined]
                    {
                        "label": "   ",
                        "score": float("nan"),
                        "index": -1,
                        "order": True,
                    },
                    type="text",
                )
            )
            self.assertNotIn(engine._LAYOUT_BLOCK_ATTR, invalid_pipeline_block)

            vlm_block: dict[str, object] = {"index": 5}
            vlm_magic._copy_raw_text_block_metadata(  # type: ignore[attr-defined]
                BlockType.TEXT,
                {"type": "text", "score": 0.73, "order": 6},
                vlm_block,
            )
            self.assertEqual(
                vlm_block[engine._LAYOUT_BLOCK_ATTR],
                {"label": "text", "score": 0.73, "index": 5, "order": 6},
            )

            pipeline_index = mkcontent.make_blocks_to_content_list_v2(  # type: ignore[attr-defined]
                index_block,
                "images",
                (100, 200),
            )
            vlm_index = vlmcontent.make_blocks_to_content_list_v2(  # type: ignore[attr-defined]
                index_block,
                "images",
                (100, 200),
            )
            pipeline_text = mkcontent.make_blocks_to_content_list_v2(  # type: ignore[attr-defined]
                text_block,
                "images",
                (100, 200),
            )
            vlm_text = vlmcontent.make_blocks_to_content_list_v2(  # type: ignore[attr-defined]
                text_block,
                "images",
                (100, 200),
            )

        self.assertEqual(len(copy_calls), 2)
        self.assertEqual(len(vlm_copy_calls), 1)
        self.assertEqual(index_block["type"], BlockType.TEXT)
        self.assertEqual(text_block["type"], BlockType.TEXT)
        for name in ("pipeline", "vlm"):
            self.assertEqual(len(make_calls[name]), 2)
            self.assertIs(make_calls[name][0][0], index_block)
            self.assertIs(make_calls[name][1][0], text_block)
            self.assertEqual(
                merge_calls[name],
                [
                    {"type": BlockType.TEXT, "lines": [index_block["lines"][0]]},
                    {"type": BlockType.TEXT, "lines": [index_block["lines"][1]]},
                ],
            )

        def expected_index(texts: list[str], bbox: list[int]) -> dict[str, object]:
            return {
                "type": ContentTypeV2.INDEX,
                "content": {
                    "list_type": ContentTypeV2.LIST_TEXT,
                    "list_items": [
                        {
                            "item_type": ContentTypeV2.SPAN_TEXT,
                            "item_content": [
                                {
                                    "type": ContentTypeV2.SPAN_TEXT,
                                    "content": text,
                                }
                            ],
                        }
                        for text in texts
                    ],
                },
                "bbox": bbox,
                engine._LAYOUT_OUTPUT_FIELD: {
                    "label": "content",
                    "score": 0.94,
                    "index": 7,
                    "order": 3,
                },
            }

        self.assertEqual(
            pipeline_index,
            expected_index(
                ["1 Introduction", "2 Methods", "3 Results"],
                [0, 1, 90, 20],
            ),
        )
        self.assertEqual(
            vlm_index,
            expected_index(
                ["一、概述", "二、方法", "三、结果"],
                [10, 11, 80, 30],
            ),
        )
        self.assertIs(pipeline_text, ordinary_results["pipeline"])
        self.assertIs(vlm_text, ordinary_results["vlm"])
        self.assertEqual(
            pipeline_text[engine._LAYOUT_OUTPUT_FIELD],
            {"label": "text", "score": 0.88, "index": 8},
        )
        self.assertEqual(
            vlm_text[engine._LAYOUT_OUTPUT_FIELD],
            {"label": "text", "score": 0.88, "index": 8},
        )
        self.assertEqual(pipeline_text["type"], "paragraph")
        self.assertEqual(vlm_text["type"], "paragraph")


class BatchDiscoveryTests(unittest.TestCase):
    def test_discovery_deduplicates_inputs_and_skips_generated_output_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "A.PDF"
            second = root / "nested" / "b.pdf"
            generated = root / "old.pdf2md" / "raw" / "copy.pdf"
            for path in (first, second, generated):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"%PDF-1.4\n")

            discovered = cli.discover_pdf_inputs(
                [str(root), str(second), str(first)],
                recursive=True,
            )

        self.assertEqual(discovered, sorted([first, second], key=lambda item: str(item).casefold()))

    def test_output_root_disambiguates_same_stem_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = (root / "one" / "paper.pdf").resolve()
            second = (root / "two" / "paper.pdf").resolve()
            output_root = root / "out"

            outputs = cli.batch_output_paths([first, second], str(output_root))

        first_digest = hashlib.sha256(str(first).encode("utf-8")).hexdigest()[:8]
        second_digest = hashlib.sha256(str(second).encode("utf-8")).hexdigest()[:8]
        self.assertEqual(outputs[first], output_root.resolve() / f"paper-{first_digest}.pdf2md")
        self.assertEqual(outputs[second], output_root.resolve() / f"paper-{second_digest}.pdf2md")
        self.assertNotEqual(outputs[first], outputs[second])
        self.assertEqual(
            cli.batch_output_paths([second], str(output_root))[second],
            outputs[second],
        )


class _FakeConversionSession:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.profile = str(kwargs.get("profile", "balanced"))
        self.method = str(kwargs.get("method", "auto"))
        self.language = str(kwargs.get("language", "ch"))
        self.cancel_event = kwargs.get("cancel_event", threading.Event())
        self.preload_result = {"elapsed_seconds": 0.1, "gpu": {"device": "mock"}}
        self.start_calls = 0
        self.close_calls = 0
        self.convert_calls: list[core.ConversionOptions] = []

    def start(self) -> "_FakeConversionSession":
        self.start_calls += 1
        return self

    def convert(self, options: core.ConversionOptions) -> core.RunResult:
        self.convert_calls.append(options)
        return _result(options.source, options.output)

    def close(self) -> None:
        self.close_calls += 1

    def __enter__(self) -> "_FakeConversionSession":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class BatchExecutionTests(unittest.TestCase):
    def test_owned_batch_starts_and_closes_one_session_for_all_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("first.pdf", "second.pdf"):
                (root / name).write_bytes(b"%PDF-1.4\n")
            args = cli.build_batch_parser().parse_args(
                [str(root), "--load-model", "-o", str(root / "outputs")]
            )
            created: list[_FakeConversionSession] = []

            def factory(**kwargs: object) -> _FakeConversionSession:
                instance = _FakeConversionSession(**kwargs)
                created.append(instance)
                return instance

            with patch.object(cli, "ConversionSession", side_effect=factory):
                summary = cli.execute_batch(args)

        self.assertEqual(len(created), 1)
        session = created[0]
        self.assertEqual(session.start_calls, 1)
        self.assertEqual(session.close_calls, 1)
        self.assertEqual(len(session.convert_calls), 2)
        self.assertTrue(session.kwargs["preload_model"])
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["succeeded"], 2)
        self.assertEqual(summary["failed"], 0)

    def test_shared_session_is_neither_started_nor_closed_by_nested_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            args = cli.build_batch_parser().parse_args([str(source)])
            session = _FakeConversionSession()

            summary = cli.execute_batch(args, shared_session=session)

        self.assertEqual(session.start_calls, 0)
        self.assertEqual(session.close_calls, 0)
        self.assertEqual(len(session.convert_calls), 1)
        self.assertTrue(summary["ok"])


class CliCompatibilityTests(unittest.TestCase):
    def test_legacy_single_pdf_command_still_dispatches_to_single_main(self) -> None:
        with (
            patch.object(cli, "configure_streams"),
            patch.object(cli, "_single_main", return_value=17) as single,
        ):
            exit_code = cli.main(["paper.pdf", "--ocr", "--page", "3"])

        self.assertEqual(exit_code, 17)
        args = single.call_args.args[0]
        self.assertEqual(args.pdf, "paper.pdf")
        self.assertTrue(args.ocr)
        self.assertEqual(args.page, 3)

    def test_preload_model_alias_and_session_alias_remain_parseable(self) -> None:
        batch = cli.build_batch_parser().parse_args(["paper.pdf", "--preload-model"])
        session = cli.build_session_parser("pdf2md session").parse_args(
            ["--profile", "fast", "--ocr", "-l", "en"]
        )

        self.assertTrue(batch.load_model)
        self.assertEqual(session.profile, "fast")
        self.assertTrue(session.ocr)
        self.assertEqual(session.lang, "en")

    def test_interactive_exit_leaves_context_and_releases_session(self) -> None:
        args = cli.build_session_parser().parse_args([])
        fake = _FakeConversionSession()

        with (
            patch.object(cli, "ConversionSession", return_value=fake),
            patch("builtins.input", return_value="exit"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = cli.run_interactive_session(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake.start_calls, 1)
        self.assertEqual(fake.close_calls, 1)

    def test_interactive_session_ignores_bom_on_first_piped_command(self) -> None:
        args = cli.build_session_parser().parse_args([])
        fake = _FakeConversionSession()
        stdout = io.StringIO()

        with (
            patch.object(cli, "ConversionSession", return_value=fake),
            patch("builtins.input", side_effect=["\ufeffstatus", "exit"]),
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = cli.run_interactive_session(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("ready profile=", stdout.getvalue())
        self.assertEqual(fake.start_calls, 1)
        self.assertEqual(fake.close_calls, 1)

    def test_powershell_utf8_bom_reconfigures_gbk_native_pipe(self) -> None:
        raw = io.BytesIO(
            b"\xef\xbb\xbfconvert \"data\\paper.pdf\" --pages 1-2\nexit\n"
        )
        stream = io.TextIOWrapper(io.BufferedReader(raw), encoding="gbk")

        with patch.object(cli.sys, "stdin", stream):
            cli._configure_piped_session_stdin()
            first = stream.readline()
            second = stream.readline()

        self.assertEqual(first, 'convert "data\\paper.pdf" --pages 1-2\n')
        self.assertEqual(second, "exit\n")


if __name__ == "__main__":
    unittest.main()
