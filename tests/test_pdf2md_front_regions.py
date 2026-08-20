from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pdf2md_front_regions import classify_content_list_v2  # noqa: E402


def spans(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "content": text}]


def title(text: str) -> dict:
    return {"type": "title", "content": {"title_content": spans(text), "level": 1}}


def paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": {"paragraph_content": spans(text)}}


def page_header(text: str) -> dict:
    return {
        "type": "page_header",
        "content": {"page_header_content": spans(text)},
    }


def split_chapter_page(
    marker: str = "Chapter 1", *, marker_y: float = 0.218,
    include_title: bool = True,
) -> list[dict]:
    marker_bbox = [0.18, marker_y, 0.42, marker_y + 0.035]
    blocks = [{
        "type": "paragraph",
        "bbox": marker_bbox,
        "content": {"paragraph_content": spans(marker)},
        "_pdf2md": {"layout": {
            "label": "text", "score": 0.5144, "order": 0,
            "bbox": marker_bbox,
        }},
    }]
    if include_title:
        title_bbox = [0.15, marker_y + 0.07, 0.86, marker_y + 0.125]
        blocks.append({
            "type": "title",
            "bbox": title_bbox,
            "content": {
                "title_content": spans("Rydberg states and their applications"),
                "level": 1,
            },
            "_pdf2md": {"layout": {
                "label": "title", "score": 0.91, "order": 1,
                "bbox": title_bbox,
            }},
        })
    return blocks


def index(*items: str) -> dict:
    return {
        "type": "index",
        "content": {
            "list_type": "text_list",
            "list_items": [
                {"item_type": "text", "item_content": spans(item)} for item in items
            ],
        },
    }


def structured_list(*items: str) -> dict:
    return {
        "type": "list",
        "content": {
            "list_type": "text_list",
            "list_items": [
                {"item_type": "text", "item_content": spans(item)}
                for item in items
            ],
        },
    }


