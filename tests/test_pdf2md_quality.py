from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pdf2md_core as core  # noqa: E402
import pdf2md_engine as engine  # noqa: E402


class MarkdownQualityTests(unittest.TestCase):
    def test_extracted_markdown_requires_valid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            extracted = Path(temporary)
            (extracted / "paper.md").write_bytes(b"valid prefix\xffinvalid")

            with self.assertRaisesRegex(core.ConversionError, "UTF-8"):
                core._read_extracted_markdown(extracted, Path("paper.pdf"), "3")

    def test_cached_selection_with_replacement_character_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            layout = core.output_layout(source, Path(temporary) / "output")
            core.ensure_layout(layout)
            selection = layout.selections / "broken.md"
            selection.write_text("time � and $T$\n", encoding="utf-8")
            manifest = {
                "selections": [
                    {
                        "task_key": "broken",
                        "selection": selection.relative_to(layout.root).as_posix(),
                    }
                ]
            }

            cached, replacement_count = core._cached_selection(manifest, layout, "broken")

            self.assertIsNone(cached)
            self.assertEqual(replacement_count, 1)

    def test_clean_cached_selection_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            layout = core.output_layout(source, Path(temporary) / "output")
            core.ensure_layout(layout)
            selection = layout.selections / "clean.md"
            selection.write_text("time $T$\n", encoding="utf-8")
            manifest = {
                "selections": [
                    {
                        "task_key": "clean",
                        "selection": selection.relative_to(layout.root).as_posix(),
                    }
                ]
            }

            cached, replacement_count = core._cached_selection(manifest, layout, "clean")

            self.assertIsNotNone(cached)
            self.assertEqual(replacement_count, 0)

    def test_selection_caches_valid_content_list_v2_as_raw_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            extracted = root / "extracted"
            extracted.mkdir()
            (extracted / "paper.md").write_text("# Contents\n", encoding="utf-8")
            structured = [[
                {
                    "type": "index",
                    "content": {
                        "list_type": "text_list",
                        "list_items": [
                            {"item_type": "text", "item_content": [{"type": "text", "content": "1 Intro 1"}]}
                        ],
                    },
                }
            ]]
            (extracted / "paper_content_list_v2.json").write_text(
                json.dumps(structured),
                encoding="utf-8",
            )
            layout = core.output_layout(source, root / "output")
            core.ensure_layout(layout)

            item = core._cache_selection(
                extracted,
                layout,
                "task",
                source,
                "1",
                core.ConversionOptions(source=source),
            )

            cached_path = layout.root / str(item["content_list_v2"])
            self.assertTrue(cached_path.is_file())
            self.assertEqual(json.loads(cached_path.read_text(encoding="utf-8")), structured)

            manifest = {"selections": [item]}
            cached, _replacement_count = core._cached_selection(manifest, layout, "task")
            assert cached is not None
            self.assertIn("content_list_v2", cached)
            cached_path.unlink()
            cached, _replacement_count = core._cached_selection(manifest, layout, "task")
            assert cached is not None
            self.assertNotIn("content_list_v2", cached)


