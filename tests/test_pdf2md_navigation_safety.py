from __future__ import annotations

import json
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

    def test_native_contents_atomically_replace_number_lost_raw_tail(self) -> None:
        source_content = """## Contents

- [1 Introduction](#legacy-1)
  - [1.1 Background](#legacy-2)
- [2 Apparatus](#legacy-3)
  - [2.1 Apparatus overview](#legacy-4)
- [3 Analysis](#legacy-5)
- [Bibliography](#legacy-6)

Apparatus 28
2.1 Apparatus overview 28
3 Analysis 50
Bibliography 157

1 Windows 10
Actual body prose must remain.

## List of Tables

1.1 Parameters 10

## 1 Introduction

## 1.1 Background

## 2 Apparatus

## 2.1 Apparatus overview

## 3 Analysis

## Bibliography
"""
        native = {
            "contents": frontmatter.FrontMatterSection(
                kind="contents",
                title="Contents",
                entries=tuple(
                    frontmatter.FrontMatterEntry("contents", title, page)
                    for title, page in (
                        ("1 Introduction", "1"),
                        ("1.1 Background", "2"),
                        ("2 Apparatus", "28"),
                        ("2.1 Apparatus overview", "28"),
                        ("3 Analysis", "50"),
                        ("Bibliography", "157"),
                    )
                ),
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(toc, "extract_front_matter", return_value=native):
                result = enhance_document_navigation(source_content, source=source)
                repeated = enhance_document_navigation(result, source=source)

        contents = result.split("## Contents", 1)[1].split("## List of Tables", 1)[0]
        self.assertEqual(repeated, result)
        for title in (
            "1 Introduction",
            "1.1 Background",
            "2 Apparatus",
            "2.1 Apparatus overview",
            "3 Analysis",
            "Bibliography",
        ):
            with self.subTest(title=title):
                self.assertRegex(contents, rf"\[{re.escape(title)}\]\(#\d+\)")
        for raw_line in (
            "Apparatus 28",
            "2.1 Apparatus overview 28",
            "3 Analysis 50",
            "Bibliography 157",
        ):
            with self.subTest(raw_line=raw_line):
                self.assertNotRegex(contents, rf"(?m)^{re.escape(raw_line)}$")
        self.assertIn(
            "\n1 Windows 10\nActual body prose must remain.\n",
            result,
        )

    def test_body_context_recovers_missing_scanned_chapter_numbers(self) -> None:
        source = """## Contents

- [1 Introduction](#legacy-1)
  - [1.1 Scope](#legacy-2)
- [2 Setup](#legacy-3)
- [3 Control](#legacy-4)
    - [3.5.2 Frequency control](#legacy-5)

3.5.3 Tilt control 73
Experimental Results 76
4.1 Measurement 77
Noise 85
5.1 Sources 86
Systematic errors 105
6.1 Gravity gradient 106
Conclusion 156
7.1 Summary 157
Bibliography 170

## List of Tables

1.1 Parameters 10

## Chapter 1
## Introduction
## 1.1 Scope
## Chapter 2
## Setup
## Chapter 3
## Control
## 3.5.2 Frequency control
## 3.5.3 Tilt control
## Chapter 4
## Experimental Results
## 4.1 Measurement
## Chapter 5
## Noise
## 5.1 Sources
## Chapter 6
## Systematic errors
## 6.1 Gravity gradient
## Chapter 7
## Conclusion
## 7.1 Summary
## Bibliography
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        contents = result.split("## Contents", 1)[1].split("## List of Tables", 1)[0]
        self.assertEqual(repeated, result)
        for title in (
            "3.5.3 Tilt control",
            "4 Experimental Results",
            "4.1 Measurement",
            "5 Noise",
            "5.1 Sources",
            "6 Systematic errors",
            "6.1 Gravity gradient",
            "7 Conclusion",
            "7.1 Summary",
            "Bibliography",
        ):
            with self.subTest(title=title):
                self.assertRegex(contents, rf"\[{re.escape(title)}\]\(#\d+\)")
        for raw_line in (
            "3.5.3 Tilt control 73",
            "Experimental Results 76",
            "4.1 Measurement 77",
            "Noise 85",
            "5.1 Sources 86",
            "Systematic errors 105",
            "6.1 Gravity gradient 106",
            "Conclusion 156",
            "7.1 Summary 157",
            "Bibliography 170",
        ):
            with self.subTest(raw_line=raw_line):
                self.assertNotRegex(contents, rf"(?m)^{re.escape(raw_line)}$")
        self.assertNotIn('data-pdf2md-heading="generated"', result)
        for chapter, title in (
            (4, "Experimental Results"),
            (5, "Noise"),
            (6, "Systematic errors"),
            (7, "Conclusion"),
        ):
            with self.subTest(chapter=chapter):
                self.assertRegex(
                    result,
                    rf"## Chapter {chapter}\n"
                    rf'<a id="\d+" data-pdf2md-nav="target"></a>\n'
                    rf"## {re.escape(title)}",
                )

    def test_high_confidence_contents_tail_allows_one_unmatched_entry(self) -> None:
        source = """## Contents

- [1 Introduction](#legacy-1)
- [2 Setup](#legacy-2)
- [3 Control](#legacy-3)

3.1 Tilt control 30
4 Results 40
4.1 Measurement 41
4.2 Verification 42
Independent Appendix 45
5 Noise 50
5.1 Sources 51
Bibliography 90

## List of Tables

1.1 Parameters 10

## 1 Introduction
## 2 Setup
## 3 Control
## 3.1 Tilt control
## 4 Results
## 4.1 Measurement
## 4.2 Verification
## 5 Noise
## 5.1 Sources
## Bibliography
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        contents = result.split("## Contents", 1)[1].split("## List of Tables", 1)[0]
        self.assertEqual(repeated, result)
        self.assertEqual(len(re.findall(r"(?m)^\s*- ", contents)), 11)
        for title in (
            "3.1 Tilt control",
            "4 Results",
            "4.1 Measurement",
            "4.2 Verification",
            "5 Noise",
            "5.1 Sources",
            "Bibliography",
        ):
            with self.subTest(title=title):
                self.assertRegex(contents, rf"\[{re.escape(title)}\]\(#\d+\)")
        self.assertRegex(contents, r"(?m)^\s*- Independent Appendix$")
        self.assertNotIn("[Independent Appendix]", contents)
        raw_tail = {
            "3.1 Tilt control 30",
            "4 Results 40",
            "4.1 Measurement 41",
            "4.2 Verification 42",
            "Independent Appendix 45",
            "5 Noise 50",
            "5.1 Sources 51",
            "Bibliography 90",
        }
        self.assertFalse(raw_tail & set(contents.splitlines()))

    def test_dot_leader_backmatter_tail_before_second_contents_is_owned_atomically(self) -> None:
        lines = [
            "## 目录",
            "- Existing entry",
            "致谢....139",
            "个人简况及联系方式....140",
            "承诺书....141",
            "学位论文使用授权声明....142",
            "",
            "## Contents",
        ]
        first = toc.NavSection(
            start=0,
            end=2,
            kind="contents",
            title="目录",
            entries=[
                toc.NavEntry(
                    kind="contents",
                    title="Acknowledgment 139 Personal profile 140 Letter of commitment 141 Authorization statement",
                    page="142",
                    depth=0,
                    structured=True,
                )
            ],
        )
        second = toc.NavSection(
            start=7,
            end=8,
            kind="contents",
            title="Contents",
        )

        toc._extend_body_backed_section_ranges(lines, [first, second])
        toc._refresh_section_entries(lines, [first, second])

        self.assertEqual(first.end, 7)
        self.assertEqual(first.owned_tail_start, 2)
        self.assertEqual(
            [(entry.title, entry.page) for entry in first.entries[-4:]],
            [
                ("致谢", "139"),
                ("个人简况及联系方式", "140"),
                ("承诺书", "141"),
                ("学位论文使用授权声明", "142"),
            ],
        )

    def test_dot_leader_tail_with_decreasing_pages_is_not_owned_without_evidence(self) -> None:
        lines = [
            "## Contents",
            "- Existing entry",
            "First note....140",
            "Second note....139",
            "Third note....141",
            "",
            "## List of Figures",
        ]
        first = toc.NavSection(
            start=0,
            end=2,
            kind="contents",
            title="Contents",
            entries=[],
        )
        second = toc.NavSection(
            start=6,
            end=7,
            kind="figures",
            title="List of Figures",
        )

        toc._extend_body_backed_section_ranges(lines, [first, second])

        self.assertEqual(first.end, 2)
        self.assertEqual(first.owned_tail_start, -1)
        self.assertEqual(first.owned_tail_records, [])

    def test_contents_without_following_navigation_ends_at_first_body_heading(self) -> None:
        source = """## Contents

- [1 Introduction](#legacy-1)
- [2 Setup](#legacy-2)
- [3 Control](#legacy-3)

3.1 Tilt control 30
Experimental Results 40
4.1 Measurement 41
Noise 50
5.1 Sources 51
Bibliography 90

## 1 Introduction
Body starts here and must remain.
## 2 Setup
## 3 Control
## 3.1 Tilt control
## Chapter 4
## Experimental Results
## 4.1 Measurement
## Chapter 5
## Noise
## 5.1 Sources
## Bibliography
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        contents = result.split("## Contents", 1)[1].split("## 1 Introduction", 1)[0]
        self.assertEqual(repeated, result)
        for title in (
            "1 Introduction",
            "2 Setup",
            "3 Control",
            "3.1 Tilt control",
            "4 Experimental Results",
            "4.1 Measurement",
            "5 Noise",
            "5.1 Sources",
            "Bibliography",
        ):
            with self.subTest(title=title):
                self.assertRegex(contents, rf"\[{re.escape(title)}\]\(#\d+\)")
        raw_tail = {
            "3.1 Tilt control 30",
            "Experimental Results 40",
            "4.1 Measurement 41",
            "Noise 50",
            "5.1 Sources 51",
            "Bibliography 90",
        }
        self.assertFalse(raw_tail & set(contents.splitlines()))
        self.assertIn("## 1 Introduction", result)
        self.assertEqual(result.count("Body starts here and must remain."), 1)
        self.assertNotIn('data-pdf2md-heading="generated"', result)

    def test_only_contents_owned_tail_is_not_duplicated_as_notes(self) -> None:
        source = """## Contents

- [1 Introduction](#legacy)

Optical Design 10
Signal Recovery 20
Vacuum Hardware 30
Thermal Control 40

## 1 Introduction
## Optical Design
## Signal Recovery
## Vacuum Hardware
## Thermal Control
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        contents = result.split("## Contents", 1)[1].split("## 1 Introduction", 1)[0]
        self.assertEqual(repeated, result)
        for title, page in (
            ("Optical Design", "10"),
            ("Signal Recovery", "20"),
            ("Vacuum Hardware", "30"),
            ("Thermal Control", "40"),
        ):
            with self.subTest(title=title):
                self.assertNotRegex(result, rf"(?m)^{re.escape(title)} {page}$")
                self.assertEqual(
                    len(
                        re.findall(
                            rf"(?m)^\s*- \[{re.escape(title)}\]\(#\d+\)$",
                            contents,
                        )
                    ),
                    1,
                )

    def test_incomplete_native_contents_merge_body_supported_tail(self) -> None:
        source_content = """## Contents

- [1 Introduction](#legacy)

Optical Design 10
Signal Recovery 20
Vacuum Hardware 30
Thermal Control 40

## List of Tables

1.1 Parameters 5

## 1 Introduction
## Optical Design
## Signal Recovery
## Vacuum Hardware
## Thermal Control
"""
        native = {
            "contents": frontmatter.FrontMatterSection(
                kind="contents",
                title="Contents",
                entries=(
                    frontmatter.FrontMatterEntry("contents", "1 Introduction", "1"),
                ),
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(toc, "extract_front_matter", return_value=native):
                result = enhance_document_navigation(source_content, source=source)
                repeated = enhance_document_navigation(result, source=source)

        contents = result.split("## Contents", 1)[1].split("## List of Tables", 1)[0]
        self.assertEqual(repeated, result)
        self.assertEqual(len(re.findall(r"(?m)^\s*- ", contents)), 5)
        for title, page in (
            ("Optical Design", "10"),
            ("Signal Recovery", "20"),
            ("Vacuum Hardware", "30"),
            ("Thermal Control", "40"),
        ):
            with self.subTest(title=title):
                self.assertRegex(contents, rf"\[{re.escape(title)}\]\(#\d+\)")
                self.assertNotRegex(result, rf"(?m)^{re.escape(title)} {page}$")

    def test_year_ending_prefatory_prose_is_not_owned_at_half_support(self) -> None:
        source = """## Contents

- [1 Introduction](#legacy)

Optical Design 2020
Committee Approval 2021
Signal Recovery 2022
Degree Awarded 2023

## List of Tables

1.1 Parameters 5

## Optical Design
Prefatory discussion.
## Signal Recovery
More prefatory discussion.
## 1 Introduction
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        contents = result.split("## Contents", 1)[1].split("## List of Tables", 1)[0]
        self.assertEqual(repeated, result)
        for title, year in (
            ("Optical Design", "2020"),
            ("Committee Approval", "2021"),
            ("Signal Recovery", "2022"),
            ("Degree Awarded", "2023"),
        ):
            with self.subTest(title=title):
                self.assertEqual(result.count(f"{title} {year}"), 1)
                self.assertNotRegex(
                    contents,
                    rf"(?m)^\s*- (?:\[)?{re.escape(title)}(?:\]\(#\d+\))?$",
                )

    def test_same_title_entries_on_different_pages_are_not_deduplicated(self) -> None:
        source = """## Contents

- [Front Matter](#legacy)

Introduction 10
Methods 11
Summary 12
Introduction 20
Methods 21
Summary 22

## List of Tables

1.1 Parameters 5

## Chapter 1
## Introduction
## Methods
## Summary
## Chapter 2
## Introduction
## Methods
## Summary
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        contents = result.split("## Contents", 1)[1].split("## List of Tables", 1)[0]
        self.assertEqual(repeated, result)
        self.assertEqual(
            len(re.findall(r"(?m)^\s*- Front Matter$", contents)),
            1,
        )
        self.assertNotRegex(contents, r"\]\(#\d+\)")
        for title in ("Introduction", "Methods", "Summary"):
            with self.subTest(title=title):
                bullets = re.findall(
                    rf"(?m)^\s*- (?:\[{re.escape(title)}\]\(#\d+\)|"
                    rf"{re.escape(title)})$",
                    contents,
                )
                self.assertEqual(len(bullets), 2)
        for raw_line in (
            "Introduction 10",
            "Methods 11",
            "Summary 12",
            "Introduction 20",
            "Methods 21",
            "Summary 22",
        ):
            with self.subTest(raw_line=raw_line):
                self.assertNotRegex(result, rf"(?m)^{re.escape(raw_line)}$")

    def test_partial_native_and_raw_tail_merge_in_document_order(self) -> None:
        source_content = """## Contents

- [1 Introduction](#legacy)

2 Methods 20
3 Results 30
4 Conclusion 40

## List of Tables

1.1 Parameters 5

## 1 Introduction
## 2 Methods
## 3 Results
## 4 Conclusion
"""
        native = {
            "contents": frontmatter.FrontMatterSection(
                kind="contents",
                title="Contents",
                entries=tuple(
                    frontmatter.FrontMatterEntry("contents", title, page)
                    for title, page in (
                        ("1 Introduction", "1"),
                        ("3 Results", "30"),
                        ("4 Conclusion", "40"),
                    )
                ),
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(toc, "extract_front_matter", return_value=native):
                result = enhance_document_navigation(source_content, source=source)
                repeated = enhance_document_navigation(result, source=source)

        contents = result.split("## Contents", 1)[1].split("## List of Tables", 1)[0]
        expected = ("1 Introduction", "2 Methods", "3 Results", "4 Conclusion")
        self.assertEqual(repeated, result)
        self.assertEqual(len(re.findall(r"(?m)^\s*- ", contents)), len(expected))
        positions = []
        for title in expected:
            with self.subTest(title=title):
                self.assertEqual(contents.count(title), 1)
                positions.append(contents.index(title))
        self.assertEqual(positions, sorted(positions))

    def test_number_lost_raw_tail_does_not_duplicate_complete_native_entries(self) -> None:
        source_content = """## Contents

- [1 Introduction](#legacy)

Introduction 1
Methods 10
Results 20
Conclusion 30

## List of Tables

1.1 Parameters 5

## Chapter 1
## Introduction
## Chapter 2
## Methods
## Chapter 3
## Results
## Chapter 4
## Conclusion
"""
        native = {
            "contents": frontmatter.FrontMatterSection(
                kind="contents",
                title="Contents",
                entries=tuple(
                    frontmatter.FrontMatterEntry("contents", title, page)
                    for title, page in (
                        ("1 Introduction", "1"),
                        ("2 Methods", "10"),
                        ("3 Results", "20"),
                        ("4 Conclusion", "30"),
                    )
                ),
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(toc, "extract_front_matter", return_value=native):
                result = enhance_document_navigation(source_content, source=source)
                repeated = enhance_document_navigation(result, source=source)

        contents = result.split("## Contents", 1)[1].split("## List of Tables", 1)[0]
        expected = ("1 Introduction", "2 Methods", "3 Results", "4 Conclusion")
        self.assertEqual(repeated, result)
        self.assertEqual(len(re.findall(r"(?m)^\s*- ", contents)), len(expected))
        for title in expected:
            with self.subTest(title=title):
                self.assertEqual(contents.count(title), 1)
        for raw_line in ("Introduction 1", "Methods 10", "Results 20", "Conclusion 30"):
            with self.subTest(raw_line=raw_line):
                self.assertNotRegex(result, rf"(?m)^{re.escape(raw_line)}$")

    def test_native_owned_raw_tail_without_body_is_not_preserved_as_notes(self) -> None:
        source_content = """## Contents

- [Optical Design](#legacy)

Optical Design 10
Signal Recovery 20
Vacuum Hardware 30
Thermal Control 40

## List of Tables

1.1 Parameters 5
"""
        native = {
            "contents": frontmatter.FrontMatterSection(
                kind="contents",
                title="Contents",
                entries=tuple(
                    frontmatter.FrontMatterEntry("contents", title, page)
                    for title, page in (
                        ("Optical Design", "10"),
                        ("Signal Recovery", "20"),
                        ("Vacuum Hardware", "30"),
                        ("Thermal Control", "40"),
                    )
                ),
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(toc, "extract_front_matter", return_value=native):
                result = enhance_document_navigation(source_content, source=source)
                repeated = enhance_document_navigation(result, source=source)

        contents = result.split("## Contents", 1)[1].split("## List of Tables", 1)[0]
        self.assertEqual(repeated, result)
        self.assertEqual(len(re.findall(r"(?m)^\s*- ", contents)), 4)
        for title, page in (
            ("Optical Design", "10"),
            ("Signal Recovery", "20"),
            ("Vacuum Hardware", "30"),
            ("Thermal Control", "40"),
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    len(re.findall(rf"(?m)^\s*- {re.escape(title)}$", contents)),
                    1,
                )
                self.assertNotRegex(result, rf"(?m)^{re.escape(title)} {page}$")

    def test_leading_article_a_is_not_treated_as_a_section_identifier(self) -> None:
        source = """## Contents

A Study of Light 10
1 Introduction 20

## A Study of Light
Body.

## 1 Introduction
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        self.assertEqual(repeated, result)
        self.assertRegex(result, r"\[A Study of Light\]\(#\d+\)")
        self.assertRegex(
            result,
            r'<a id="\d+" data-pdf2md-nav="target"></a>\n## A Study of Light',
        )
        self.assertNotIn('data-pdf2md-heading="generated"', result)

    def test_year_ending_prefatory_prose_is_not_owned_at_three_quarter_support(self) -> None:
        source = """## Contents

- [1 Introduction](#legacy)

Optical Design 2020
Committee Approval 2021
Signal Recovery 2022
Degree Awarded 2023

## List of Tables

1.1 Parameters 5

## Optical Design
Prefatory discussion.
## Signal Recovery
More prefatory discussion.
## Degree Awarded
Administrative discussion.
## 1 Introduction
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        contents = result.split("## Contents", 1)[1].split("## List of Tables", 1)[0]
        self.assertEqual(repeated, result)
        for title, year in (
            ("Optical Design", "2020"),
            ("Committee Approval", "2021"),
            ("Signal Recovery", "2022"),
            ("Degree Awarded", "2023"),
        ):
            with self.subTest(title=title):
                self.assertEqual(result.count(f"{title} {year}"), 1)
                self.assertNotRegex(
                    contents,
                    rf"(?m)^\s*- (?:\[)?{re.escape(title)}(?:\]\(#\d+\))?$",
                )

    def test_contents_extension_stops_before_real_body_preceding_table_list(self) -> None:
        source = """## Contents

1 Introduction 1
2 Methods 5

1 Introduction
Actual body prose must remain.
This paragraph is not front-matter navigation.

## List of Tables

1.1 Parameters 10
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        self.assertEqual(repeated, result)
        self.assertIn(
            "\n1 Introduction\nActual body prose must remain.\n"
            "This paragraph is not front-matter navigation.\n",
            result,
        )
        self.assertEqual(result.count("Actual body prose must remain."), 1)

    def test_owned_tail_stops_at_numbered_body_restart(self) -> None:
        source = """## Contents

- [1 Introduction](#legacy-1)
- [2 Setup](#legacy-2)
- [3 Control](#legacy-3)

3.1 Tilt control 30
4 Results 40
4.1 Measurement 41
5 Noise 50
5.1 Sources 51
Bibliography 90

1 Windows 10
Actual body prose must remain.

## List of Tables

1.1 Parameters 10

## 1 Introduction
## 2 Setup
## 3 Control
## 3.1 Tilt control
## 4 Results
## 4.1 Measurement
## 5 Noise
## 5.1 Sources
## Bibliography
"""

        result = enhance_document_navigation(source)
        repeated = enhance_document_navigation(result)

        self.assertEqual(repeated, result)
        self.assertIn(
            "\n1 Windows 10\nActual body prose must remain.\n",
            result,
        )
        self.assertEqual(result.count("Actual body prose must remain."), 1)

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


    def test_heading_index_avoids_cartesian_scoring(self) -> None:
        entry_count = 300
        heading_count = 1_800
        entries = "\n".join(
            f"{number} Topic {number} ........ {number}"
            for number in range(1, entry_count + 1)
        )
        headings = "\n\n".join(
            [
                *(
                    f"## {number} Topic {number}"
                    for number in range(1, entry_count + 1)
                ),
                *(
                    f"## {number} Distractor {number}"
                    for number in range(entry_count + 1, heading_count + 1)
                ),
            ]
        )
        source = f"## Contents\n\n{entries}\n\n{headings}\n"

        with mock.patch.object(
            toc, "_heading_score", wraps=toc._heading_score
        ) as scorer:
            result = enhance_document_navigation(source)

        self.assertEqual(result.count("[↑ Contents](#toc)"), entry_count)
        cartesian_scores = entry_count * heading_count
        self.assertLess(scorer.call_count, cartesian_scores // 100)
        self.assertLessEqual(scorer.call_count, entry_count * 3)
        self.assertEqual(enhance_document_navigation(result), result)


class FrontMatterParsingSafetyTests(unittest.TestCase):
    def test_listing_of_figures_is_an_exact_navigation_alias(self) -> None:
        self.assertEqual(frontmatter.navigation_kind("Listing of Figures"), "figures")
        self.assertEqual(frontmatter.navigation_kind("LISTING OF FIGURES"), "figures")
        self.assertIsNone(frontmatter.navigation_kind("Listing"))
        self.assertIsNone(frontmatter.navigation_kind("Listing of Equations"))

    def test_unnumbered_leader_entries_are_not_joined(self) -> None:
        entries = parse_entry_lines(
            [
                "\u7279\u6027 . . . . 1",
                "\u5e94\u7528 . . . . 1",
                "\u529f\u80fd\u6846\u56fe . . . . 2",
            ],
            "contents",
        )

        self.assertEqual(
            [(entry.title, entry.page) for entry in entries],
            [
                ("\u7279\u6027", "1"),
                ("\u5e94\u7528", "1"),
                ("\u529f\u80fd\u6846\u56fe", "2"),
            ],
        )

    def test_long_hyphen_leaders_split_numbered_and_unnumbered_entries(self) -> None:
        entries = parse_entry_lines(
            [
                "Chapter 1 Overview-------------------1",
                "1.1 Experimental method-------------------2",
                "Chapter 2 Results -17",
                "Bibliography -------------------10",
            ],
            "contents",
        )

        self.assertEqual(
            [(entry.title, entry.page) for entry in entries],
            [
                ("Chapter 1 Overview", "1"),
                ("1.1 Experimental method", "2"),
                ("Chapter 2 Results", "17"),
                ("Bibliography", "10"),
            ],
        )

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
            cache = root / "raw" / "cache" / "frontmatter-v7.json"
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
            cache = root / "raw" / "cache" / "frontmatter-v7.json"
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
            cache = root / "raw" / "cache" / "frontmatter-v7.json"
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
            '{"version": 7, "source": [], "max_pages": 64, "sections": []}\n',
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.pdf"
            cache = root / "raw" / "cache" / "frontmatter-v7.json"
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

    def test_selected_native_cache_is_bound_to_physical_page_provenance(self) -> None:
        class FakePage:
            def __init__(self, text: str) -> None:
                self.text = text

            def extract_text(self, extraction_mode: str | None = None) -> str:
                return self.text

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.pdf"
            cache = root / "raw" / "cache" / "frontmatter-v8.json"
            source.write_bytes(b"pdf")
            first_reader = mock.Mock(
                pages=[
                    FakePage("Contents\n1 Alpha 1\n2 Beta 2"),
                    FakePage("List of Tables\n1.1 First table 7\n1.2 Second table 8"),
                ]
            )
            with mock.patch.object(
                frontmatter, "PdfReader", return_value=first_reader
            ):
                tables = extract_front_matter(
                    source,
                    cache_path=cache,
                    physical_pages={2},
                )
            second_reader = mock.Mock(
                pages=[
                    FakePage("Contents\n1 Alpha 1\n2 Beta 2"),
                    FakePage("List of Tables\n1.1 First table 7\n1.2 Second table 8"),
                ]
            )
            with mock.patch.object(
                frontmatter, "PdfReader", return_value=second_reader
            ) as reader:
                contents = extract_front_matter(
                    source,
                    cache_path=cache,
                    physical_pages={1},
                )

        self.assertEqual(
            {entry.physical_page for entry in tables["tables"].entries},
            {2},
        )
        self.assertEqual(
            {entry.physical_page for entry in contents["contents"].entries},
            {1},
        )
        self.assertEqual(reader.call_count, 1)

    def test_disjoint_native_page_selection_fails_closed_before_pdf_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(
                frontmatter,
                "PdfReader",
                side_effect=AssertionError("disjoint selection must not read PDF"),
            ) as reader:
                result = extract_front_matter(
                    source,
                    physical_pages={5, 6, 10, 11},
                )

        self.assertEqual(result, {})
        reader.assert_not_called()

    def test_selected_native_cache_is_invalidated_when_source_changes(self) -> None:
        class FakePage:
            def __init__(self, text: str) -> None:
                self.text = text

            def extract_text(self, extraction_mode: str | None = None) -> str:
                return self.text

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.pdf"
            cache = root / "raw" / "cache" / "frontmatter-v8.json"
            source.write_bytes(b"pdf")
            with mock.patch.object(
                frontmatter,
                "PdfReader",
                return_value=mock.Mock(
                    pages=[FakePage("Contents\n1 Old 1\n2 End 2")]
                ),
            ):
                first = extract_front_matter(
                    source,
                    cache_path=cache,
                    physical_pages={1},
                )
            source.write_bytes(b"changed-pdf")
            with mock.patch.object(
                frontmatter,
                "PdfReader",
                return_value=mock.Mock(
                    pages=[FakePage("Contents\n1 New 1\n2 End 2")]
                ),
            ) as reader:
                refreshed = extract_front_matter(
                    source,
                    cache_path=cache,
                    physical_pages={1},
                )

        self.assertEqual(first["contents"].entries[0].title, "1 Old")
        self.assertEqual(refreshed["contents"].entries[0].title, "1 New")
        self.assertEqual(reader.call_count, 1)

    def test_cached_entry_outside_selected_physical_pages_is_rejected(self) -> None:
        class FakePage:
            def __init__(self, text: str) -> None:
                self.text = text

            def extract_text(self, extraction_mode: str | None = None) -> str:
                return self.text

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.pdf"
            cache = root / "raw" / "cache" / "frontmatter-v8.json"
            source.write_bytes(b"pdf")
            with mock.patch.object(
                frontmatter,
                "PdfReader",
                return_value=mock.Mock(
                    pages=[FakePage("Contents\n1 Old 1\n2 End 2")]
                ),
            ):
                extract_front_matter(
                    source,
                    cache_path=cache,
                    physical_pages={1},
                )
            payload = json.loads(cache.read_text(encoding="utf-8"))
            payload["sections"][0]["entries"][0]["physical_page"] = 2
            cache.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                frontmatter,
                "PdfReader",
                return_value=mock.Mock(
                    pages=[FakePage("Contents\n1 Fresh 1\n2 End 2")]
                ),
            ) as reader:
                refreshed = extract_front_matter(
                    source,
                    cache_path=cache,
                    physical_pages={1},
                )

        self.assertEqual(refreshed["contents"].entries[0].title, "1 Fresh")
        self.assertEqual(reader.call_count, 1)


class StructuredFrontRegionNavigationTests(unittest.TestCase):
    @staticmethod
    def _front_regions(
        navigation: dict[str, list[dict[str, object]]],
        *,
        confidence: float = 0.98,
    ) -> dict[str, object]:
        kind_by_navigation = {
            "contents": "contents",
            "list_of_figures": "list_of_figures",
            "list_of_tables": "list_of_tables",
        }
        pages = []
        for kind, sources in navigation.items():
            for source in sources:
                pages.append(
                    {
                        "page": source["page"],
                        "kind": kind_by_navigation[kind],
                        "confidence": confidence,
                        "evidence": source.get(
                            "evidence", ["explicit_title", "index_blocks"]
                        ),
                        "stats": {"index_items": 1},
                    }
                )
        return {
            "schema": "pdf2md.front-regions.v1",
            "total_pages": max((page["page"] for page in pages), default=0),
            "page_count": len(pages),
            "scanned_pages": len(pages),
            "truncated": False,
            "body_start_page": None,
            "pages": pages,
            "regions": [],
            "navigation": navigation,
            "warnings": [],
        }

    def test_structured_contents_repairs_and_completes_existing_list(self) -> None:
        source = """## Contents

1 Introduction 1
2 Totally Unrelated OCR Debris 8

2 Methods
This plain-text duplicate is not a Markdown heading.

## 1 Introduction

First body.

## 2 Methods

Second body.

## 3 Results

Third body.
"""
        front_regions = self._front_regions(
            {
                "contents": [
                    {
                        "page": 2,
                        "blocks": [
                            [
                                "1 Introduction 1",
                                "2 Methods 8",
                                "3 Results 20",
                            ]
                        ],
                    }
                ]
            }
        )

        result = enhance_document_navigation(
            source,
            front_regions=front_regions,
            selected_physical_pages={2},
        )
        repeated = enhance_document_navigation(
            result,
            front_regions=front_regions,
            selected_physical_pages={2},
        )

        contents = result.split("## Contents", 1)[1].split("\n2 Methods\n", 1)[0]
        self.assertEqual(repeated, result)
        self.assertNotIn("Totally Unrelated OCR Debris", contents)
        for title in ("1 Introduction", "2 Methods", "3 Results"):
            with self.subTest(title=title):
                self.assertRegex(contents, rf"\[{re.escape(title)}\]\(#\d+\)")
        self.assertEqual(result.count("[↑ Contents](#toc)"), 3)
        methods_link = re.search(r"\[2 Methods\]\(#(?P<id>\d+)\)", contents)
        self.assertIsNotNone(methods_link)
        methods_id = methods_link.group("id")
        self.assertIn(
            f'<a id="{methods_id}" data-pdf2md-nav="target"></a>\n## 2 Methods',
            result,
        )
        self.assertNotIn(
            f'<a id="{methods_id}" data-pdf2md-nav="target"></a>\n2 Methods',
            result,
        )

    def test_bilingual_contents_runs_are_partitioned_without_language_guessing(self) -> None:
        source_content = """## \u76ee\u5f55

\u7b2c\u4e00\u7ae0 \u4e2d\u6587\u6982\u8ff0 ........ 1
1.1 \u4e2d\u6587\u65b9\u6cd5 ........ 2
\u7b2c\u4e8c\u7ae0 \u4e2d\u6587\u7ed3\u679c ........ 3

## Contents

Chapter 1 English Overview ........ 1
1.1 English Method ........ 2
Chapter 2 English Results ........ 3

## \u7b2c\u4e00\u7ae0 \u4e2d\u6587\u6982\u8ff0

Body.

## 1.1 \u4e2d\u6587\u65b9\u6cd5

Body.

## \u7b2c\u4e8c\u7ae0 \u4e2d\u6587\u7ed3\u679c

Body.
"""
        front_regions = self._front_regions(
            {
                "contents": [
                    {
                        "page": 5,
                        "blocks": [[
                            "\u7b2c\u4e00\u7ae0 \u4e2d\u6587\u6982\u8ff0 ........ 1",
                            "1.1 \u4e2d\u6587\u65b9\u6cd5 ........ 2",
                        ]],
                    },
                    {
                        "page": 6,
                        "evidence": ["navigation_continuation", "index_blocks"],
                        "blocks": [["\u7b2c\u4e8c\u7ae0 \u4e2d\u6587\u7ed3\u679c ........ 3"]],
                    },
                    {
                        "page": 10,
                        "blocks": [[
                            "Chapter 1 English Overview ........ 1",
                            "1.1 English Method ........ 2",
                        ]],
                    },
                    {
                        "page": 11,
                        "evidence": ["navigation_continuation", "index_blocks"],
                        "blocks": [["Chapter 2 English Results ........ 3"]],
                    },
                ]
            }
        )
        aggregated_native = frontmatter.FrontMatterSection(
            kind="contents",
            title="Contents",
            entries=tuple(
                frontmatter.FrontMatterEntry("contents", title, str(page))
                for title, page in (
                    ("\u7b2c\u4e00\u7ae0 \u4e2d\u6587\u6982\u8ff0", 1),
                    ("1.1 \u4e2d\u6587\u65b9\u6cd5", 2),
                    ("\u7b2c\u4e8c\u7ae0 \u4e2d\u6587\u7ed3\u679c", 3),
                    ("Chapter 1 English Overview", 1),
                    ("1.1 English Method", 2),
                    ("Chapter 2 English Results", 3),
                )
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "fixture.pdf"
            source.write_bytes(b"fixture")
            with mock.patch.object(
                toc,
                "extract_front_matter",
                return_value={"contents": aggregated_native},
            ):
                result = enhance_document_navigation(
                    source_content,
                    source=source,
                    front_regions=front_regions,
                    selected_physical_pages={5, 6, 10, 11},
                )
                repeated = enhance_document_navigation(
                    result,
                    source=source,
                    front_regions=front_regions,
                    selected_physical_pages={5, 6, 10, 11},
                )

        chinese = result.split("## \u76ee\u5f55", 1)[1].split("## Contents", 1)[0]
        english = result.split("## Contents", 1)[1].split(
            "## \u7b2c\u4e00\u7ae0 \u4e2d\u6587\u6982\u8ff0", 1
        )[0]
        self.assertEqual(repeated, result)
        self.assertNotIn("English", chinese)
        self.assertNotIn("\u4e2d\u6587", english)
        for title in (
            "\u7b2c\u4e00\u7ae0 \u4e2d\u6587\u6982\u8ff0",
            "1.1 \u4e2d\u6587\u65b9\u6cd5",
            "\u7b2c\u4e8c\u7ae0 \u4e2d\u6587\u7ed3\u679c",
        ):
            self.assertEqual(chinese.count(title), 1)
        for title in (
            "Chapter 1 English Overview",
            "1.1 English Method",
            "Chapter 2 English Results",
        ):
            self.assertEqual(english.count(title), 1)
        self.assertNotRegex(chinese + english, r"(?:\.{3,}|-{5,})\s*\d+\s*$")

    def test_same_title_on_consecutive_structured_pages_is_one_section(self) -> None:
        source = """## TABLE OF CONTENTS

1 Alpha ........ 1
2 Beta ........ 2

## REVISION HISTORY

Change to Alpha ........ 1

## TABLE OF CONTENTS

3 Gamma ........ 3
4 Delta ........ 4

## 1 Alpha

Body.

## 2 Beta

Body.

## 3 Gamma

Body.

## 4 Delta

Body.
"""
        front_regions = self._front_regions(
            {
                "contents": [
                    {
                        "page": 2,
                        "blocks": [["1 Alpha ........ 1", "2 Beta ........ 2"]],
                    },
                    {
                        "page": 3,
                        "blocks": [["3 Gamma ........ 3", "4 Delta ........ 4"]],
                    },
                ]
            }
        )

        result = enhance_document_navigation(source, front_regions=front_regions)
        repeated = enhance_document_navigation(result, front_regions=front_regions)

        self.assertEqual(repeated, result)
        self.assertEqual(result.count("## TABLE OF CONTENTS"), 1)
        self.assertEqual(result.count('data-pdf2md-nav="section"'), 1)
        self.assertIn("## REVISION HISTORY", result)
        for title in ("1 Alpha", "2 Beta", "3 Gamma", "4 Delta"):
            self.assertEqual(result.count(title), 2)

    def test_same_title_structured_run_at_body_boundary_is_not_collapsed(self) -> None:
        source = """## Contents

1 Alpha ........ 1

## Contents

This ordinary body section is not a directory.

## 1 Alpha

Body.
"""
        front_regions = self._front_regions(
            {
                "contents": [
                    {"page": 2, "blocks": [["1 Alpha ........ 1"]]},
                    {"page": 3, "blocks": [["This ordinary body section"]]},
                ]
            }
        )
        front_regions["body_start_page"] = 3

        result = enhance_document_navigation(source, front_regions=front_regions)

        self.assertEqual(result.count("## Contents"), 2)
        self.assertIn("This ordinary body section is not a directory.", result)

    def test_unstructured_repeated_body_title_is_not_collapsed(self) -> None:
        source = """## Contents

1 Alpha ........ 1

## 1 Alpha

Body.

## Contents

This is an ordinary body section with a repeated title.
"""

        result = enhance_document_navigation(source)

        self.assertEqual(result.count("## Contents"), 2)
        self.assertIn(
            "This is an ordinary body section with a repeated title.", result
        )

    def test_single_contents_section_keeps_all_explicit_structured_runs(self) -> None:
        source = """## Contents

1 Alpha ........ 1
2 Beta ........ 2

## 1 Alpha

Body.

## 2 Beta

Body.
"""
        front_regions = self._front_regions(
            {
                "contents": [
                    {"page": 2, "blocks": [["1 Alpha ........ 1"]]},
                    {"page": 4, "blocks": [["2 Beta ........ 2"]]},
                ]
            }
        )

        result = enhance_document_navigation(source, front_regions=front_regions)

        contents = result.split("## Contents", 1)[1].split("## 1 Alpha", 1)[0]
        self.assertRegex(contents, r"\[1 Alpha\]\(#\d+\)")
        self.assertRegex(contents, r"\[2 Beta\]\(#\d+\)")
        self.assertEqual(enhance_document_navigation(result, front_regions=front_regions), result)

    def test_bad_or_low_confidence_front_regions_leave_unusable_list_unchanged(self) -> None:
        source = """## Contents

unusable OCR fragment

## 1 Alpha

Body.
"""
        low_confidence = self._front_regions(
            {
                "contents": [
                    {"page": 2, "blocks": [["1 Alpha 1"]]},
                ]
            },
            confidence=0.55,
        )
        malformed_reports: list[object] = [
            {"schema": "pdf2md.front-regions.v0", "navigation": {}},
            {
                "schema": "pdf2md.front-regions.v1",
                "pages": "not-a-list",
                "navigation": {"contents": "not-a-list"},
            },
            low_confidence,
        ]

        for front_regions in malformed_reports:
            with self.subTest(front_regions=front_regions):
                self.assertEqual(
                    enhance_document_navigation(source, front_regions=front_regions),
                    source,
                )

    def test_structured_navigation_never_creates_a_markdown_list_section(self) -> None:
        source = """# Document

## 1 Alpha

Body.
"""
        front_regions = self._front_regions(
            {
                "contents": [
                    {"page": 2, "blocks": [["1 Alpha 1"]]},
                ]
            }
        )

        self.assertEqual(
            enhance_document_navigation(source, front_regions=front_regions),
            source,
        )

    def test_structured_figure_and_table_lists_match_only_strict_captions(self) -> None:
        source = """## List of Figures

unusable figure-list fragment

## List of Tables

unusable table-list fragment

## 1.1 Shared result

This heading deliberately has the same identifier and title.

Figure 1.1: Shared result.

Table 1.1: Shared result.
"""
        front_regions = self._front_regions(
            {
                "list_of_figures": [
                    {"page": 4, "blocks": [["Figure 1.1 Shared result 12"]]},
                ],
                "list_of_tables": [
                    {"page": 5, "blocks": [["Table 1.1 Shared result 13"]]},
                ],
            }
        )

        result = enhance_document_navigation(source, front_regions=front_regions)
        repeated = enhance_document_navigation(result, front_regions=front_regions)

        figures = result.split("## List of Figures", 1)[1].split(
            "## List of Tables", 1
        )[0]
        tables = result.split("## List of Tables", 1)[1].split(
            "## 1.1 Shared result", 1
        )[0]
        figure_link = re.search(
            r"\[Figure 1\.1 Shared result\]\(#(?P<id>\d+)\)", figures
        )
        table_link = re.search(
            r"\[Table 1\.1 Shared result\]\(#(?P<id>\d+)\)", tables
        )
        self.assertEqual(repeated, result)
        self.assertIsNotNone(figure_link)
        self.assertIsNotNone(table_link)
        figure_id = figure_link.group("id")
        table_id = table_link.group("id")
        self.assertNotEqual(figure_id, table_id)
        self.assertIn(
            f'<a id="{figure_id}" data-pdf2md-nav="target" '
            'data-pdf2md-heading="generated"></a>\n'
            "###### Figure 1.1\n"
            "[↑ List of Figures](#list-of-figures)\n"
            "Figure 1.1: Shared result.",
            result,
        )
        self.assertIn(
            f'<a id="{table_id}" data-pdf2md-nav="target" '
            'data-pdf2md-heading="generated"></a>\n'
            "###### Table 1.1\n"
            "[↑ List of Tables](#list-of-tables)\n"
            "Table 1.1: Shared result.",
            result,
        )
        self.assertNotIn(
            f'<a id="{figure_id}" data-pdf2md-nav="target"></a>\n'
            "## 1.1 Shared result",
            result,
        )
        self.assertNotIn(
            f'<a id="{table_id}" data-pdf2md-nav="target"></a>\n'
            "## 1.1 Shared result",
            result,
        )

    def test_selected_physical_pages_filter_structured_navigation_sources(self) -> None:
        source = """## Contents

unusable OCR fragment

## 1 Alpha

Alpha body.

## 2 Beta

Beta body.
"""
        front_regions = self._front_regions(
            {
                "contents": [
                    {"page": 2, "blocks": [["1 Alpha 1"]]},
                    {"page": 3, "blocks": [["2 Beta 2"]]},
                ]
            }
        )

        result = enhance_document_navigation(
            source,
            front_regions=front_regions,
            selected_physical_pages={2},
        )
        contents = result.split("## Contents", 1)[1].split("## 1 Alpha", 1)[0]

        self.assertRegex(contents, r"\[1 Alpha\]\(#\d+\)")
        self.assertNotIn("2 Beta", contents)
        self.assertEqual(
            enhance_document_navigation(
                source,
                front_regions=front_regions,
                selected_physical_pages={99},
            ),
            source,
        )

    def test_contextual_chinese_contents_alias_with_leaders_is_idempotent(self) -> None:
        source = """# Device

## \u5185\u5bb9

1 \u7279\u6027 ........ 1
2 \u5e94\u7528 ........ 2
3 \u8bf4\u660e ........ 3

## 1 \u7279\u6027

Feature body.

## 2 \u5e94\u7528

Application body.

## 3 \u8bf4\u660e

Description body.
"""
        front_regions = self._front_regions(
            {
                "contents": [
                    {
                        "page": 6,
                        "blocks": [
                            [
                                "1 \u7279\u6027 ........ 1",
                                "2 \u5e94\u7528 ........ 2",
                                "3 \u8bf4\u660e ........ 3",
                            ]
                        ],
                    }
                ]
            },
            confidence=0.92,
        )

        result = enhance_document_navigation(
            source,
            front_regions=front_regions,
            selected_physical_pages={6},
        )
        repeated = enhance_document_navigation(
            result,
            front_regions=front_regions,
            selected_physical_pages={6},
        )

        self.assertEqual(repeated, result)
        self.assertIn(
            '<a id="toc" data-pdf2md-nav="section"></a>\n## \u5185\u5bb9',
            result,
        )
        for title in ("1 \u7279\u6027", "2 \u5e94\u7528", "3 \u8bf4\u660e"):
            with self.subTest(title=title):
                self.assertRegex(result, rf"\[{re.escape(title)}\]\(#\d+\)")

    def test_contextual_chinese_contents_alias_accepts_structured_rows(self) -> None:
        source = """## \u5185\u5bb9

1 Alpha 1
2 Beta 2
3 Gamma 3

## 1 Alpha

Alpha body.

## 2 Beta

Beta body.

## 3 Gamma

Gamma body.
"""
        front_regions = self._front_regions(
            {
                "contents": [
                    {
                        "page": 6,
                        "blocks": [["1 Alpha 1", "2 Beta 2", "3 Gamma 3"]],
                    }
                ]
            },
            confidence=0.92,
        )

        result = enhance_document_navigation(source, front_regions=front_regions)

        self.assertIn(
            '<a id="toc" data-pdf2md-nav="section"></a>\n## \u5185\u5bb9',
            result,
        )
        self.assertEqual(
            enhance_document_navigation(result, front_regions=front_regions),
            result,
        )

    def test_contextual_chinese_contents_alias_requires_report(self) -> None:
        source = """## \u5185\u5bb9

1 Alpha ........ 1
2 Beta ........ 2
3 Gamma ........ 3

## 1 Alpha

Body.
"""

        self.assertIsNone(frontmatter.navigation_kind("\u5185\u5bb9"))
        self.assertEqual(enhance_document_navigation(source), source)
        low_confidence = self._front_regions(
            {
                "contents": [
                    {
                        "page": 6,
                        "blocks": [["1 Alpha 1", "2 Beta 2", "3 Gamma 3"]],
                    }
                ]
            },
            confidence=0.89,
        )
        high_confidence = self._front_regions(
            {
                "contents": [
                    {
                        "page": 6,
                        "blocks": [["1 Alpha 1", "2 Beta 2", "3 Gamma 3"]],
                    }
                ]
            },
            confidence=0.98,
        )
        self.assertEqual(
            enhance_document_navigation(source, front_regions=low_confidence),
            source,
        )
        self.assertEqual(
            enhance_document_navigation(
                source,
                front_regions=high_confidence,
                selected_physical_pages={99},
            ),
            source,
        )

    def test_contextual_chinese_contents_alias_rejects_body_prose(self) -> None:
        source = """# Document

## 1 Overview

Introductory body.

## \u5185\u5bb9

This body section describes what the document contains.
It is ordinary prose, not a directory.

## 2 Details

More body.
"""
        front_regions = self._front_regions(
            {
                "contents": [
                    {
                        "page": 2,
                        "blocks": [["1 Alpha 1", "2 Beta 2", "3 Gamma 3"]],
                    }
                ]
            },
            confidence=0.98,
        )

        self.assertEqual(
            enhance_document_navigation(source, front_regions=front_regions),
            source,
        )

    def test_contextual_chinese_contents_alias_is_front_bounded(self) -> None:
        prefix = "\n".join("front filler" for _ in range(1000))
        source = (
            f"{prefix}\n## \u5185\u5bb9\n\n"
            "1 Alpha ........ 1\n"
            "2 Beta ........ 2\n"
            "3 Gamma ........ 3\n"
        )
        front_regions = self._front_regions(
            {
                "contents": [
                    {
                        "page": 2,
                        "blocks": [["1 Alpha 1", "2 Beta 2", "3 Gamma 3"]],
                    }
                ]
            },
            confidence=0.98,
        )

        self.assertEqual(
            enhance_document_navigation(source, front_regions=front_regions),
            source,
        )

    def test_contiguous_partial_selection_recovers_damaged_native_table_list(self) -> None:
        debris = "3.1 Broken table " + ". " * 300
        source_content = f"""## List of Tables

{debris}

## Acknowledgments

Front matter continues without table captions.
"""
        report = self._front_regions({})
        report["native_recovery_pages"] = [{
            "page": 13,
            "kind": "list_of_tables",
            "confidence": 0.62,
            "evidence": ["explicit_title", "unusable_navigation_debris"],
        }]
        native_titles = (
            "3.1 Scaling summary",
            "5.1 Multi-qubit measurement",
            "6.1 Rydberg states",
            "6.2 Molecular constants",
            "6.3 Possible hyperfine states",
            "6.4 Measurement encoding",
        )
        native = {
            "tables": frontmatter.FrontMatterSection(
                kind="tables",
                title="List of Tables",
                entries=tuple(
                    frontmatter.FrontMatterEntry(
                        "tables",
                        title,
                        str(page),
                        physical_page=13,
                    )
                    for title, page in zip(
                        native_titles,
                        (63, 146, 164, 167, 168, 173),
                    )
                ),
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "thesis.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(
                toc, "extract_front_matter", return_value=native
            ) as extract:
                result = enhance_document_navigation(
                    source_content,
                    source=source,
                    front_regions=report,
                    selected_physical_pages=range(1, 21),
                )
                repeated = enhance_document_navigation(
                    result,
                    source=source,
                    front_regions=report,
                    selected_physical_pages=range(1, 21),
                )

        self.assertEqual(repeated, result)
        self.assertEqual(extract.call_count, 2)
        self.assertNotIn(debris, result)
        table_list = result.split("## List of Tables", 1)[1].split(
            "## Acknowledgments", 1
        )[0]
        for title in native_titles:
            with self.subTest(title=title):
                self.assertIn(f"- {title}", table_list)
        self.assertNotIn("](", table_list)

    def test_body_only_selection_cannot_inject_unselected_native_navigation(self) -> None:
        source_content = """## List of Tables

This is ordinary body prose, not a table directory.
"""
        report = self._front_regions({})
        report["native_recovery_pages"] = [{
            "page": 13,
            "kind": "list_of_tables",
            "confidence": 0.62,
            "evidence": ["explicit_title", "unusable_navigation_debris"],
        }]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "thesis.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(
                toc,
                "extract_front_matter",
                side_effect=AssertionError("unselected page must not be read"),
            ) as extract:
                result = enhance_document_navigation(
                    source_content,
                    source=source,
                    front_regions=report,
                    selected_physical_pages={20},
                )

        self.assertEqual(result, source_content)
        extract.assert_not_called()

    def test_disjoint_selection_cannot_bridge_native_navigation_entries(self) -> None:
        source_content = """## List of Tables

unusable OCR fragment
"""
        report = self._front_regions({})
        report["native_recovery_pages"] = [{
            "page": 5,
            "kind": "list_of_tables",
            "confidence": 0.62,
            "evidence": ["explicit_title", "unusable_navigation_debris"],
        }]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "thesis.pdf"
            source.write_bytes(b"pdf")
            with mock.patch.object(
                toc,
                "extract_front_matter",
                side_effect=AssertionError("disjoint selection must fail closed"),
            ) as extract:
                result = enhance_document_navigation(
                    source_content,
                    source=source,
                    front_regions=report,
                    selected_physical_pages={5, 6, 10, 11},
                )

        self.assertNotIn("Injected table", result)
        extract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