class FrontRegionTests(unittest.TestCase):
    def test_exact_navigation_page_header_requires_dense_terminal_page_column(self) -> None:
        english = tuple(f"Figure {number}. Apparatus {number + 40}" for number in range(1, 8))
        chinese = tuple(f"29.3-{number} 波形示例 {600 + number}" for number in range(1, 8))
        for heading, block in (
            ("List of Figures", index(*english)),
            ("Listing of Figures", structured_list(*english)),
            ("插图", index(*chinese)),
        ):
            with self.subTest(heading=heading):
                report = classify_content_list_v2(
                    [[block, page_header(heading)]],
                    start_page=8,
                    stop_at_body=False,
                )
                page = report["pages"][0]
                self.assertEqual(page["kind"], "list_of_figures")
                self.assertEqual(page["confidence"], 0.95)
                self.assertIn("page_header_navigation", page["evidence"])
                self.assertIn("dense_terminal_page_column", page["evidence"])
                self.assertEqual(
                    len(report["navigation"]["list_of_figures"][0]["blocks"][0]),
                    7,
                )

    def test_navigation_page_header_rule_rejects_weak_or_conflicting_pages(self) -> None:
        dense = tuple(f"Figure {number}. Apparatus {number + 40}" for number in range(1, 8))
        cases = {
            "header_only": [page_header("List of Figures")],
            "too_few": [page_header("List of Figures"), index(*dense[:5])],
            "no_page_tail": [
                page_header("List of Figures"),
                index(*(f"Figure {number}. Apparatus" for number in range(1, 8))),
            ],
            "inexact_header": [page_header("List of Figures — Draft"), index(*dense)],
            "prose_mention": [
                paragraph("See the List of Figures for supporting material."),
                index(*dense),
            ],
        }
        for name, blocks in cases.items():
            with self.subTest(name=name):
                page = classify_content_list_v2(
                    [blocks], start_page=8, stop_at_body=False
                )["pages"][0]
                self.assertNotEqual(page["kind"], "list_of_figures")
                self.assertNotIn("page_header_navigation", page["evidence"])

        body = classify_content_list_v2(
            [[title("Chapter 1 Introduction"), page_header("List of Figures"), index(*dense)]],
            start_page=8,
            stop_at_body=False,
        )["pages"][0]
        self.assertEqual(body["kind"], "body_start")
        self.assertNotIn("page_header_navigation", body["evidence"])

    def test_page_one_chinese_short_manual_body_uses_ordered_section_pairs(self) -> None:
        payload = [[
            title("CH32V307 评估板说明及应用参考"),
            paragraph("版本：V1.3"),
            title("一、概述"),
            paragraph("本评估板用于芯片开发，并提供完整的资源示例、下载接口与调试说明。"),
            title("二、评估板硬件"),
            paragraph("评估板原理图和硬件接口说明可参考配套文档，使用前请检查供电配置。"),
        ]]
        report = classify_content_list_v2(payload, start_page=1)

        page = report["pages"][0]
        self.assertEqual(page["kind"], "body_start")
        self.assertEqual(page["confidence"], 0.94)
        self.assertIn("short_chinese_manual_body", page["evidence"])
        self.assertEqual(report["body_start_page"], 1)

    def test_page_one_chinese_short_manual_guards_are_strict(self) -> None:
        prose_one = "这是第一节的完整正文说明，包含足够多的中文文本用于确认正文而不是菜单。"
        prose_two = "这是第二节的完整正文说明，继续介绍硬件资源、接口与开发配置。"
        good_blocks = [
            title("开发手册"), title("一、概述"), paragraph(prose_one),
            title("二、硬件"), paragraph(prose_two),
        ]
        cases = {
            "not_physical_page_one": (good_blocks, 2),
            "one_section": ([title("开发手册"), title("一、概述"), paragraph(prose_one)], 1),
            "skipped_number": (
                [title("开发手册"), title("一、概述"), paragraph(prose_one),
                 title("三、硬件"), paragraph(prose_two)],
                1,
            ),
            "no_section_prose": (
                [title("开发手册"), title("一、概述"), title("二、硬件")],
                1,
            ),
            "paragraph_reference": (
                [title("开发手册"), paragraph(f"参见一、概述和二、硬件。{prose_one}{prose_two}")],
                1,
            ),
            "navigation_present": (
                [*good_blocks, index("第一章 概述 .... 1", "第二章 硬件 .... 4")],
                1,
            ),
        }
        for name, (blocks, start_page) in cases.items():
            with self.subTest(name=name):
                page = classify_content_list_v2(
                    [blocks], start_page=start_page, stop_at_body=False
                )["pages"][0]
                self.assertNotIn("short_chinese_manual_body", page["evidence"])
                self.assertFalse(
                    page["kind"] == "body_start" and page["confidence"] >= 0.9
                )

    def test_chinese_and_english_navigation_headings(self) -> None:
        payload = [
            [title("\u76ee \u5f55"), index("\u7b2c\u4e00\u7ae0 \u7eea\u8bba \u2026\u2026 1", "\u7b2c\u4e8c\u7ae0 \u65b9\u6cd5 \u2026\u2026 9")],
            [title("List of Figures"), index("Figure 1. Apparatus .... 4")],
            [title("\u8868 \u76ee \u5f55"), index("\u8868 1 \u53c2\u6570 \u2026\u2026 5")],
        ]
        report = classify_content_list_v2(payload)

        self.assertEqual(
            [page["kind"] for page in report["pages"]],
            ["contents", "list_of_figures", "list_of_tables"],
        )
        self.assertEqual(
            report["navigation"]["contents"][0]["blocks"][0][0],
            "\u7b2c\u4e00\u7ae0 \u7eea\u8bba \u2026\u2026 1",
        )
        self.assertEqual(report["navigation"]["list_of_tables"][0]["page"], 3)

    def test_listing_of_figures_is_an_exact_title_only_alias(self) -> None:
        payload = [
            [title("Listing of Figures"), index("Figure 1 .... 2")],
            [index(*(f"Figure {number} .... {number + 2}" for number in range(2, 8)))],
        ]

        report = classify_content_list_v2(payload)

        self.assertEqual(
            [page["kind"] for page in report["pages"]],
            ["list_of_figures", "list_of_figures"],
        )
        self.assertIn("explicit_title", report["pages"][0]["evidence"])
        self.assertIn("navigation_continuation", report["pages"][1]["evidence"])

        paragraph_alias = classify_content_list_v2(
            [[paragraph("Listing of Figures")]], start_page=2
        )
        self.assertNotEqual(
            paragraph_alias["pages"][0]["kind"], "list_of_figures"
        )
        for generic in ("Listing", "Listing of Figure", "Figure Listing Notes"):
            with self.subTest(generic=generic):
                page = classify_content_list_v2(
                    [[title(generic), index("Figure 1 .... 2")]],
                    start_page=2,
                )["pages"][0]
                self.assertNotEqual(page["kind"], "list_of_figures")

    def test_revision_history_does_not_become_contents(self) -> None:
        payload = [
            [
                title("Revision History"),
                {"type": "table", "content": {"html": "<table><tr><td>Rev 1</td></tr></table>"}},
                paragraph("Version 1.2     2026-08-20"),
            ],
            [title("Contents"), index("1 Introduction .... 1", "2 Usage .... 7")],
        ]
        report = classify_content_list_v2(payload)

        self.assertEqual(report["pages"][0]["kind"], "revision_history")
        self.assertEqual(report["pages"][1]["kind"], "contents")
        self.assertNotIn("revision_history", report["navigation"])

    def test_paper_without_contents_finds_abstract_and_body_boundary(self) -> None:
        payload = [
            [title("Abstract"), paragraph("This paper presents a method.")],
            [title("1 Introduction"), paragraph("Prior work is reviewed here.")],
            [title("2 Methods"), paragraph("The experiment follows.")],
        ]
        report = classify_content_list_v2(payload, start_page=4)

        self.assertEqual(report["pages"][0]["kind"], "abstract")
        self.assertEqual(report["body_start_page"], 5)
        self.assertEqual(report["regions"][-1]["kind"], "body_start")
        self.assertEqual(report["regions"][-1]["start_page"], 5)
        self.assertEqual(report["regions"][-1]["end_page"], 5)
        self.assertEqual(report["scanned_pages"], 2)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["navigation"], {})

    def test_abstract_page_exports_independent_exact_contents_block(self) -> None:
        report = classify_content_list_v2([[
            title("Abstract"),
            paragraph("This work studies an open quantum system."),
            title("Contents"),
            index("1 Introduction .... 1", "2 Methods .... 7"),
        ]])

        self.assertEqual(report["pages"][0]["kind"], "abstract")
        self.assertEqual(report["pages"][0]["confidence"], 0.97)
        self.assertEqual(
            report["navigation"]["contents"][0],
            {
                "page": 1,
                "blocks": [["1 Introduction .... 1", "2 Methods .... 7"]],
            },
        )

    def test_abstract_page_exports_flattened_single_space_contents_paragraphs(self) -> None:
        report = classify_content_list_v2([[
            title("Abstract"),
            paragraph("A compact abstract that must remain the primary region."),
            title("Contents"),
            paragraph(
                "1 Introduction 2 "
                "2 Jordan-Wigner transformation 4 "
                "2.1 Fermionic formulation 9"
            ),
            paragraph(
                "3 Open boundary conditions 13 "
                "Appendix A Conventions 21 References 24"
            ),
        ]])

        self.assertEqual(report["pages"][0]["kind"], "abstract")
        self.assertEqual(
            report["navigation"]["contents"][0]["blocks"],
            [
                [
                    "1 Introduction 2",
                    "2 Jordan-Wigner transformation 4",
                    "2.1 Fermionic formulation 9",
                ],
                [
                    "3 Open boundary conditions 13",
                    "Appendix A Conventions 21",
                    "References 24",
                ],
            ],
        )

    def test_mixed_page_navigation_requires_exact_unambiguous_title_and_two_candidates(self) -> None:
        cases = {
            "heading_without_entries": [title("Abstract"), title("Contents")],
            "one_candidate": [
                title("Abstract"), title("Contents"), index("1 Introduction .... 1")
            ],
            "prose_mention": [
                title("Abstract"),
                paragraph("The Contents section summarizes this paper."),
                index("1 Introduction .... 1", "2 Methods .... 7"),
            ],
            "conflicting_navigation_titles": [
                title("Abstract"), title("Contents"), title("List of Figures"),
                index("1 Introduction .... 1", "Figure 1 .... 7"),
            ],
            "unusable_debris": [
                title("Abstract"), title("Contents"),
                index("1 Introduction " + "." * 1200),
            ],
            "received_years": [
                title("Abstract"), title("Contents"),
                paragraph(
                    "Received 22-09-2020 Accepted 21-03-2024 "
                    "Published 14-06-2024"
                ),
                paragraph("ISO 9001 quality management statement 2024"),
                paragraph(
                    "1 This ordinary prose sentence reports work from 2024"
                ),
                paragraph(
                    "2 Another ordinary sentence closes with the year 2025"
                ),
            ],
        }
        for name, blocks in cases.items():
            with self.subTest(name=name):
                report = classify_content_list_v2([blocks])
                self.assertEqual(report["pages"][0]["kind"], "abstract")
                self.assertEqual(report["navigation"], {})

    def test_chinese_abstract_title_is_exact_and_cjk_space_tolerant(self) -> None:
        for heading in ("\u4e2d\u6587\u6458\u8981", "\u4e2d \u6587 \u6458 \u8981"):
            with self.subTest(heading=heading):
                page = classify_content_list_v2(
                    [[title(heading)]], start_page=2
                )["pages"][0]
                self.assertEqual(page["kind"], "abstract")
                self.assertEqual(page["confidence"], 0.97)
                self.assertIn("explicit_title", page["evidence"])

        for false_heading in ("\u6458\u8981\u4fe1\u606f", "\u4e2d\u6587\u6458\u8981\u4fe1\u606f"):
            with self.subTest(false_heading=false_heading):
                page = classify_content_list_v2(
                    [[title(false_heading)]], start_page=2
                )["pages"][0]
                self.assertEqual(page["kind"], "other_front")

    def test_chinese_chapter_body_start_survives_cjk_spacing_normalization(self) -> None:
        for heading in ("\u7b2c\u4e00\u7ae0 \u7eea\u8bba", "\u7b2c \u4e00 \u7ae0 \u7eea \u8bba"):
            with self.subTest(heading=heading):
                report = classify_content_list_v2(
                    [[title(heading)], [title("Never reported")]], start_page=8
                )
                self.assertEqual(report["pages"][0]["kind"], "body_start")
                self.assertEqual(report["pages"][0]["confidence"], 0.9)
                self.assertEqual(report["body_start_page"], 8)

        self.assertEqual(
            classify_content_list_v2(
                [[title("\u76ee\u5f55\u7ed3\u6784")]], start_page=8
            )["pages"][0]["kind"],
            "other_front",
        )

    def test_split_paragraph_chapter_marker_is_strong_body_boundary(self) -> None:
        for marker in ("Chapter 1", "Part IV", "\u7b2c \u4e00 \u7ae0"):
            with self.subTest(marker=marker):
                report = classify_content_list_v2(
                    [split_chapter_page(marker), [title("Never reported")]],
                    start_page=12,
                )
                self.assertEqual(report["reported_page_count"], 1)
                self.assertEqual(report["body_start_page"], 12)
                self.assertEqual(report["pages"][0]["kind"], "body_start")
                self.assertEqual(report["pages"][0]["confidence"], 0.94)
                self.assertIn("split_body_heading", report["pages"][0]["evidence"])

    def test_split_chapter_rule_rejects_headers_references_and_unpaired_markers(self) -> None:
        cases = {
            "running_header": split_chapter_page(marker_y=0.04),
            "body_reference": split_chapter_page(marker_y=0.52),
            "not_exact": split_chapter_page("Chapter 1 is discussed", marker_y=0.218),
            "no_following_title": split_chapter_page(include_title=False),
        }
        for name, blocks in cases.items():
            with self.subTest(name=name):
                page = classify_content_list_v2(
                    [blocks], start_page=12, stop_at_body=False
                )["pages"][0]
                self.assertNotIn("split_body_heading", page["evidence"])
                self.assertLess(page["confidence"], 0.9)

    def test_navigation_continuation_is_inherited_and_regions_merge(self) -> None:
        payload = [
            [title("Table of Contents"), index("1 Start .... 1", "2 Setup .... 8")],
            [index("3 Results .... 20", "4 Conclusions .... 31")],
            [title("Chapter 1"), paragraph("Start")],
        ]
        report = classify_content_list_v2(payload)

        self.assertEqual([page["kind"] for page in report["pages"][:2]], ["contents", "contents"])
        self.assertIn("navigation_continuation", report["pages"][1]["evidence"])
        self.assertEqual(report["regions"][0]["start_page"], 1)
        self.assertEqual(report["regions"][0]["end_page"], 2)
        self.assertEqual(len(report["navigation"]["contents"]), 2)

    def test_dense_structured_figure_and_table_continuations_keep_their_kind(self) -> None:
        dense_figures = tuple(f"Figure {number} .... {number + 10}" for number in range(1, 7))
        dense_tables = tuple(f"Table {number} .... {number + 20}" for number in range(1, 7))
        payload = [
            [title("List of Figures"), index("Figure 0 .... 4")],
            [index(*dense_figures)],
            [index(*(f"Figure {number} .... {number + 30}" for number in range(7, 13)))],
            [paragraph("This page intentionally separates navigation sections.")],
            [title("List of Tables"), index("Table 0 .... 5")],
            [index(*dense_tables)],
        ]

        report = classify_content_list_v2(payload)

        self.assertEqual(
            [page["kind"] for page in report["pages"]],
            [
                "list_of_figures", "list_of_figures", "list_of_figures",
                "other_front", "list_of_tables", "list_of_tables",
            ],
        )
        for page in (report["pages"][1], report["pages"][2], report["pages"][5]):
            self.assertIn("navigation_continuation", page["evidence"])
            self.assertIn("structured_index_navigation", page["evidence"])
        self.assertEqual(
            [entry["page"] for entry in report["navigation"]["list_of_figures"]],
            [1, 2, 3],
        )
        self.assertEqual(
            [entry["page"] for entry in report["navigation"]["list_of_tables"]],
            [5, 6],
        )

    def test_body_title_wins_over_dense_structured_navigation_continuation(self) -> None:
        payload = [
            [title("List of Figures"), index("Figure 1 .... 2")],
            [
                title("Chapter 1 Introduction"),
                index(*(f"Item {number} .... {number}" for number in range(1, 7))),
            ],
        ]

        report = classify_content_list_v2(payload)

        self.assertEqual(report["pages"][1]["kind"], "body_start")
        self.assertIn("body_heading", report["pages"][1]["evidence"])
        self.assertEqual(report["body_start_page"], 2)
        self.assertEqual(
            [entry["page"] for entry in report["navigation"]["list_of_figures"]],
            [1],
        )

    def test_hybrid_paragraph_navigation_is_exported_and_continued(self) -> None:
        payload = [
            [
                title("Contents"),
                paragraph("1 Scope .... 1\n2 Pins .... 6"),
            ],
            [
                paragraph("3 Electrical characteristics .... 12"),
                paragraph("4 Package information .... 29"),
            ],
        ]

        report = classify_content_list_v2(payload)

        self.assertEqual([page["kind"] for page in report["pages"]], ["contents", "contents"])
        self.assertIn("paragraph_navigation_blocks", report["pages"][0]["evidence"])
        self.assertIn("navigation_continuation", report["pages"][1]["evidence"])
        self.assertEqual(
            report["navigation"]["contents"][0]["blocks"],
            [["1 Scope .... 1", "2 Pins .... 6"]],
        )
        self.assertEqual(
            report["navigation"]["contents"][1]["blocks"],
            [
                ["3 Electrical characteristics .... 12"],
                ["4 Package information .... 29"],
            ],
        )

    def test_navigation_inheritance_is_broken_by_an_unrelated_page(self) -> None:
        payload = [
            [title("List of Figures"), index("Figure 1 .... 2")],
            [paragraph("This intentionally blank page separates the sections.")],
            [index("1 Scope .... 1", "2 Pins .... 6")],
        ]

        report = classify_content_list_v2(payload)

        self.assertEqual(
            [page["kind"] for page in report["pages"]],
            ["list_of_figures", "other_front", "contents"],
        )
        self.assertEqual(report["navigation"]["contents"][0]["page"], 3)

    def test_body_headings_override_navigation_continuation(self) -> None:
        for heading in ("1 Scope", "1 \u8303\u56f4", "Chapter I", "Part I"):
            with self.subTest(heading=heading):
                payload = [
                    [title("Contents"), index("1 Scope .... 1", "2 Details .... 4")],
                    [title(heading), index("2 Details .... 4", "3 Notes .... 9")],
                    [title("Appendix")],
                ]

                report = classify_content_list_v2(payload)

                self.assertEqual(report["pages"][1]["kind"], "body_start")
                self.assertIn("body_heading", report["pages"][1]["evidence"])
                self.assertEqual(report["body_start_page"], 2)
                self.assertEqual(report["reported_page_count"], 2)

    def test_datasheet_page_one_features_can_look_ahead_to_contents(self) -> None:
        payload = [
            [title("1 Features"), paragraph("Low-power precision ADC")],
            [title("Contents"), index("1 Features .... 1", "6 Description .... 4")],
            [index("7 Applications .... 12", "8 Specifications .... 19")],
            [title("6 Detailed Description"), paragraph("The converter consists of...")],
            [title("7 Applications"), paragraph("Not reported")],
        ]

        report = classify_content_list_v2(payload)

        self.assertEqual(
            [page["kind"] for page in report["pages"]],
            ["body_start", "contents", "contents", "body_start"],
        )
        self.assertIn("front_navigation_lookahead", report["pages"][0]["evidence"])
        self.assertIn("post_navigation_body_heading", report["pages"][3]["evidence"])
        self.assertEqual(
            [page["page"] for page in report["navigation"]["contents"]],
            [2, 3],
        )
        self.assertEqual(report["body_start_page"], 1)
        self.assertEqual(report["input_page_count"], 5)
        self.assertEqual(report["examined_page_count"], 5)
        self.assertEqual(report["reported_page_count"], 4)
        self.assertEqual(report["stop_reason"], "body_boundary")
        self.assertTrue(report["stopped_at_body"])
        self.assertTrue(report["truncated"])

    def test_unnumbered_datasheet_stops_at_exact_post_navigation_heading(self) -> None:
        cases = (
            ("GENERAL DESCRIPTION", "FEATURES", "APPLICATIONS"),
            ("SPECIFICATIONS", "FEATURES", "FUNCTIONAL BLOCK DIAGRAM"),
            ("技术规格", "特性", "应用"),
            ("概述", "特性", "接口"),
        )
        for body_heading, primary, secondary in cases:
            with self.subTest(body_heading=body_heading):
                payload = [
                    [
                        title("Precision Converter"),
                        title(primary),
                        title(secondary),
                        # The same candidate before navigation is deliberately
                        # present and must not become a boundary.
                        title(body_heading),
                    ],
                    [
                        title("Contents"),
                        index("Specifications .... 3", "Pin Functions .... 8"),
                    ],
                    [title(body_heading), paragraph("Main datasheet material")],
                    [title("Never reported")],
                ]

                report = classify_content_list_v2(payload)

                self.assertEqual(report["reported_page_count"], 3)
                self.assertEqual(report["body_start_page"], 3)
                self.assertNotEqual(report["pages"][0]["kind"], "body_start")
                self.assertEqual(report["pages"][1]["kind"], "contents")
                self.assertEqual(report["pages"][2]["kind"], "body_start")
                self.assertEqual(report["pages"][2]["confidence"], 0.93)
                self.assertIn(
                    "datasheet_exact_heading", report["pages"][2]["evidence"]
                )
                self.assertEqual(report["stop_reason"], "body_boundary")

    def test_datasheet_post_navigation_heading_guards_are_strict(self) -> None:
        front = [title("FEATURES"), title("APPLICATIONS")]
        navigation = [
            title("Contents"),
            index("General Description .... 3", "Specifications .... 4"),
        ]
        cases = {
            "paragraph_mention": [
                paragraph("See GENERAL DESCRIPTION for operating details.")
            ],
            "third_title": [
                title("Device Name"), title("Data Sheet"), title("SPECIFICATIONS")
            ],
            "non_exact_title": [title("SPECIFICATIONS AND TEST CONDITIONS")],
            "still_navigation": [
                title("SPECIFICATIONS"), index("DC Performance .... 8")
            ],
        }
        for name, candidate in cases.items():
            with self.subTest(name=name):
                report = classify_content_list_v2(
                    [front, navigation, candidate], stop_at_body=False
                )
                self.assertIsNone(report["body_start_page"])
                self.assertNotIn(
                    "datasheet_exact_heading", report["pages"][2]["evidence"]
                )

        without_signature = classify_content_list_v2(
            [
                [title("Product Handbook")],
                navigation,
                [title("SPECIFICATIONS")],
            ],
            stop_at_body=False,
        )
        self.assertIsNone(without_signature["body_start_page"])

        excerpt = classify_content_list_v2(
            [navigation, [title("SPECIFICATIONS")]],
            start_page=20,
            stop_at_body=False,
        )
        self.assertIsNone(excerpt["body_start_page"])

    def test_datasheet_delayed_dense_index_defers_front_boundary(self) -> None:
        payload = [
            [title("1 Features"), paragraph("Low-power MCU")],
            [paragraph("Feature list continuation")],
            [title("2 Applications"), paragraph("Industrial control")],
            [title("3 Description"), paragraph("Device overview")],
            [title("3.1 Functional Block Diagram")],
            [
                title("Content"),
                index(
                    "1 Features .... 1", "2 Applications .... 3",
                    "3 Description .... 4", "4 Device Comparison .... 6",
                    "5 Terminal Configuration .... 10", "6 Specifications .... 76",
                ),
                title("4 Device Comparison"),
            ],
            [paragraph("Package comparison table")],
            [title("4.1 Device Identification")],
            [paragraph("Related products")],
            [title("5 Terminal Configuration and Functions")],
            [title("6 Specifications")],
        ]

        report = classify_content_list_v2(payload)

        self.assertEqual(report["reported_page_count"], 10)
        self.assertEqual(report["pages"][5]["kind"], "contents")
        self.assertEqual(report["pages"][5]["confidence"], 0.92)
        self.assertIn(
            "structured_index_navigation", report["pages"][5]["evidence"]
        )
        self.assertIn(
            "front_navigation_lookahead", report["pages"][0]["evidence"]
        )
        self.assertEqual(report["pages"][-1]["page"], 10)
        self.assertEqual(report["pages"][-1]["kind"], "body_start")
        self.assertIn(
            "post_navigation_body_heading", report["pages"][-1]["evidence"]
        )
        self.assertEqual(
            [entry["page"] for entry in report["navigation"]["contents"]], [6]
        )
        self.assertEqual(report["stop_reason"], "body_boundary")
        self.assertTrue(report["stopped_at_body"])

    def test_datasheet_leading_body_may_follow_a_cover(self) -> None:
        payload = [
            [title("Precision Converter Data Sheet")],
            [title("1 Features")],
            [paragraph("Feature continuation")],
            [
                index(
                    "1 Features .... 2", "2 Applications .... 5",
                    "3 Description .... 8", "4 Pins .... 11",
                    "5 Specifications .... 20", "6 Ordering .... 30",
                )
            ],
            [title("3 Detailed Description")],
        ]

        report = classify_content_list_v2(payload)

        self.assertEqual(
            [page["kind"] for page in report["pages"]],
            ["cover", "body_start", "other_front", "contents", "body_start"],
        )
        self.assertIn(
            "front_navigation_lookahead", report["pages"][1]["evidence"]
        )
        self.assertEqual(report["pages"][-1]["page"], 5)

    def test_datasheet_pages_one_and_two_keep_contents_without_second_body(self) -> None:
        payload = [
            [title("1 Features"), paragraph("Low-power precision ADC")],
            [title("Table of Contents"), index("1 Features .... 1", "4 Pins .... 3")],
        ]

        report = classify_content_list_v2(payload)

        self.assertEqual(
            [page["kind"] for page in report["pages"]],
            ["body_start", "contents"],
        )
        self.assertIn("front_navigation_lookahead", report["pages"][0]["evidence"])
        self.assertEqual(report["navigation"]["contents"][0]["page"], 2)
        self.assertEqual(report["body_start_page"], 1)
        self.assertEqual(report["reported_page_count"], 2)
        self.assertEqual(report["stop_reason"], "end")
        self.assertFalse(report["limited_by_max_pages"])
        self.assertFalse(report["stopped_at_body"])
        self.assertFalse(report["truncated"])

        limited = classify_content_list_v2(
            payload + [[title("4 Pin Configuration and Functions")]],
            max_pages=2,
        )
        self.assertEqual(
            [page["kind"] for page in limited["pages"]],
            ["body_start", "contents"],
        )
        self.assertEqual(limited["stop_reason"], "page_limit")
        self.assertTrue(limited["limited_by_max_pages"])
        self.assertFalse(limited["stopped_at_body"])
        self.assertTrue(limited["truncated"])

    def test_datasheet_lookahead_requires_navigation_and_stays_bounded(self) -> None:
        without_navigation = [
            [title("1 Features")],
            *[[paragraph(f"ordinary front page {number}")] for number in range(20)],
        ]
        no_navigation_report = classify_content_list_v2(without_navigation)
        self.assertEqual(len(no_navigation_report["pages"]), 1)
        self.assertEqual(no_navigation_report["stop_reason"], "body_boundary")
        self.assertNotIn(
            "front_navigation_lookahead",
            no_navigation_report["pages"][0]["evidence"],
        )

        beyond_boundary_window = [
            [title("1 Features")],
            [title("Contents"), index("1 Features .... 1", "4 Pins .... 3")],
            *[[paragraph(f"unclassified page {number}")] for number in range(13)],
        ]
        bounded_report = classify_content_list_v2(beyond_boundary_window)
        self.assertEqual(len(bounded_report["pages"]), 1)
        self.assertEqual(bounded_report["pages"][0]["kind"], "body_start")
        self.assertEqual(bounded_report["navigation"], {})
        self.assertEqual(bounded_report["stop_reason"], "body_boundary")
        self.assertTrue(bounded_report["stopped_at_body"])
        self.assertNotIn(
            "front_navigation_lookahead",
            bounded_report["pages"][0]["evidence"],
        )

        navigation_after_front_window = [
            [title("1 Features")],
            *[[paragraph(f"front page {number}")] for number in range(7)],
            [
                title("Contents"),
                index("1 Features .... 1", "2 Details .... 12"),
            ],
        ]
        late_report = classify_content_list_v2(navigation_after_front_window)
        self.assertEqual(late_report["reported_page_count"], 1)
        self.assertEqual(late_report["stop_reason"], "body_boundary")
        self.assertNotIn(
            "front_navigation_lookahead", late_report["pages"][0]["evidence"]
        )

    def test_paper_introduction_without_contents_still_stops_immediately(self) -> None:
        payload = [
            [title("1 Introduction"), paragraph("This paper presents...")],
            [title("2 Methods"), paragraph("The method is...")],
            [title("3 Results"), paragraph("Never reported")],
        ]

        report = classify_content_list_v2(payload)

        self.assertEqual(len(report["pages"]), 1)
        self.assertEqual(report["pages"][0]["kind"], "body_start")
        self.assertNotIn("front_navigation_lookahead", report["pages"][0]["evidence"])
        self.assertEqual(report["body_start_page"], 1)
        self.assertEqual(report["input_page_count"], 3)
        self.assertEqual(report["examined_page_count"], 3)
        self.assertEqual(report["reported_page_count"], 1)
        self.assertEqual(report["stop_reason"], "body_boundary")
        self.assertFalse(report["limited_by_max_pages"])
        self.assertTrue(report["stopped_at_body"])
        self.assertEqual(report["total_pages"], 3)
        self.assertEqual(report["page_count"], 1)
        self.assertEqual(report["scanned_pages"], 1)
        self.assertTrue(report["truncated"])

    def test_cover_heuristic_is_restricted_to_physical_page_one(self) -> None:
        payload = [[title("A Product Handbook")]]

        cover = classify_content_list_v2(payload, start_page=1)
        excerpt = classify_content_list_v2(payload, start_page=7)

        self.assertEqual(cover["pages"][0]["kind"], "cover")
        self.assertEqual(excerpt["pages"][0]["kind"], "other_front")
        self.assertEqual(excerpt["pages"][0]["page"], 7)

    def test_keywords_require_complete_headings(self) -> None:
        report = classify_content_list_v2(
            [[title("\u76ee\u5f55\u7ed3\u6784")], [title("Abstract Algebra")]], start_page=2
        )

        self.assertEqual(
            [page["kind"] for page in report["pages"]],
            ["other_front", "other_front"],
        )

    def test_unicode_escapes_are_real_runtime_chinese(self) -> None:
        simplified = "\u76ee\u5f55"
        traditional = "\u76ee\u9304"
        scope = "1 \u8303\u56f4"

        self.assertEqual(simplified, chr(0x76EE) + chr(0x5F55))
        self.assertEqual(traditional, chr(0x76EE) + chr(0x9304))
        self.assertEqual(scope, "1 " + chr(0x8303) + chr(0x56F4))
        self.assertEqual(
            classify_content_list_v2([[title(simplified)]], start_page=2)["pages"][0]["kind"],
            "contents",
        )
        self.assertEqual(
            classify_content_list_v2([[title(traditional)]], start_page=2)["pages"][0]["kind"],
            "contents",
        )
        self.assertEqual(
            classify_content_list_v2([[title(scope)]], start_page=2)["pages"][0]["kind"],
            "body_start",
        )
        for false_heading in ("\u76ee\u5f55\u7ed3\u6784", "Abstract Algebra"):
            with self.subTest(false_heading=false_heading):
                self.assertEqual(
                    classify_content_list_v2(
                        [[title(false_heading)]], start_page=2
                    )["pages"][0]["kind"],
                    "other_front",
                )

    def test_footnotes_and_asides_cannot_drive_classification(self) -> None:
        payload = [[
            {
                "type": "page_footnote",
                "content": {"page_footnote_content": spans("Contents")},
            },
            {
                "type": "page_aside_text",
                "content": {"page_aside_text_content": spans("1 Scope")},
            },
        ]]

        report = classify_content_list_v2(payload, start_page=4)
        page = report["pages"][0]

        self.assertEqual(page["kind"], "other_front")
        self.assertEqual(page["stats"]["paragraphs"], 0)
        self.assertEqual(page["stats"]["navigation_blocks"], 0)
        self.assertEqual(page["stats"]["footnotes"], 1)
        self.assertEqual(page["stats"]["asides"], 1)

    def test_traditional_and_combined_navigation_headings(self) -> None:
        examples = {
            "\u76ee\u9304": "contents",
            "\u5716\u76ee\u9304": "list_of_figures",
            "\u8868\u76ee\u9304": "list_of_tables",
            "\u5716\u8868\u76ee\u9304": "list_of_figures",
            "List of Figures and Tables": "list_of_figures",
            "List of Tables and Figures": "list_of_figures",
            "Table of Figures": "list_of_figures",
            "Table of Tables": "list_of_tables",
            "\u76ee\u9304 / Contents": "contents",
        }
        for heading, expected in examples.items():
            with self.subTest(heading=heading):
                report = classify_content_list_v2(
                    [[title(heading), index("Example .... 1", "More .... 2")]],
                    start_page=2,
                )
                self.assertEqual(report["pages"][0]["kind"], expected)

    def test_structured_index_without_heading_is_conservative_contents(self) -> None:
        payload = [[
            {"type": "page_header", "content": {"page_header_content": spans("Manual")}},
            index("Overview ........ 1", "Installation ........ 4", "Reference ........ 9"),
            {"type": "page_number", "content": {"page_number_content": spans("iii")}},
        ]]
        report = classify_content_list_v2(payload)

        page = report["pages"][0]
        self.assertEqual(page["kind"], "contents")
        self.assertEqual(page["confidence"], 0.68)
        self.assertIn("structured_index_without_heading", page["evidence"])
        self.assertEqual(page["stats"]["headers"], 1)
        self.assertEqual(page["stats"]["page_numbers"], 1)

    def test_explicit_contents_with_only_long_leader_debris_is_not_exported(self) -> None:
        report = classify_content_list_v2(
            [[title("\u76ee\u5f55"), index("1 \u7eea\u8bba " + "." * 1200)]],
            start_page=2,
        )

        page = report["pages"][0]
        self.assertEqual(page["kind"], "contents")
        self.assertEqual(page["confidence"], 0.62)
        self.assertIn("explicit_title", page["evidence"])
        self.assertIn("unusable_navigation_debris", page["evidence"])
        self.assertNotIn("index_blocks", page["evidence"])
        self.assertEqual(report["navigation"], {})

        valid = classify_content_list_v2(
            [[title("\u76ee\u5f55"), index("\u7b2c\u4e00\u7ae0 \u7eea\u8bba .... 1")]],
            start_page=2,
        )
        self.assertEqual(valid["pages"][0]["confidence"], 0.98)
        self.assertIn("contents", valid["navigation"])

    def test_scan_style_index_text_fallback_is_accepted(self) -> None:
        payload = [[
            {"type": "index", "text": "Chapter 1 .... 1"},
            {"type": "index", "text": "Chapter 2 .... 8"},
        ]]
        report = classify_content_list_v2(payload)

        self.assertEqual(report["pages"][0]["kind"], "contents")
        self.assertEqual(
            report["navigation"]["contents"][0]["blocks"],
            [["Chapter 1 .... 1"], ["Chapter 2 .... 8"]],
        )

    def test_list_blocks_are_navigation_candidates_only_under_explicit_heading(self) -> None:
        list_block = {
            "type": "list",
            "content": {
                "list_type": "text_list",
                "list_items": [
                    {"item_content": spans("1 Scope .... 1")},
                    {"item_content": spans("2 Pins .... 6")},
                ],
            },
        }
        report = classify_content_list_v2([[title("Contents"), list_block]])

        self.assertEqual(
            report["navigation"]["contents"][0]["blocks"],
            [["1 Scope .... 1", "2 Pins .... 6"]],
        )

    def test_front_heading_dictionary(self) -> None:
        examples = {
            "Copyright": "legal",
            "\u524d\u8a00": "preface",
            "\u81f4 \u8c22": "acknowledgements",
            "\u7f29\u7565\u8bed": "abbreviations",
            "List of Symbols": "nomenclature",
        }
        for heading, expected in examples.items():
            with self.subTest(heading=heading):
                report = classify_content_list_v2([[title(heading)]])
                self.assertEqual(report["pages"][0]["kind"], expected)

    def test_path_input_limit_and_compact_metadata(self) -> None:
        payload = [[title(f"Appendix page {number}")] for number in range(70)]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "content_list_v2.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = classify_content_list_v2(path)

        self.assertEqual(report["total_pages"], 70)
        self.assertEqual(report["scanned_pages"], 64)
        self.assertTrue(report["truncated"])
        self.assertNotIn("Appendix", json.dumps(report))

    def test_stop_metadata_distinguishes_limit_body_and_end(self) -> None:
        limited = classify_content_list_v2(
            [[paragraph(f"front {number}")] for number in range(5)],
            start_page=10,
            max_pages=2,
        )
        self.assertEqual(limited["start_page"], 10)
        self.assertEqual(limited["input_page_count"], 5)
        self.assertEqual(limited["examined_page_count"], 2)
        self.assertEqual(limited["reported_page_count"], 2)
        self.assertEqual(limited["stop_reason"], "page_limit")
        self.assertTrue(limited["limited_by_max_pages"])
        self.assertFalse(limited["stopped_at_body"])
        self.assertEqual(limited["total_pages"], 5)
        self.assertEqual(limited["page_count"], 2)
        self.assertEqual(limited["scanned_pages"], 2)
        self.assertTrue(limited["truncated"])

        body = classify_content_list_v2([
            [title("Abstract")],
            [title("1 Scope")],
            [title("Never reported")],
        ], start_page=4)
        self.assertEqual(body["input_page_count"], 3)
        self.assertEqual(body["examined_page_count"], 3)
        self.assertEqual(body["reported_page_count"], 2)
        self.assertEqual(body["stop_reason"], "body_boundary")
        self.assertFalse(body["limited_by_max_pages"])
        self.assertTrue(body["stopped_at_body"])
        self.assertEqual(body["scanned_pages"], 2)
        self.assertTrue(body["truncated"])

        ended = classify_content_list_v2(
            [[paragraph("front")], [paragraph("more front")]], start_page=2
        )
        self.assertEqual(ended["examined_page_count"], 2)
        self.assertEqual(ended["reported_page_count"], 2)
        self.assertEqual(ended["stop_reason"], "end")
        self.assertFalse(ended["limited_by_max_pages"])
        self.assertFalse(ended["stopped_at_body"])
        self.assertFalse(ended["truncated"])

    def test_bad_json_and_malformed_pages_are_safe(self) -> None:
        bad = classify_content_list_v2("{broken")
        self.assertEqual(bad["scanned_pages"], 0)
        self.assertIn("invalid_json", bad["warnings"])

        partial = classify_content_list_v2([None, "scan", {"blocks": "bad"}])
        self.assertEqual(partial["scanned_pages"], 3)
        self.assertTrue(all(page["kind"] == "other_front" for page in partial["pages"]))


if __name__ == "__main__":
    unittest.main()