class FrontRegionPublishingTests(unittest.TestCase):
    def _layout_with_selection(self, root: Path, name: str):
        source = root / f"{name}.pdf"
        source.write_bytes(b"%PDF-1.4")
        layout = core.output_layout(source, root / "output")
        core.ensure_layout(layout)
        selection = layout.selections / f"{name}.md"
        selection.write_text(name, encoding="utf-8")
        return source, layout, selection

    def test_only_explicit_damaged_navigation_becomes_native_recovery_page(self) -> None:
        damaged = {
            "page": 13,
            "kind": None,
            "accepted": False,
            "rule_strength": 0.62,
            "top_candidates": [{"kind": "list_of_tables", "strength": 0.62}],
            "evidence": {
                "rule": [
                    "explicit_title",
                    "unusable_navigation_debris",
                    "page_number_block",
                ],
                "stats": {"navigation_blocks": 1},
                "navigation_candidates": [["damaged entry"]],
            },
        }
        ordinary = {
            "page": 14,
            "kind": None,
            "accepted": False,
            "rule_strength": 0.62,
            "top_candidates": [{"kind": "list_of_tables", "strength": 0.62}],
            "evidence": {
                "rule": ["unusable_navigation_debris"],
                "stats": {"navigation_blocks": 1},
                "navigation_candidates": [["ordinary text"]],
            },
        }

        self.assertEqual(
            core._native_recovery_pages({"pages": [damaged, ordinary]}),
            [{
                "page": 13,
                "kind": "list_of_tables",
                "confidence": 0.62,
                "evidence": [
                    "explicit_title",
                    "page_number_block",
                    "unusable_navigation_debris",
                ],
            }],
        )

    def test_full_document_publishes_front_region_report_and_forwards_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, layout, selection = self._layout_with_selection(root, "paper")
            structured = [[
                {
                    "type": "title",
                    "content": {
                        "title_content": [{"type": "text", "content": "Contents"}]
                    },
                },
                {
                    "type": "index",
                    "content": {
                        "list_items": [{
                            "item_content": [
                                {"type": "text", "content": "1 Introduction .... 1"}
                            ]
                        }]
                    },
                },
            ]]
            content_list = layout.content_lists / "full.json"
            content_list.write_text(json.dumps(structured), encoding="utf-8")
            selected = [{
                "selection": selection.relative_to(layout.root).as_posix(),
                "content_list_v2": content_list.relative_to(layout.root).as_posix(),
                "pages": "all",
            }]

            with mock.patch.object(
                core,
                "enhance_document_navigation",
                side_effect=lambda content, **_kwargs: content,
            ) as enhance:
                core._publish_document(layout, selected, source=source)

            report_path = layout.cache / "front-regions-v1.json"
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["schema"], "pdf2md.front-regions.v1")
            self.assertEqual(report["pages"][0]["kind"], "contents")
            self.assertEqual(enhance.call_args.kwargs.get("front_regions"), report)

    def test_old_cached_selection_without_content_list_still_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, layout, selection = self._layout_with_selection(root, "legacy")
            manifest = {
                "selections": [{
                    "task_key": "legacy",
                    "selection": selection.relative_to(layout.root).as_posix(),
                    "pages": "all",
                }]
            }
            cached, replacement_count = core._cached_selection(manifest, layout, "legacy")
            self.assertEqual(replacement_count, 0)
            self.assertIsNotNone(cached)
            assert cached is not None
            with mock.patch.object(
                core,
                "enhance_document_navigation",
                side_effect=lambda content, **_kwargs: content,
            ) as enhance:
                core._publish_document(layout, [cached], source=source)

            self.assertEqual(layout.markdown.read_text(encoding="utf-8").strip(), "legacy")
            self.assertFalse((layout.cache / "front-regions-v1.json").exists())
            self.assertIsNone(enhance.call_args.kwargs.get("front_regions"))

    def test_model_fingerprint_reclassifies_content_list_without_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, layout, selection = self._layout_with_selection(root, "cached")
            content_list = layout.content_lists / "full.json"
            content_list.write_text(
                json.dumps([[
                    {
                        "type": "title",
                        "content": {"title_content": [
                            {"type": "text", "content": "Contents"}
                        ]},
                    },
                    {
                        "type": "index",
                        "content": {"list_items": [{
                            "item_content": [
                                {"type": "text", "content": "1 Introduction .... 1"}
                            ]
                        }]},
                    },
                ]]),
                encoding="utf-8",
            )
            selected = [{
                "task_key": "0123456789abcdef",
                "selection": selection.relative_to(layout.root).as_posix(),
                "content_list_v2": content_list.relative_to(layout.root).as_posix(),
                "pages": "all",
            }]
            project = root / "project"

            with (
                mock.patch.object(core, "project_root", return_value=project),
                mock.patch.object(
                    core,
                    "enhance_document_navigation",
                    side_effect=lambda content, **_kwargs: content,
                ),
                mock.patch.object(
                    core,
                    "classify_front_regions_v2",
                    wraps=core.classify_front_regions_v2,
                ) as classify,
            ):
                core._publish_document(layout, selected, source=source)
                core._publish_document(layout, selected, source=source)
                self.assertEqual(classify.call_count, 1)

                model_dir = project / "models" / "front-region" / "v1"
                model_dir.mkdir(parents=True)
                (model_dir / "layout.json").write_text(
                    json.dumps({
                        "schema": "pdf2md.region-linear.v1",
                        "kind": "layout",
                        "classes": ["contents"],
                        "feature_names": ["layout.mean_score"],
                        "weights": [[1.0]],
                        "bias": [0.0],
                        "temperature": 1.0,
                        "metadata": {
                            "approved_for_auto_action": True,
                            "experimental": False,
                            "training_eligible": True,
                            "redistributable": True,
                        },
                    }),
                    encoding="utf-8",
                )
                core._publish_document(layout, selected, source=source)
                self.assertEqual(classify.call_count, 1)

                (model_dir / "text.json").write_text(
                    json.dumps({
                        "schema": "pdf2md.region-linear.v1",
                        "kind": "text",
                        "classes": ["contents"],
                        "feature_names": ["text.log1p_chars"],
                        "weights": [[1.0]],
                        "bias": [0.0],
                        "temperature": 1.0,
                        "metadata": {
                            "approved_for_auto_action": True,
                            "experimental": False,
                            "training_eligible": True,
                            "redistributable": True,
                        },
                    }),
                    encoding="utf-8",
                )
                (model_dir / "policy.json").write_text(
                    json.dumps({
                        "schema": "pdf2md.region-cascade-policy.v1",
                        "approved_for_auto_action": True,
                        "experimental": False,
                        "artifact_sha256": {
                            "layout": core._file_sha256(model_dir / "layout.json"),
                            "text": core._file_sha256(model_dir / "text.json"),
                        },
                    }),
                    encoding="utf-8",
                )
                core._publish_document(layout, selected, source=source)

            self.assertEqual(classify.call_count, 2)
            reports = list(
                (layout.cache / "front-regions" / "0123456789abcdef").glob("*.json")
            )
            self.assertEqual(len(reports), 2)
            self.assertTrue((layout.cache / "front-regions-v1.json").is_file())

    def test_experimental_or_regression_only_model_cannot_be_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            base_metadata = {
                "approved_for_auto_action": True,
                "experimental": False,
                "training_eligible": True,
                "redistributable": True,
            }
            for kind in ("layout", "text"):
                (model_dir / f"{kind}.json").write_text(json.dumps({
                    "schema": "pdf2md.region-linear.v1",
                    "kind": kind,
                    "classes": ["contents"],
                    "feature_names": ["feature"],
                    "weights": [[1.0]],
                    "bias": [0.0],
                    "temperature": 1.0,
                    "metadata": dict(base_metadata),
                }), encoding="utf-8")

            def policy(**changes: object) -> dict[str, object]:
                value: dict[str, object] = {
                    "schema": "pdf2md.region-cascade-policy.v1",
                    "approved_for_auto_action": True,
                    "experimental": False,
                    "artifact_sha256": {
                        kind: core._file_sha256(model_dir / f"{kind}.json")
                        for kind in ("layout", "text")
                    },
                }
                value.update(changes)
                return value

            self.assertTrue(core._front_region_models_approved(model_dir, policy()))
            self.assertFalse(core._front_region_models_approved(
                model_dir, policy(experimental=True),
            ))
            layout = json.loads((model_dir / "layout.json").read_text(encoding="utf-8"))
            layout["metadata"]["training_eligible"] = False
            (model_dir / "layout.json").write_text(json.dumps(layout), encoding="utf-8")
            self.assertFalse(core._front_region_models_approved(model_dir, policy()))


