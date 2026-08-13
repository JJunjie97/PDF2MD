from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pdf2md_core as core  # noqa: E402
from pdf2md_markdown import convert_html_tables  # noqa: E402


class HtmlTableConversionTests(unittest.TestCase):
    def test_mineru_symbol_table_becomes_gfm_markdown(self) -> None:
        source = (
            '<table><tr><td colspan="3">字符</td></tr>'
            "<tr><td>Symbol</td><td>Description</td><td>Unit</td></tr>"
            "<tr><td>R</td><td>the gas constant</td>"
            "<td> $m^{2} \\cdot s^{-2} \\cdot K^{-1}$ </td></tr>"
            '<tr><td colspan="3">算子</td></tr>'
            "<tr><td>Δ</td><td>difference</td><td></td></tr></table>"
        )

        result = convert_html_tables(source)

        self.assertNotIn("<table", result.casefold())
        self.assertNotIn("<td", result.casefold())
        self.assertIn("**字符**", result)
        self.assertIn("| Symbol | Description | Unit |", result)
        self.assertIn("| --- | --- | --- |", result)
        self.assertIn(r"$m^{2} \cdot s^{-2} \cdot K^{-1}$", result)
        self.assertIn("| **算子** |  |  |", result)

    def test_colspan_and_rowspan_are_flattened_without_html(self) -> None:
        source = (
            "<table><tr><th>A</th><th>B</th><th>C</th></tr>"
            '<tr><td rowspan="2">x</td><td colspan="2">y</td></tr>'
            "<tr><td>z</td><td>w</td></tr></table>"
        )

        result = convert_html_tables(source)

        self.assertNotIn("<table", result.casefold())
        self.assertIn("| A | B | C |", result)
        self.assertIn("| x | y |  |", result)
        self.assertIn("|  | z | w |", result)

    def test_image_inside_table_is_preserved_as_markdown_image(self) -> None:
        source = (
            "<table><tr><th>Pin</th><th>Diagram</th></tr>"
            '<tr><td>1</td><td><img src="images/cache.png" alt="pin"></td></tr></table>'
        )

        result = convert_html_tables(source)

        self.assertIn("![pin](images/cache.png)", result)
        self.assertNotIn("<img", result.casefold())

    def test_non_table_markdown_is_unchanged(self) -> None:
        source = "# Title\n\nText with $x | y$.\n"
        self.assertEqual(convert_html_tables(source), source)


class NumberedImagePublishingTests(unittest.TestCase):
    def test_images_are_numbered_by_first_markdown_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.pdf"
            layout = core.output_layout(source, root / "output")
            core.ensure_layout(layout)
            (layout.cached_images / "aaa.png").write_bytes(b"first")
            (layout.cached_images / "bbb.jpg").write_bytes(b"second")
            selection = layout.selections / "cached.md"
            original = (
                "Second first: ![](images/bbb.jpg)\n\n"
                "Then first: ![diagram](images/aaa.png)\n\n"
                "Repeated: ![](images/bbb.jpg)\n"
            )
            selection.write_text(original, encoding="utf-8")

            core._publish_document(
                layout,
                [{"selection": selection.relative_to(layout.root).as_posix(), "pages": "all"}],
            )

            published = layout.markdown.read_text(encoding="utf-8")
            self.assertIn("![](images/1.jpg)", published)
            self.assertIn("![diagram](images/2.png)", published)
            self.assertEqual(published.count("images/1.jpg"), 2)
            self.assertEqual(
                {path.name for path in layout.images.iterdir()},
                {"1.jpg", "2.png"},
            )
            self.assertEqual(selection.read_text(encoding="utf-8"), original)

    def test_table_images_are_numbered_after_table_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.pdf"
            layout = core.output_layout(source, root / "output")
            core.ensure_layout(layout)
            (layout.cached_images / "figure.png").write_bytes(b"figure")
            selection = layout.selections / "cached.md"
            selection.write_text(
                "<table><tr><th>Name</th><th>Figure</th></tr>"
                '<tr><td>A</td><td><img src="images/figure.png"></td></tr></table>\n',
                encoding="utf-8",
            )

            core._publish_document(
                layout,
                [{"selection": selection.relative_to(layout.root).as_posix(), "pages": "all"}],
            )

            published = layout.markdown.read_text(encoding="utf-8")
            self.assertNotIn("<table", published.casefold())
            self.assertIn("![](images/1.png)", published)
            self.assertTrue((layout.images / "1.png").is_file())


if __name__ == "__main__":
    unittest.main()
