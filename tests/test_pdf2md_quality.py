from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


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
