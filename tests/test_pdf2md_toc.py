from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pdf2md_frontmatter import parse_entry_lines  # noqa: E402
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
            self.assertIn('<a id="1" data-pdf2md-nav="target"></a>', published)

    def test_publish_forwards_frontmatter_refresh_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.pdf"
            source.write_bytes(b"pdf")
            layout = core.output_layout(source, root / "output")
            core.ensure_layout(layout)
            selection = layout.selections / "cached.md"
            selection.write_text("## Contents\n\n1 Start 1\n", encoding="utf-8")

            with mock.patch.object(
                core,
                "enhance_document_navigation",
                side_effect=lambda content, **_kwargs: content,
            ) as enhance:
                core._publish_document(
                    layout,
                    [
                        {
                            "selection": selection.relative_to(layout.root).as_posix(),
                            "pages": "all",
                        }
                    ],
                    source=source,
                    refresh_frontmatter=True,
                )

        self.assertTrue(enhance.call_args.kwargs["force_frontmatter"])

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
        self.assertIn("## 1.1 研究背景", result)
        self.assertIn("## 1.1.1 方法", result)
        self.assertNotIn("### 1.1 研究背景", result)

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
        self.assertLess(result.index('<a id="1"'), result.index("## Contents"))

    def test_standalone_body_text_is_never_promoted_or_linked(self) -> None:
        source = """## 目录

一.产品描述....2
二.通讯格式....3

一.产品描述

Text.

## 二.通讯格式

Text.
"""

        result = enhance_document_navigation(source)

        self.assertIn("\n一.产品描述\n", result)
        self.assertIn("- 一.产品描述", result)
        self.assertNotIn("[一.产品描述](#", result)
        self.assertIn("[二.通讯格式](#1)", result)

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

    def test_title_ending_in_number_is_stable_after_page_number_removal(self) -> None:
        source = """## Contents

3.3.1 F = 3 55
3.3.2 Other 56

## 3.3.1 F = 3

Body.

## 3.3.2 Other

Body.
"""

        once = enhance_document_navigation(source)
        twice = enhance_document_navigation(once)

        self.assertEqual(twice, once)
        self.assertIn("[3.3.1 F = 3](#1)", once)

    def test_escaped_brackets_in_link_labels_do_not_accumulate(self) -> None:
        source = """## Contents

1 Method [59] 2
2 Other 3

## 1 Method [59]

Body.

## 2 Other

Body.
"""

        once = enhance_document_navigation(source)
        twice = enhance_document_navigation(once)

        self.assertEqual(twice, once)
        self.assertIn(r"[1 Method \[59\]](#1)", once)

    def test_targets_link_back_to_their_navigation_section(self) -> None:
        source = """## Contents

1. Overview 1
2. Details 2

## 1. Overview

Text.

## 2. Details

Text.
"""
        result = enhance_document_navigation(source)

        self.assertIn('<a id="toc" data-pdf2md-nav="section"></a>\n## Contents', result)
        self.assertEqual(result.count("[↑ Contents](#toc)"), 2)

    def test_number_mismatch_cannot_link_same_heading_body(self) -> None:
        source = """## Contents

2.1 Methods 10
2.2 Results 11

## 3.1 Methods

Wrong section.

## 2.1 Methods

Correct section.

## 2.2 Results

Text.
"""
        result = enhance_document_navigation(source)
        anchor_position = result.index('<a id="1"')
        self.assertLess(anchor_position, result.index("## 2.1 Methods"))
        self.assertGreater(anchor_position, result.index("## 3.1 Methods"))

    def test_ambiguous_unnumbered_heading_targets_are_not_guessed(self) -> None:
        source = """## Contents

2.1 Methods 10
2.2 Results 11

## Methods

First occurrence.

## Methods

Second occurrence.

## 2.2 Results

Text.
"""

        result = enhance_document_navigation(source)

        self.assertIn("- 2.1 Methods", result)
        self.assertNotIn("[2.1 Methods](#", result)
        self.assertIn("[2.2 Results](#1)", result)

    def test_figure_and_table_lists_link_only_to_promoted_captions(self) -> None:
        source = """## List of Tables

A.1 Historical experiments 12

## List of Figures

2.1 Apparatus overview 14

# Historical experiments

This heading has the same words as the table entry.

See Figure 2.1 in the discussion.

Table A.1: Historical experiments and measurements.

Figure 2.1: Apparatus overview and optical path.
"""
        result = enhance_document_navigation(source)

        self.assertRegex(result, r"\[A\.1 Historical experiments\]\(#\d+\)")
        self.assertRegex(result, r"\[2\.1 Apparatus overview\]\(#\d+\)")
        self.assertIn("###### Table A.1", result)
        self.assertIn("###### Figure 2.1", result)
        self.assertIn("[↑ List of Tables](#list-of-tables)", result)
        self.assertIn("[↑ List of Figures](#list-of-figures)", result)
        self.assertNotIn("###### See Figure", result)

        lines = result.splitlines()
        for index, line in enumerate(lines[:-1]):
            if re.fullmatch(
                r'<a id="\d+" data-pdf2md-nav="target"'
                r'(?: data-pdf2md-heading="generated")?></a>',
                line,
            ):
                self.assertRegex(lines[index + 1], r"^#{1,6}\s")

    def test_duplicate_caption_identifier_without_clear_title_match_is_not_guessed(self) -> None:
        source = """## List of Figures

2.1 Apparatus 4

Figure 2.1: Apparatus left view.

Figure 2.1: Apparatus right view.
"""

        result = enhance_document_navigation(source)

        self.assertIn("- 2.1 Apparatus", result)
        self.assertNotIn("[2.1 Apparatus](#", result)
        self.assertNotIn("###### Figure 2.1", result)

    def test_continued_contents_are_merged(self) -> None:
        source = """## Contents

1. First 1

## Contents (continued)

2. Second 2

## 1. First

Text.

## 2. Second

Text.
"""
        result = enhance_document_navigation(source)

        self.assertEqual(result.count("## Contents"), 1)
        self.assertIn("[1. First](#1)", result)
        self.assertIn("[2. Second](#2)", result)

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

    def test_unrecoverable_long_list_debris_is_removed_before_body_heading(self) -> None:
        debris = "10.4 Comparison " + ". " * 400 + "garbled"
        source = f"""## List of Figures

1.1 First result 2
1.2 Second result 3

{debris}

## Chapter 1

Body.
"""

        result = enhance_document_navigation(source)

        self.assertNotIn(debris, result)
        self.assertIn("## Chapter 1", result)

    def test_corrupt_figure_list_is_rebuilt_from_strict_body_captions(self) -> None:
        debris = "1.1 First figure " + ". " * 400 + "garbled"
        source = f"""## List of Figures

{debris}

## Body

Figure 1.1: First figure.

Figure 1.2: Second figure.
"""

        result = enhance_document_navigation(source)

        self.assertNotIn(debris, result)
        self.assertRegex(result, r"\[1\.1 First figure\.\]\(#\d+\)")
        self.assertRegex(result, r"\[1\.2 Second figure\.\]\(#\d+\)")
        self.assertEqual(result.count("[↑ List of Figures](#list-of-figures)"), 2)

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
        self.assertIn('<a id="1" data-pdf2md-nav="target"></a>', result)
        self.assertIn('<a id="2" data-pdf2md-nav="target"></a>', result)
        self.assertNotIn("p2m-", result)
        self.assertNotIn('id="3"', result)


class FrontMatterEntryTests(unittest.TestCase):
    def test_wrapped_title_ending_in_scientific_number_is_not_mistaken_for_page(self) -> None:
        entries = parse_entry_lines(
            [
                "10.4 Comparison of isotope 85 and isotope 87",
                "magnetic lensing in a TOP trap .... 207",
            ],
            "figures",
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].page, "207")
        self.assertIn("isotope 87 magnetic lensing", entries[0].title)

    def test_plain_single_space_page_suffix_is_removed(self) -> None:
        entries = parse_entry_lines(["4 Apparatus 42", "4.1 Setup 47"], "contents")
        self.assertEqual([(entry.title, entry.page) for entry in entries], [("4 Apparatus", "42"), ("4.1 Setup", "47")])


if __name__ == "__main__":
    unittest.main()