class SpanRepairPatchTests(unittest.TestCase):
    def test_font_mapping_repairs_only_aligned_replacement_slots(self) -> None:
        chars = [
            {"char": "\ufffd", "font": {"name": "ABCDEF+XITSMath-Regular"}},
            {"char": "\u27c2", "font": {"name": "ABCDEF+XITSMath-Regular"}},
            {"char": "\ufffd", "font": {"name": "ABCDEF+XITSMath-Regular"}},
        ]

        repaired = engine._repair_font_characters(
            chars,
            {"XITSMath-Regular": "\U0001d459\u27c2\U0001d714"},
        )

        self.assertEqual(repaired, 2)
        self.assertEqual([char["char"] for char in chars], ["l", "\u27c2", "\u03c9"])

    def test_font_mapping_leaves_ambiguous_alignment_for_span_ocr(self) -> None:
        chars = [
            {"char": "A", "font": {"name": "ABCDEF+Math"}},
            {"char": "\ufffd", "font": {"name": "ABCDEF+Math"}},
        ]

        repaired = engine._repair_font_characters(chars, {"Math": "BC"})

        self.assertEqual(repaired, 0)
        self.assertEqual(chars[1]["char"], "\ufffd")

    def test_replacement_character_triggers_existing_span_ocr_path(self) -> None:
        engine.install_span_repair_patch()

        from mineru.utils import span_pre_proc

        signal = span_pre_proc._get_private_use_text_signal(
            [{"char": "time "}, {"char": "\ufffd"}, {"char": " axis"}]
        )

        self.assertEqual(signal["replacement_count"], 1)
        self.assertTrue(
            span_pre_proc._should_fallback_to_post_ocr_for_private_use_text(signal)
        )

    def test_clean_text_keeps_original_span_decision(self) -> None:
        engine.install_span_repair_patch()

        from mineru.utils import span_pre_proc

        signal = span_pre_proc._get_private_use_text_signal(
            [{"char": "ordinary text without damaged glyphs"}]
        )

        self.assertEqual(signal["replacement_count"], 0)
        self.assertFalse(
            span_pre_proc._should_fallback_to_post_ocr_for_private_use_text(signal)
        )


if __name__ == "__main__":
    unittest.main()
