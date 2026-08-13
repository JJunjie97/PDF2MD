from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pdf2md_cli as cli  # noqa: E402
import pdf2md_core as core  # noqa: E402
import pdf2md_gui as gui  # noqa: E402


class PageInputTests(unittest.TestCase):
    def test_gui_defaults_full_document(self) -> None:
        self.assertIsNone(gui.normalize_pages(""))
        self.assertIsNone(gui.normalize_pages("全文"))
        self.assertIsNone(gui.normalize_pages("all"))

    def test_word_style_ranges_are_normalized(self) -> None:
        self.assertEqual(gui.normalize_pages("1, 3, 5-12"), "1,3,5-12")
        self.assertEqual(gui.normalize_pages("1，3、5—12"), "1,3,5-12")
        self.assertEqual(core.parse_page_ranges("1，3、5—12"), [(1, 1), (3, 3), (5, 12)])

    def test_invalid_page_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            gui.normalize_pages("12-5")


class ProgressEventTests(unittest.TestCase):
    def test_mineru_page_progress_is_parsed(self) -> None:
        line = "Processing pages:  45%|████▌     | 9/20 [00:15<00:18]"
        self.assertEqual(core.parse_engine_progress(line), (45, 9, 20))

    def test_unrelated_log_line_has_no_progress(self) -> None:
        self.assertIsNone(core.parse_engine_progress("Loading models..."))


class GuiBridgeTests(unittest.TestCase):
    def test_bridge_prepares_cli_parameters_without_starting_gui(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "paper.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            prepared = gui.PDF2MDBridge()._prepare_config(
                {
                    "source": str(source),
                    "output": "",
                    "pages": "1，3-5",
                    "profile": "balanced",
                    "method": "auto",
                    "language": "ch",
                    "timeout": "1800",
                    "force": True,
                }
            )
            self.assertEqual(prepared["output"], gui.default_output_for(source))
            self.assertEqual(prepared["pages"], "1,3-5")
            self.assertEqual(prepared["method"], "auto")
            self.assertTrue(prepared["force"])

    def test_bridge_rejects_unknown_method(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "paper.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            with self.assertRaisesRegex(ValueError, "解析方式"):
                gui.PDF2MDBridge()._prepare_config(
                    {
                        "source": str(source),
                        "profile": "balanced",
                        "method": "unknown",
                        "language": "ch",
                        "timeout": 1800,
                    }
                )


class CliInterfaceTests(unittest.TestCase):
    def test_method_option_supports_text_mode(self) -> None:
        args = cli.build_parser().parse_args(["paper.pdf", "--method", "txt"])
        self.assertEqual(args.method, "txt")
        self.assertFalse(args.ocr)

    def test_legacy_ocr_flag_remains_available(self) -> None:
        args = cli.build_parser().parse_args(["paper.pdf", "--ocr"])
        self.assertTrue(args.ocr)


if __name__ == "__main__":
    unittest.main()
