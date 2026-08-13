from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pdf2md_core as core  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
