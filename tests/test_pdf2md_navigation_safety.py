from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pdf2md_frontmatter as frontmatter  # noqa: E402
import pdf2md_toc as toc  # noqa: E402
from pdf2md_frontmatter import extract_front_matter, parse_entry_lines  # noqa: E402
from pdf2md_toc import enhance_document_navigation  # noqa: E402


class NavigationContentSafetyTests(unittest.TestCase):
    def test_continued_contents_cannot_merge_across_body(self) -> None:
        source = """## Contents

1. Alpha 1

## 1. Alpha

SECRET BODY MUST STAY.

## Contents (continued)

2. Beta 2

## 2. Beta

Body.
"""

        result = enhance_document_navigation(source)

        self.assertIn("## 1. Alpha", result)
        self.assertIn("SECRET BODY MUST STAY.", result)
        self.assertIn("## 2. Beta", result)

    def test_plain_numbered_body_after_contents_is_preserved(self) -> None:
        source = """## Contents
1 Introduction 1
1.1 Background 2

1 Introduction
1.1 Background
Actual body prose.
"""

        result = enhance_document_navigation(source)

        self.assertIn("\n1 Introduction\n1.1 Background\nActual body prose.\n", result)
        self.assertEqual(result.count("Actual body prose."), 1)

    def test_body_heading_ending_in_a_number_is_not_a_directory_entry(self) -> None:
        source = """## Contents
1 Introduction 1
2 Methods 5
3 Results 9

1 Windows 10
Actual body prose must remain.
"""

        result = enhance_document_navigation(source)

        self.assertIn("\n1 Windows 10\nActual body prose must remain.\n", result)
        self.assertNotIn("Windows 10 Actual body prose", result)

    def test_numbered_body_restart_needs_no_blank_separator(self) -> None:
        source = """## Contents
1 Introduction 1
2 Methods 5
3 Results 9
1 Windows 10
Actual body prose must remain.
"""

        result = enhance_document_navigation(source)

        self.assertIn("\n1 Windows 10\nActual body prose must remain.\n", result)

    def test_caption_sequence_restart_is_not_absorbed_by_figure_list(self) -> None:
        source = """## List of Figures
1.1 Diagram 3
2.1 Graph 4

Figure 1.1: Sensor 10
Figure 1.2: Other 11
Actual body prose must remain.
"""

        result = enhance_document_navigation(source)

        self.assertIn(
            "\nFigure 1.1: Sensor 10\nFigure 1.2: Other 11\n"
            "Actual body prose must remain.\n",
            result,
        )

    def test_prose_immediately_after_directory_entries_is_preserved(self) -> None:
        source = """## Contents
1 Introduction 1
2 Methods 5
Actual body prose must remain.
Second sentence.
"""

        result = enhance_document_navigation(source)

        self.assertIn("\nActual body prose must remain.\nSecond sentence.\n", result)
        self.assertEqual(result.count("Actual body prose must remain."), 1)

    def test_indented_code_after_directory_is_preserved(self) -> None:
        source = """## Contents
1 Introduction 1
2 Methods 5

    1 Example 2
    code body must remain
"""

        result = enhance_document_navigation(source)

        self.assertIn("\n    1 Example 2\n    code body must remain\n", result)

    def test_corrupt_list_debris_cannot_consume_unheaded_body(self) -> None:
        debris = "1.1 Broken figure " + ". " * 300
        source = f"""## List of Figures

{debris}
Figure 1.1: Body figure.
Plain body prose must remain.
"""

        result = enhance_document_navigation(source)

        self.assertNotIn(debris, result)
        self.assertIn("Figure 1.1: Body figure.", result)
        self.assertIn("Plain body prose must remain.", result)

    def test_native_entries_remove_only_corroborated_source_list_remnants(self) -> None:
        debris = "1.1 Broken figure " + ". " * 300
        source_content = f"""## List of Figures

{debris}

7.1 Ellipse fitting example. 80
7.2 Detection result. 82

## Body

Figure 7.1: Ellipse fitting example.
"""
        native = {
            "figures": frontmatter.FrontMatterSection(
                kind="figures",
                title="List of Figures",
                entries=(
                    frontmatter.FrontMatterEntry("figures", "1.1 Broken figure", "2"),
                    frontmatter.FrontMatterEntry(
                        "figures", "7.1 Ellipse fitting example.", "80"
                    ),
                    frontmatter.FrontMatterEntry("figures", "7.2 Detection result.", "82"),
                ),
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(toc, "extract_front_matter", return_value=native):
                result = enhance_document_navigation(source_content, source=source)
                repeated = enhance_document_navigation(result, source=source)

        self.assertNotIn(debris, result)
        self.assertNotIn("7.1 Ellipse fitting example. 80", result)
        self.assertNotIn("7.2 Detection result. 82", result)
        self.assertIn("Figure 7.1: Ellipse fitting example.", result)
        self.assertEqual(repeated, result)

    def test_native_cleanup_requires_the_source_page_to_match(self) -> None:
        source_content = """## Contents
1 Introduction 1
2 Methods 5

1 Introduction 2024
Actual body prose must remain.
"""
        native = {
            "contents": frontmatter.FrontMatterSection(
                kind="contents",
                title="Contents",
                entries=(
                    frontmatter.FrontMatterEntry("contents", "1 Introduction", "1"),
                    frontmatter.FrontMatterEntry("contents", "2 Methods", "5"),
                ),
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(toc, "extract_front_matter", return_value=native):
                result = enhance_document_navigation(source_content, source=source)

        self.assertIn("\n1 Introduction 2024\nActual body prose must remain.\n", result)

    def test_legacy_navigation_bullets_are_fully_migrated_without_dangling_links(self) -> None:
        source = """## List of Figures

- [1.1 First result](#91)
- [2 2(a) apparatus detail ending in 10](#92)
- [2.2 Second result](#93)

## Body

Figure 1.1: First result.

Figure 2.2: Second result.
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)
        anchors = set(re.findall(r'<a id="([^"]+)"', result))
        links = set(re.findall(r"\]\(#([^\s)]+)\)", result))

        self.assertNotRegex(result, r"\]\(#(?:91|92|93)\)")
        self.assertTrue(links <= anchors)
        self.assertEqual(repeated, result)

    def test_plain_figures_and_tables_sections_are_not_navigation(self) -> None:
        source = """## Contents

1. First 1
2. Second 2

## 1. First

Body.

## 2. Second

Body.

## Figures

This section discusses how figures are generated.

## Tables

This section discusses SQL tables and schemas.
"""

        result = enhance_document_navigation(source)

        self.assertIn("## Figures\n\nThis section discusses how figures are generated.", result)
        self.assertIn("## Tables\n\nThis section discusses SQL tables and schemas.", result)

    def test_empty_navigation_section_keeps_its_original_body(self) -> None:
        source = """## Contents

1. First 1
2. Second 2

## 1. First

Body.

## 2. Second

Body.

## List of Figures

No numbered figures are included in this document.
"""

        result = enhance_document_navigation(source)

        self.assertIn("## List of Figures", result)
        self.assertIn("No numbered figures are included in this document.", result)

    def test_user_navigation_like_markup_is_preserved(self) -> None:
        source = """<a id="1"></a>
## User Numeric Landmark

<a id="toc"></a>
## User Toc Landmark

[↑ Personal note](#toc)

## Contents

1. Alpha 1
2. Beta 2

## 1. Alpha

Body.

## 2. Beta

Body.
"""

        result = enhance_document_navigation(source)

        self.assertIn('<a id="1"></a>\n## User Numeric Landmark', result)
        self.assertIn('<a id="toc"></a>\n## User Toc Landmark', result)
        self.assertIn("[↑ Personal note](#toc)", result)

    def test_all_user_ids_on_same_line_are_reserved(self) -> None:
        source = """<span id="custom"></span><span id="1"></span>

## Contents

1. Alpha 1
2. Beta 2

## 1. Alpha

## 2. Beta
"""

        result = enhance_document_navigation(source)

        self.assertIn('<span id="custom"></span><span id="1"></span>', result)
        self.assertNotIn('<a id="1" data-pdf2md-nav="target"></a>', result)
        self.assertIn('<a id="2" data-pdf2md-nav="target"></a>', result)

    def test_existing_caption_heading_is_reused_without_releveling(self) -> None:
        source = """## List of Figures

Figure 1.1 Existing caption 3
Figure 1.2 Generated caption 4

## Body

##### Figure 1.1
Figure 1.1: Existing caption.

Figure 1.2: Generated caption.
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        self.assertRegex(result, r"(?m)^##### Figure 1\.1$")
        self.assertNotRegex(result, r"(?m)^###### Figure 1\.1$")
        self.assertIn("Figure 1.1: Existing caption.", result)
        self.assertEqual(repeated, result)

    def test_explicit_heading_levels_are_preserved(self) -> None:
        source = """## Contents

1. Alpha 1
2.1 Beta 2

# 1. Alpha

Body.

#### 2.1 Beta

Body.
"""

        result = enhance_document_navigation(source)

        self.assertRegex(result, r"(?m)^# 1\. Alpha$")
        self.assertRegex(result, r"(?m)^#### 2\.1 Beta$")

    def test_numbered_entry_does_not_link_unnumbered_same_body(self) -> None:
        source = """## Contents

2.1 Methods 10
2.2 Results 11

## Methods

This is not section 2.1.

## 2.2 Results

Body.
"""

        result = enhance_document_navigation(source)

        self.assertIn("- 2.1 Methods", result)
        self.assertNotRegex(result, r"\[2\.1 Methods\]\(#[^)]+\)")
        self.assertRegex(result, r"\[2\.2 Results\]\(#[^)]+\)")

    def test_isolated_chapter_label_can_disambiguate_next_markdown_heading(self) -> None:
        source = """## Contents

2 Methods 10

Chapter 2

# Methods

Body.
"""

        result = enhance_document_navigation(source)

        self.assertRegex(result, r"\[2 Methods\]\(#[^)]+\)")
        self.assertIn("\nChapter 2\n", result)
        self.assertNotIn("# Chapter 2", result)

    def test_main_chapter_cannot_jump_to_same_numbered_appendix_heading(self) -> None:
        source = """## Contents

1 Introduction 1
1.1 Scope 2
A Appendix 10
A.1 Introduction 11

Chapter 1

# Introduction

## 1.1 Scope

# A Appendix

## A.1 Introduction
"""

        result = enhance_document_navigation(source)

        chapter_link = re.search(r"\[1 Introduction\]\(#(?P<anchor>\d+)\)", result)
        appendix_link = re.search(r"\[A\.1 Introduction\]\(#(?P<anchor>\d+)\)", result)
        self.assertIsNotNone(chapter_link)
        self.assertIsNotNone(appendix_link)
        self.assertIn(
            f'<a id="{chapter_link.group("anchor")}" data-pdf2md-nav="target"></a>\n# Introduction',
            result,
        )
        self.assertIn(
            f'<a id="{appendix_link.group("anchor")}" data-pdf2md-nav="target"></a>\n## A.1 Introduction',
            result,
        )
        self.assertRegex(result, r"\[1\.1 Scope\]\(#\d+\)")

    def test_table_list_rejects_explicit_figure_entry(self) -> None:
        source = """## List of Tables

Figure 1 Results 3
Table 2 Other results 4

## Body

Table 1: Results.

Table 2: Other results.
"""

        result = enhance_document_navigation(source)

        self.assertIn("- Figure 1 Results", result)
        self.assertNotRegex(result, r"\[Figure 1 Results\]\(#[^)]+\)")
        self.assertRegex(result, r"\[Table 2 Other results\]\(#[^)]+\)")

    def test_duplicate_caption_number_and_title_are_not_guessed(self) -> None:
        source = """## List of Figures

1.1 Example 3

## Body

Figure 1.1: Example.

Figure 1.1: Example.
"""

        result = enhance_document_navigation(source)

        self.assertNotRegex(result, r"\[1\.1 Example\]\(#\d+\)")
        self.assertNotRegex(result, r"(?m)^###### Figure 1\.1$")

    def test_repeated_table_caption_links_only_with_continuation_evidence(self) -> None:
        source = """## List of Tables

A.1 Historical experiments 3

## Appendix A

Table A.1 – Continued on next page
Table A.1: Historical experiments.

| Year | Result |
| --- | --- |
| 2000 | A |

Table A.1 – Continued from previous page

| Year | Result |
| --- | --- |
| 2001 | B |

Table A.1: Historical experiments.
"""

        result = enhance_document_navigation(source)

        self.assertRegex(result, r"\[A\.1 Historical experiments\]\(#\d+\)")
        self.assertEqual(result.count("###### Table A.1\n"), 1)

    def test_subfigure_sentence_is_not_misread_as_a_shorter_caption(self) -> None:
        debris = "1.1 Broken list " + ". " * 300
        source = f"""## List of Figures

{debris}

## Body

Figure 9.1(b) plots the variation of the measured signal.

Figure 9.1: Real figure caption.
"""

        result = enhance_document_navigation(source)

        self.assertIn("Figure 9.1(b) plots the variation", result)
        self.assertNotIn("###### Figure 9\n", result)
        self.assertEqual(result.count("###### Figure 9.1\n"), 1)

    def test_caption_title_may_start_with_a_number(self) -> None:
        source = """## List of Figures

2.1 3-Step Cycle 5

## Body

Figure 2.1: 3-Step Cycle for the instrument.
"""

        result = enhance_document_navigation(source)

        self.assertRegex(result, r"\[2\.1 3-Step Cycle\]\(#\d+\)")
        self.assertIn("###### Figure 2.1\n", result)

    def test_tilde_fenced_caption_is_not_promoted(self) -> None:
        source = """## List of Figures

Figure 1.1 Example 3
Figure 1.2 Other example 4

## Code Example

~~~text
Figure 1.1: Example
~~~
"""

        result = enhance_document_navigation(source)

        self.assertIn("~~~text\nFigure 1.1: Example\n~~~", result)
        self.assertNotRegex(result, r"(?m)^###### Figure 1\.1$")

    def test_long_fence_is_not_closed_by_a_shorter_marker(self) -> None:
        source = """## List of Figures

Figure 1.1 Code sample 3
Figure 1.2 Real sample 4

````markdown
Figure 1.1: Code sample.
```
still inside the four-backtick fence
````

Figure 1.2: Real sample.
"""

        result = enhance_document_navigation(source)

        self.assertNotRegex(result, r"(?m)^###### Figure 1\.1$")
        self.assertRegex(result, r"\[Figure 1\.2 Real sample\]\(#\d+\)")
        self.assertIn("still inside the four-backtick fence\n````", result)

    def test_code_fence_headings_are_neither_navigation_nor_targets(self) -> None:
        source = """## Contents

1. Fake 1
2. HTML Fake 2
3. Real 3

```markdown
## Contents
## 1. Fake
<a id="91" data-pdf2md-nav="target" data-pdf2md-heading="generated"></a>
###### Figure 9.1
[↑ List of Figures](#list-of-figures)
```

<pre>
## 2. HTML Fake
</pre>

## 3. Real

Body.
"""

        result = enhance_document_navigation(source)

        self.assertIn(
            '<a id="91" data-pdf2md-nav="target" data-pdf2md-heading="generated"></a>\n'
            "###### Figure 9.1\n[↑ List of Figures](#list-of-figures)\n```",
            result,
        )
        self.assertNotRegex(result, r"\[1\. Fake\]\(#\d+\)")
        self.assertNotRegex(result, r"\[2\. HTML Fake\]\(#\d+\)")
        self.assertRegex(result, r"\[3\. Real\]\(#\d+\)")

    def test_duplicate_exact_markdown_headings_are_not_guessed(self) -> None:
        source = """## Contents

2.1 Methods 10
2.2 Results 11

## 2.1 Methods

First.

## 2.1 Methods

Second.

## 2.2 Results
"""

        result = enhance_document_navigation(source)

        self.assertNotRegex(result, r"\[2\.1 Methods\]\(#\d+\)")
        self.assertRegex(result, r"\[2\.2 Results\]\(#\d+\)")

    def test_punctuation_stripping_cannot_turn_section_1_1_into_11(self) -> None:
        source = """## Contents

1.1 Methods 10
1.2 Results 11

## 11 Methods

## 1.2 Results
"""

        result = enhance_document_navigation(source)

        self.assertNotRegex(result, r"\[1\.1 Methods\]\(#\d+\)")
        self.assertRegex(result, r"\[1\.2 Results\]\(#\d+\)")

    def test_normalized_exact_titles_win_before_prefix_heuristics(self) -> None:
        source = """## Acknowledgements

## Contents

A ck n ow l e d g e m e nt s ix
10.1Summary 10

## 10.1 Summary
"""

        result = enhance_document_navigation(source)

        self.assertRegex(result, r"\[Acknowledgements\]\(#\d+\)")
        self.assertRegex(result, r"\[10\.1 Summary\]\(#\d+\)")

    def test_front_entry_can_fall_back_to_a_unique_heading_after_contents(self) -> None:
        source = """## Contents

Acknowledgements ix
1 Start 1

## Acknowledgements

## 1 Start
"""

        result = enhance_document_navigation(source)

        self.assertRegex(result, r"\[Acknowledgements\]\(#\d+\)")
        self.assertRegex(result, r"\[1 Start\]\(#\d+\)")

    def test_unique_appendix_container_links_to_expanded_heading(self) -> None:
        source = """## Contents

APPENDIX A 100
1. Start 1

## 1. Start

## APPENDIX A SODIUM MATRIX ELEMENTS
"""

        result = enhance_document_navigation(source)

        self.assertRegex(
            result,
            r"\[APPENDIX A SODIUM MATRIX ELEMENTS\]\(#\d+\)",
        )

    def test_lettered_roman_and_chinese_appendix_headings_link(self) -> None:
        source = """## Contents

A. Supplement 10
I. Introduction 11
II. Methods 12
附录 B 数据 13

## A. Supplement

## I. Introduction

## II. Methods

## 附录 B 数据
"""

        result = enhance_document_navigation(source)

        self.assertRegex(result, r"\[A\. Supplement\]\(#\d+\)")
        self.assertRegex(result, r"\[I\. Introduction\]\(#\d+\)")
        self.assertRegex(result, r"\[II\. Methods\]\(#\d+\)")
        self.assertRegex(result, r"\[附录 B 数据\]\(#\d+\)")

    def test_combined_figure_table_list_links_each_caption_type(self) -> None:
        source = """## 图表目录

Figure 1.1 Optical path 3
Table 1.1 Parameters 4

Figure 1.1: Optical path.

Table 1.1: Parameters.
"""

        result = enhance_document_navigation(source)

        self.assertRegex(result, r"\[Figure 1\.1 Optical path\]\(#\d+\)")
        self.assertRegex(result, r"\[Table 1\.1 Parameters\]\(#\d+\)")
        self.assertEqual(
            result.count("[↑ 图表目录](#list-of-figures-and-tables)"),
            2,
        )

    def test_combined_list_continuation_keeps_table_routing(self) -> None:
        source = """## List of Figures and Tables

Figure 1.1 Optical path 3

## List of Figures and Tables (continued)

Table 1.1 Parameters 4

Figure 1.1: Optical path.

Table 1.1: Parameters.
"""

        result = enhance_document_navigation(source)

        self.assertEqual(result.count("## List of Figures and Tables"), 1)
        self.assertRegex(result, r"\[Figure 1\.1 Optical path\]\(#\d+\)")
        self.assertRegex(result, r"\[Table 1\.1 Parameters\]\(#\d+\)")

    def test_contents_can_link_to_combined_figure_table_list(self) -> None:
        source = """## Contents

List of Figures and Tables 3
1 Start 4

## List of Figures and Tables

Figure 1.1 Example 4

## 1 Start

Figure 1.1: Example.
"""

        result = enhance_document_navigation(source)

        self.assertRegex(
            result,
            r"\[List of Figures and Tables\]\(#list-of-figures-and-tables(?:-\d+)?\)",
        )

    def test_corrupt_combined_list_rebuilds_same_number_figure_and_table(self) -> None:
        debris = "corrupt " + ". " * 300
        source = f"""## 图表目录

{debris}

## Body

Figure 1.1: Optical path.

Table 1.1: Parameters.
"""

        result = enhance_document_navigation(source)

        self.assertRegex(result, r"\[1\.1 Optical path\.\]\(#\d+\)")
        self.assertRegex(result, r"\[1\.1 Parameters\.\]\(#\d+\)")

    def test_caption_heading_shared_by_contents_and_figure_list_uses_one_anchor(self) -> None:
        source = """## Contents

Figure 1.1: Example 3

## List of Figures

Figure 1.1 Example 3

##### Figure 1.1: Example
"""

        result = enhance_document_navigation(source)
        content_link = re.search(r"\[Figure 1\.1: Example\]\(#(?P<id>\d+)\)", result)
        figure_link = re.search(r"\[Figure 1\.1 Example\]\(#(?P<id>\d+)\)", result)

        self.assertIsNotNone(content_link)
        self.assertIsNotNone(figure_link)
        self.assertEqual(content_link.group("id"), figure_link.group("id"))
        self.assertEqual(
            result.count(
                f'<a id="{content_link.group("id")}" data-pdf2md-nav="target"></a>'
            ),
            1,
        )

    def test_contents_can_link_to_list_of_figures(self) -> None:
        source = """## Contents

List of Figures 3
1. Start 4

## List of Figures

Figure 1.1 Example 4

## 1. Start

Body.

Figure 1.1: Example.
"""

        result = enhance_document_navigation(source)

        self.assertRegex(
            result,
            r"\[List of Figures\]\(#list-of-figures(?:-\d+)?\)",
        )


    def test_mixed_legacy_bullet_and_parallel_page_column_are_rebuilt(self) -> None:
        source = """## Contents

- [Abstract](#legacy)

Preface
Acknowledgements
1 Introduction
1.1 Scope
1.2 Methods
2 Results
2.1 Data
iv
v
vi
1
2
3
4
5

## List of Figures

1.1 Example 6

## Abstract

## Preface

## Acknowledgements

## 1 Introduction

### 1.1 Scope

### 1.2 Methods

## 2 Results

### 2.1 Data
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)
        contents = result.split("## Contents", 1)[1].split("## List of Figures", 1)[0]

        self.assertEqual(result, repeated)
        self.assertEqual(len(re.findall(r"(?m)^\s*- ", contents)), 8)
        self.assertNotRegex(contents, r"(?m)^(?:iv|v|vi|[1-5])$")
        self.assertNotIn("#legacy", result)
        self.assertIn("[2.1 Data](#", contents)

    def test_page_less_contents_can_end_at_the_first_body_heading(self) -> None:
        source = """## Contents

Abstract
1 Introduction
1.1 Scope
1.2 Methods
2 Results
2.1 Data
iv
1
2
3
4
5

## Abstract

## 1 Introduction

### 1.1 Scope

### 1.2 Methods

## 2 Results

### 2.1 Data
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)
        contents = result.split("## Contents", 1)[1].split("## Abstract", 1)[0]

        self.assertEqual(result, repeated)
        self.assertEqual(len(re.findall(r"(?m)^\s*- ", contents)), 6)
        self.assertNotRegex(contents, r"(?m)^(?:iv|[1-5])$")

    def test_bulletized_page_column_is_discarded(self) -> None:
        source = """## Contents

- Abstract
- 1 Introduction
- 1.1 Scope
- 1.2 Methods
- 2 Results
- 2.1 Data
- iv
- 1
- 2
- 3
- 4
- 5

## Abstract

## 1 Introduction

### 1.1 Scope

### 1.2 Methods

## 2 Results

### 2.1 Data
"""

        result = enhance_document_navigation(source)
        contents = result.split("## Contents", 1)[1].split("## Abstract", 1)[0]

        self.assertEqual(len(re.findall(r"(?m)^\s*- ", contents)), 6)
        self.assertNotRegex(contents, r"(?m)^\s*- (?:iv|[1-5])$")

    def test_page_less_outline_without_layout_evidence_is_not_consumed(self) -> None:
        outline = """1 Introduction
1.1 Scope
1.2 Methods
2 Results
2.1 Data
2.2 Discussion
Ordinary body prose must remain.
"""
        source = f"""## Contents

- Abstract

{outline}
## List of Figures

1.1 Example 6
"""

        result = enhance_document_navigation(source)

        self.assertIn(outline, result)
        self.assertEqual(result.count("Ordinary body prose must remain."), 1)

    def test_pathological_leader_row_keeps_its_structured_title(self) -> None:
        debris = "1.4 Matter Wave Lensing " + ". " * 300
        source = f"""## Contents

- Abstract
Preface
1 Introduction 1
1.1 Scope 2
1.2 Methods 3
1.3 Results 4
{debris}
1.5 Summary 6

## List of Figures

1.1 Example 7
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        self.assertEqual(result, repeated)
        self.assertIn("- 1.4 Matter Wave Lensing", result)
        self.assertNotIn(debris, result)
        self.assertIn("- 1.5 Summary", result)

    def test_parallel_page_columns_are_paired_only_when_counts_match(self) -> None:
        titles = ["Abstract", "1 Introduction", "1.1 Scope"]

        self.assertEqual(
            toc._pair_parallel_page_column(titles + ["iv", "1", "2"], "contents"),
            ["Abstract iv", "1 Introduction 1", "1.1 Scope 2"],
        )
        self.assertEqual(
            toc._pair_parallel_page_column(titles + ["iv", "1"], "contents"),
            titles + ["iv", "1"],
        )


class FrontMatterParsingSafetyTests(unittest.TestCase):
    def test_repeated_navigation_headers_with_page_labels_are_ignored(self) -> None:
        entries = parse_entry_lines(
            ["vi Contents", "Contents vii", "1 Alpha 1"],
            "contents",
        )

        self.assertEqual(
            [(entry.title, entry.page) for entry in entries],
            [("1 Alpha", "1")],
        )

    def test_repeated_navigation_header_does_not_merge_neighboring_entries(self) -> None:
        entries = parse_entry_lines(
            ["6.4 Results 124", "vi Contents", "7.1 Discussion 133"],
            "contents",
        )

        self.assertEqual(
            [(entry.title, entry.page) for entry in entries],
            [("6.4 Results", "124"), ("7.1 Discussion", "133")],
        )

    def test_multilingual_abstract_and_thesis_organization_are_separate_entries(self) -> None:
        entries = parse_entry_lines(
            [
                "Abstract, Résumé, Riassunto i",
                "Introduction 1",
                "Organization of thesis 4",
            ],
            "contents",
        )

        self.assertEqual(
            [(entry.title, entry.page) for entry in entries],
            [
                ("Abstract, Résumé, Riassunto", "i"),
                ("Introduction", "1"),
                ("Organization of thesis", "4"),
            ],
        )

    def test_consecutive_unnumbered_entries_keep_their_pages(self) -> None:
        entries = parse_entry_lines(
            ["Abstract i", "Acknowledgements ii", "1. Introduction 1"],
            "contents",
        )

        self.assertEqual(
            [(entry.title, entry.page) for entry in entries],
            [
                ("Abstract", "i"),
                ("Acknowledgements", "ii"),
                ("1. Introduction", "1"),
            ],
        )

    def test_common_front_matter_lists_and_precis_are_separate_entries(self) -> None:
        entries = parse_entry_lines(
            [
                "List of Publications iv",
                "List of Symbols v",
                "List of Abbreviations vi",
                "Précis of the thesis vii",
                "1 Introduction 1",
            ],
            "contents",
        )

        self.assertEqual(
            [(entry.title, entry.page) for entry in entries],
            [
                ("List of Publications", "iv"),
                ("List of Symbols", "v"),
                ("List of Abbreviations", "vi"),
                ("Précis of the thesis", "vii"),
                ("1 Introduction", "1"),
            ],
        )

    def test_technical_number_is_not_reinterpreted_as_page(self) -> None:
        entries = parse_entry_lines(["2.4 ISO9001", "2.5 ISO 9001"], "contents")

        self.assertEqual(
            [(entry.title, entry.page) for entry in entries],
            [("2.4 ISO9001", ""), ("2.5 ISO 9001", "")],
        )

    def test_spaced_page_labels_and_pathological_lines_are_handled(self) -> None:
        entries = parse_entry_lines(
            [
                "x" * 5000,
                "Acknowledgements i x",
                "4 Apparatus 1 1 4",
            ],
            "contents",
        )

        self.assertEqual(
            [(entry.title, entry.page) for entry in entries],
            [("Acknowledgements", "ix"), ("4 Apparatus", "114")],
        )

    def test_two_layout_columns_split_only_between_complete_entries(self) -> None:
        entries = parse_entry_lines(
            ["1 Introduction  1     5 Conclusion  10"],
            "contents",
        )

        self.assertEqual(
            [(entry.title, entry.page) for entry in entries],
            [("1 Introduction", "1"), ("5 Conclusion", "10")],
        )

    def test_multiple_layout_rows_are_read_down_columns(self) -> None:
        entries = parse_entry_lines(
            [
                "1 Alpha  1     3 Gamma  3",
                "2 Beta  2     4 Delta  4",
            ],
            "contents",
        )

        self.assertEqual(
            [entry.title for entry in entries],
            ["1 Alpha", "2 Beta", "3 Gamma", "4 Delta"],
        )

    def test_spaced_section_number_is_not_a_false_column_boundary(self) -> None:
        entries = parse_entry_lines(
            ["5 . 4 . 1          M i c r owave Generation          . . . .          4 7"],
            "contents",
        )

        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].title.startswith("5.4.1"))
        self.assertEqual(entries[0].page, "47")

    def test_continuation_without_page_labels_cannot_absorb_body_headings(self) -> None:
        class FakePage:
            def __init__(self, text: str) -> None:
                self.text = text

            def extract_text(self, extraction_mode: str | None = None) -> str:
                return self.text

        reader = mock.Mock(
            pages=[
                FakePage("Contents\n1 Alpha 1\n2 Beta 2\n3 Gamma 3"),
                FakePage("1 Introduction\n1.1 Background\n1.2 Details"),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(frontmatter, "PdfReader", return_value=reader):
                result = extract_front_matter(source)

        self.assertEqual(
            [entry.title for entry in result["contents"].entries],
            ["1 Alpha", "2 Beta", "3 Gamma"],
        )

    def test_frontmatter_cache_reuses_even_empty_or_expensive_results(self) -> None:
        class FakePage:
            def extract_text(self, extraction_mode: str | None = None) -> str:
                return "Contents\n1 Alpha 1\n2 Beta 2"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.pdf"
            cache = root / "raw" / "cache" / "frontmatter-v6.json"
            source.write_bytes(b"pdf")
            with mock.patch.object(
                frontmatter,
                "PdfReader",
                return_value=mock.Mock(pages=[FakePage()]),
            ) as reader:
                first = extract_front_matter(source, cache_path=cache)
            with mock.patch.object(
                frontmatter,
                "PdfReader",
                side_effect=AssertionError("cache miss"),
            ):
                second = extract_front_matter(source, cache_path=cache)

        self.assertEqual(first, second)
        self.assertEqual(reader.call_count, 1)

    def test_force_bypasses_and_replaces_frontmatter_cache(self) -> None:
        class FakePage:
            def __init__(self, text: str) -> None:
                self.text = text

            def extract_text(self, extraction_mode: str | None = None) -> str:
                return self.text

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.pdf"
            cache = root / "raw" / "cache" / "frontmatter-v6.json"
            source.write_bytes(b"pdf")
            with mock.patch.object(
                frontmatter,
                "PdfReader",
                return_value=mock.Mock(pages=[FakePage("Contents\n1 Old 1\n2 End 2")]),
            ):
                first = extract_front_matter(source, cache_path=cache)
            with mock.patch.object(
                frontmatter,
                "PdfReader",
                return_value=mock.Mock(pages=[FakePage("Contents\n1 New 1\n2 End 2")]),
            ) as reader:
                refreshed = extract_front_matter(source, cache_path=cache, force=True)

        self.assertEqual(first["contents"].entries[0].title, "1 Old")
        self.assertEqual(refreshed["contents"].entries[0].title, "1 New")
        self.assertEqual(reader.call_count, 1)

    def test_pdf_reader_failure_does_not_create_a_valid_empty_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.pdf"
            cache = root / "raw" / "cache" / "frontmatter-v6.json"
            source.write_bytes(b"not a pdf")
            with mock.patch.object(frontmatter, "PdfReader", side_effect=ValueError("bad")):
                result = extract_front_matter(source, cache_path=cache)

            self.assertEqual(result, {})
            self.assertFalse(cache.exists())

    def test_structurally_malformed_cache_is_treated_as_a_miss(self) -> None:
        class FakePage:
            def extract_text(self, extraction_mode: str | None = None) -> str:
                return "Contents\n1 Alpha 1\n2 Beta 2"

        malformed_payloads = [
            "[]\n",
            '{"version": 6, "source": [], "max_pages": 64, "sections": []}\n',
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.pdf"
            cache = root / "raw" / "cache" / "frontmatter-v6.json"
            source.write_bytes(b"pdf")
            cache.parent.mkdir(parents=True)
            for payload in malformed_payloads:
                with self.subTest(payload=payload):
                    cache.write_text(payload, encoding="utf-8")
                    with mock.patch.object(
                        frontmatter,
                        "PdfReader",
                        return_value=mock.Mock(pages=[FakePage()]),
                    ):
                        result = extract_front_matter(source, cache_path=cache)
                    self.assertEqual(
                        [entry.title for entry in result["contents"].entries],
                        ["1 Alpha", "2 Beta"],
                    )


if __name__ == "__main__":
    unittest.main()
