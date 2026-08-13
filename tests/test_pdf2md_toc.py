from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pdf2md_toc import enhance_document_navigation  # noqa: E402
import pdf2md_core as core  # noqa: E402


class TocEnhancementTests(unittest.TestCase):
    def test_publish_applies_navigation_to_cached_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.pdf"
            layout = core.output_layout(source, root / "output")
            core.ensure_layout(layout)
            selection = layout.selections / "cached.md"
            selection.write_text(
                "## Contents\n\n1. Start....1\n2. End....2\n\n"
                "## 1. Start\n\nText.\n\n## 2. End\n\nText.\n",
                encoding="utf-8",
            )

            core._publish_document(
                layout,
                [
                    {
                        "selection": selection.relative_to(layout.root).as_posix(),
                        "pages": "all",
                    }
                ],
            )

            published = layout.markdown.read_text(encoding="utf-8")
            self.assertIn("[1. Start](#1)", published)
            self.assertIn('<a id="1"></a>', published)

    def test_numbered_thesis_toc_becomes_nested_links(self) -> None:
        source = """# Paper

## 目录

第1章 绪论……3
1.1 研究背景……3
1.1.1 方法……5

## 第1章 绪论

Body.

## 1.1 研究背景

Body.

## 1.1.1 方法

Body.
"""

        result = enhance_document_navigation(source)

        self.assertIn("- [第1章 绪论](#1)", result)
        self.assertIn("  - [1.1 研究背景](#2)", result)
        self.assertIn("    - [1.1.1 方法](#3)", result)
        self.assertNotIn("— 3", result)
        self.assertNotIn("— 5", result)
        self.assertIn("## 第1章 绪论", result)
        self.assertIn("### 1.1 研究背景", result)
        self.assertIn("#### 1.1.1 方法", result)

    def test_datasheet_toc_can_link_back_to_earlier_headings(self) -> None:
        source = """## 1. General description

Text.

## 2. Features and benefits

Text.

## Contents

1. General description....1
2. Features and benefits....2
"""

        result = enhance_document_navigation(source)

        self.assertIn("[1. General description](#1)", result)
        self.assertLess(result.index('<a id="1">'), result.index("## Contents"))

    def test_exact_standalone_chapter_is_promoted_to_heading(self) -> None:
        source = """## 目录

一.产品描述....2
二.通讯格式....3

一.产品描述

Text.

## 二.通讯格式

Text.
"""

        result = enhance_document_navigation(source)

        self.assertIn("## 一.产品描述", result)
        self.assertIn("[一.产品描述](#1)", result)

    def test_minor_toc_ocr_error_uses_real_heading_title(self) -> None:
        source = """## Contents

1. General descriptlon....1
2. Applications....2

## 1. General description

Text.

## 2. Applications

Text.
"""

        result = enhance_document_navigation(source)

        self.assertIn("[1. General description](#1)", result)
        self.assertNotIn("descriptlon", result)

    def test_split_page_suffix_is_joined_without_guessing_missing_target(self) -> None:
        source = """## Table of Contents

Chapter 1 Getting Started
.... 7
Chapter 2 Missing chapter....19

## Chapter 1 Getting Started

Text.
"""

        result = enhance_document_navigation(source)

        self.assertIn("- [Chapter 1 Getting Started](#1)", result)
        self.assertIn("- Chapter 2 Missing chapter", result)
        self.assertNotIn("— 7", result)
        self.assertNotIn("— 19", result)

    def test_document_without_source_toc_is_unchanged(self) -> None:
        source = "# Title\n\n## Section\n\nText.\n"
        self.assertEqual(enhance_document_navigation(source), source)

    def test_processing_is_idempotent(self) -> None:
        source = """## Contents

1. Overview....1
2. Details....2

## 1. Overview

Text.

## 2. Details

Text.
"""
        once = enhance_document_navigation(source)
        twice = enhance_document_navigation(once)
        self.assertEqual(twice, once)

    def test_merged_figure_entries_are_split_at_page_title_boundary(self) -> None:
        source = """## List of Figures

Figure 1 First result......12Figure 2 Second result......13

## Body

Text.
"""

        result = enhance_document_navigation(source)

        self.assertIn("- Figure 1 First result", result)
        self.assertIn("- Figure 2 Second result", result)
        self.assertNotIn("— 12", result)
        self.assertNotIn("— 13", result)

    def test_unmatched_math_entry_does_not_accumulate_backslashes(self) -> None:
        source = """## 图目录

图 1 $\\theta$ 结果……2
图 2 $\\omega$ 结果……3

## 正文

Text.
"""

        once = enhance_document_navigation(source)
        twice = enhance_document_navigation(once)

        self.assertEqual(twice, once)
        self.assertIn(r"$\theta$", once)

    def test_heading_markdown_is_preserved_while_link_label_is_plain(self) -> None:
        source = """## Contents

1. Gain $\\theta$....1
2. End....2

## 1. **Gain** $\\theta$

Text.

## 2. End

Text.
"""

        result = enhance_document_navigation(source)

        self.assertIn(r"[1. Gain $\theta$](#1)", result)
        self.assertIn(r"## 1. **Gain** $\theta$", result)

    def test_anchors_are_sequential_for_matched_headings_only(self) -> None:
        source = """# Cover

## Preface

Text.

## Contents

1. First....3
2. Missing....4
3. Third....5

## Unlisted heading

Text.

## 1. First

Text.

## 3. Third

Text.
"""

        result = enhance_document_navigation(source)

        self.assertIn("[1. First](#1)", result)
        self.assertIn("- 2. Missing", result)
        self.assertIn("[3. Third](#2)", result)
        self.assertIn('<a id="1"></a>', result)
        self.assertIn('<a id="2"></a>', result)
        self.assertNotIn("p2m-", result)
        self.assertNotIn('id="3"', result)


if __name__ == "__main__":
    unittest.main()
