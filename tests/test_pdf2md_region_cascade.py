from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pdf2md_region_cascade as cascade  # noqa: E402
from pdf2md_region_cascade import (  # noqa: E402
    classifier_fingerprint, classify_front_regions_v2, project_front_regions_v1,
)
from pdf2md_region_evidence import extract_region_evidence  # noqa: E402
from pdf2md_region_models import (  # noqa: E402
    ARTIFACT_SCHEMA, Prediction, load_model_artifact, save_json_artifact,
)


def spans(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "content": text}]


def detector(label: str = "text", *, order: int = 1) -> dict:
    return {"label": label, "score": 0.95, "order": order}


def title(text: str, *, layout: dict | None = None) -> dict:
    block = {
        "type": "title",
        "bbox": [10, 20, 300, 60],
        "content": {"title_content": spans(text), "level": 1},
    }
    if layout is not None:
        block["_pdf2md"] = {"layout": layout}
    return block


def paragraph(text: str, *, layout: dict | None = None) -> dict:
    block = {
        "type": "paragraph", "bbox": [10, 70, 500, 700],
        "content": {"paragraph_content": spans(text)},
    }
    if layout is not None:
        block["_pdf2md"] = {"layout": layout}
    return block


def page_header(text: str) -> dict:
    return {
        "type": "page_header",
        "bbox": [10, 5, 300, 18],
        "content": {"page_header_content": spans(text)},
    }


def index(*items: str, layout: dict | None = None) -> dict:
    block = {
        "type": "index", "bbox": [20, 100, 500, 700],
        "content": {"list_items": [
            {"item_content": spans(item)} for item in items
        ]},
    }
    if layout is not None:
        block["_pdf2md"] = {"layout": layout}
    return block


def split_chapter_page(marker_y: float = 0.218) -> list[dict]:
    marker_bbox = [0.18, marker_y, 0.42, marker_y + 0.035]
    title_bbox = [0.15, marker_y + 0.07, 0.86, marker_y + 0.125]
    return [
        {
            "type": "paragraph", "bbox": marker_bbox,
            "content": {"paragraph_content": spans("Chapter 1")},
            "_pdf2md": {"layout": {
                "label": "text", "score": 0.5144, "order": 0,
                "bbox": marker_bbox,
            }},
        },
        {
            "type": "title", "bbox": title_bbox,
            "content": {
                "title_content": spans("Rydberg states and their applications"),
                "level": 1,
            },
            "_pdf2md": {"layout": {
                "label": "title", "score": 0.91, "order": 1,
                "bbox": title_bbox,
            }},
        },
    ]


class SpyModel:
    def __init__(self, prediction: Prediction, fingerprint: str) -> None:
        self.prediction = prediction
        self.fingerprint = fingerprint
        self.calls = 0

    def predict(self, features: dict[str, float]) -> Prediction:
        self.calls += 1
        self.last_features = features
        return self.prediction


class CascadeTests(unittest.TestCase):
    def test_rules_only_locks_dense_page_header_navigation_and_short_manual_body(self) -> None:
        figures = [[
            page_header("List of Figures"),
            index(*(f"Figure {number}. Apparatus {number + 40}" for number in range(1, 8))),
        ]]
        figure_report = classify_front_regions_v2(figures, start_page=8)
        figure_page = figure_report["pages"][0]
        self.assertTrue(figure_page["accepted"])
        self.assertEqual(figure_page["kind"], "list_of_figures")
        self.assertEqual(figure_page["decision_source"], "rule")
        self.assertIn("page_header_navigation", figure_page["evidence"]["rule"])
        projected = project_front_regions_v1(figure_report)
        self.assertEqual(
            projected["navigation"]["list_of_figures"][0]["page"], 8
        )

        manual = [[
            title("CH32V307 评估板说明及应用参考"),
            title("一、概述"),
            paragraph("本评估板用于芯片开发，并提供完整的资源示例、下载接口与调试说明。"),
            title("二、评估板硬件"),
            paragraph("评估板原理图和硬件接口说明可参考配套文档，使用前请检查供电配置。"),
        ]]
        manual_report = classify_front_regions_v2(manual, start_page=1)
        manual_page = manual_report["pages"][0]
        self.assertTrue(manual_page["accepted"])
        self.assertEqual(manual_page["kind"], "body_start")
        self.assertEqual(manual_page["decision_source"], "rule")
        self.assertIn(
            "short_chinese_manual_body", manual_page["evidence"]["rule"]
        )

    def test_layout_metadata_is_strictly_validated(self) -> None:
        payload = [[
            title("ordinary", layout={
                "label": "title", "score": 0.91, "order": 2,
                "bbox": [10, 20, 300, 60],
            }),
            title("bad", layout={
                "label": "title", "score": float("nan"), "order": -1,
                "bbox": [30, 20, 10, 60],
            }),
        ]]
        evidence = extract_region_evidence(payload)

        self.assertEqual(len(evidence.pages[0].valid_layout_blocks), 1)
        self.assertEqual(evidence.pages[0].valid_layout_blocks[0].order, 2)
        self.assertIn("invalid_layout_metadata", evidence.pages[0].blocks[1].warnings)

    def test_high_confidence_rule_has_no_fake_calibrated_probability(self) -> None:
        report = classify_front_regions_v2([[title("Contents"), index("1 Start .... 1")]])
        page = report["pages"][0]

        self.assertTrue(page["accepted"])
        self.assertEqual(page["decision_source"], "rule")
        self.assertIsNone(page["calibrated_probability"])
        self.assertGreater(page["rule_strength"], 0.9)

    def test_long_leader_debris_does_not_rule_lock_or_project_navigation(self) -> None:
        report = classify_front_regions_v2(
            [[title("\u76ee\u5f55"), index("1 \u7eea\u8bba " + "." * 1200)]],
            start_page=2,
        )
        page = report["pages"][0]

        self.assertFalse(page["accepted"])
        self.assertEqual(page["decision_source"], "abstain")
        self.assertEqual(page["rule_strength"], 0.62)
        self.assertIn(
            "unusable_navigation_debris", page["evidence"]["rule"]
        )
        self.assertEqual(report["processing"]["stage_counts"]["rule_locked"], 0)
        self.assertEqual(project_front_regions_v1(report)["navigation"], {})

    def test_layout_acceptance_never_calls_text_model(self) -> None:
        layout = SpyModel(Prediction((("contents", 0.96), ("abstract", 0.04))), "layout-a")
        text = SpyModel(Prediction((("abstract", 0.99), ("contents", 0.01))), "text-a")
        report = classify_front_regions_v2(
            [[paragraph("ordinary page", layout=detector())]],
            layout_model=layout,
            text_model=text,
        )

        self.assertEqual(layout.calls, 1)
        self.assertEqual(text.calls, 0)
        self.assertEqual(report["pages"][0]["decision_source"], "layout")
        self.assertEqual(report["processing"]["stage_counts"]["text_called"], 0)

    def test_uncertain_layout_calls_text_and_agreement_is_accepted(self) -> None:
        layout = SpyModel(Prediction((("contents", 0.60), ("abstract", 0.40))), "layout-b")
        text = SpyModel(Prediction((("contents", 0.96), ("abstract", 0.04))), "text-b")
        report = classify_front_regions_v2(
            [[paragraph(
                "chapter list ........................ 2",
                layout=detector(),
            )]],
            layout_model=layout, text_model=text,
        )

        self.assertEqual(text.calls, 1)
        self.assertTrue(report["pages"][0]["accepted"])
        self.assertEqual(report["pages"][0]["decision_source"], "text")

    def test_missing_layout_evidence_skips_layout_and_calls_text(self) -> None:
        layout = SpyModel(
            Prediction((("contents", 0.99), ("abstract", 0.01))),
            "layout-missing-evidence",
        )
        text = SpyModel(
            Prediction((("abstract", 0.99), ("contents", 0.01))),
            "text-fallback",
        )
        page = classify_front_regions_v2(
            [[paragraph("ordinary page")]],
            layout_model=layout,
            text_model=text,
        )["pages"][0]

        self.assertEqual(layout.calls, 0)
        self.assertEqual(text.calls, 1)
        self.assertEqual(page["decision_source"], "text")
        self.assertEqual(
            page["evidence"]["layout"]["reason"],
            "layout_evidence_unavailable",
        )

    def test_text_fallback_maps_navigation_for_legacy_scoreless_cache(self) -> None:
        layout = SpyModel(
            Prediction((("other_front", 0.99), ("contents", 0.01))),
            "layout-must-not-run",
        )
        text = SpyModel(
            Prediction((("contents", 0.99), ("other_front", 0.01))),
            "text-legacy-navigation",
        )
        report = classify_front_regions_v2(
            [[index("1 Introduction .... 1\n2 Methods .... 9")]],
            layout_model=layout,
            text_model=text,
        )
        projected = project_front_regions_v1(report)

        self.assertEqual(layout.calls, 0)
        self.assertEqual(text.calls, 1)
        self.assertEqual(report["pages"][0]["decision_source"], "text")
        self.assertEqual(
            projected["navigation"]["contents"][0]["blocks"],
            [["1 Introduction .... 1", "2 Methods .... 9"]],
        )

    def test_model_recognized_navigation_uses_page_candidates(self) -> None:
        layout = SpyModel(
            Prediction((("contents", 0.99), ("other_front", 0.01))),
            "layout-navigation",
        )
        report = classify_front_regions_v2(
            [[index(
                "1 Introduction .... 1\n2 Methods .... 9",
                layout=detector("content"),
            )]],
            layout_model=layout,
        )
        projected = project_front_regions_v1(report)

        self.assertEqual(report["pages"][0]["decision_source"], "layout")
        self.assertEqual(
            projected["navigation"]["contents"][0]["blocks"],
            [["1 Introduction .... 1", "2 Methods .... 9"]],
        )

    def test_model_retypes_rule_navigation_without_losing_candidates(self) -> None:
        layout = SpyModel(
            Prediction((("list_of_figures", 0.99), ("contents", 0.01))),
            "layout-figures",
        )
        report = classify_front_regions_v2(
            [[index(
                "Figure 1 Overview .... 2",
                "Figure 2 Detail .... 7",
                layout=detector("content"),
            )]],
            layout_model=layout,
        )
        projected = project_front_regions_v1(report)

        self.assertEqual(report["pages"][0]["kind"], "list_of_figures")
        self.assertNotIn("contents", projected["navigation"])
        self.assertEqual(
            projected["navigation"]["list_of_figures"][0]["blocks"],
            [["Figure 1 Overview .... 2", "Figure 2 Detail .... 7"]],
        )

    def test_low_confidence_body_does_not_hide_later_navigation(self) -> None:
        payload = [
            [paragraph("1 Introduction")],
            [title("Contents"), index("1 Introduction .... 1", "2 Methods .... 9")],
            [title("Chapter 1 Introduction")],
            [title("Contents"), index("outside selected pages .... 20")],
        ]
        report = classify_front_regions_v2(
            payload,
            start_page=10,
            max_pages=3,
        )
        projected = project_front_regions_v1(report)

        self.assertEqual(
            [(page["page"], page["kind"], page["accepted"]) for page in report["pages"]],
            [(10, None, False), (11, "contents", True), (12, "body_start", True)],
        )
        self.assertTrue(report["stopped_at_body"])
        self.assertEqual(report["stop_reason"], "body_boundary")
        self.assertEqual(
            [entry["page"] for entry in projected["navigation"]["contents"]],
            [11],
        )

    def test_split_paragraph_chapter_marker_locks_and_projects_body_stop(self) -> None:
        payload = [
            [title("Acknowledgements")],
            split_chapter_page(),
            [title("Never examined")],
        ]
        report = classify_front_regions_v2(payload, start_page=11)
        projected = project_front_regions_v1(report)

        self.assertEqual(
            [(page["page"], page["kind"], page["accepted"]) for page in report["pages"]],
            [(11, "acknowledgements", True), (12, "body_start", True)],
        )
        self.assertTrue(report["stopped_at_body"])
        self.assertEqual(report["stop_reason"], "body_boundary")
        self.assertEqual(projected["body_start_page"], 12)
        self.assertTrue(projected["stopped_at_body"])
        self.assertIn("split_body_heading", projected["pages"][-1]["evidence"])

    def test_split_chapter_geometry_guards_do_not_create_cascade_boundary(self) -> None:
        for marker_y in (0.04, 0.52):
            with self.subTest(marker_y=marker_y):
                report = classify_front_regions_v2(
                    [split_chapter_page(marker_y)], start_page=12
                )
                self.assertFalse(report["stopped_at_body"])
                self.assertFalse(report["pages"][0]["accepted"])
                self.assertNotIn(
                    "split_body_heading", report["pages"][0]["evidence"]["rule"]
                )

    def test_datasheet_front_navigation_lookahead_still_defers_body_stop(self) -> None:
        payload = [
            [
                title("1 Features", layout=detector("paragraph_title")),
                paragraph("Low-power precision ADC"),
            ],
            [title("Contents"), index("1 Features .... 1", "6 Description .... 4")],
            [index("7 Applications .... 12", "8 Specifications .... 19")],
            [title("6 Detailed Description"), paragraph("Converter details")],
            [title("7 Applications"), paragraph("Must not be examined")],
        ]
        report = classify_front_regions_v2(payload)
        projected = project_front_regions_v1(report)

        self.assertEqual(
            [(page["page"], page["kind"]) for page in report["pages"]],
            [
                (1, "body_start"),
                (2, "contents"),
                (3, "contents"),
                (4, "body_start"),
            ],
        )
        self.assertTrue(report["stopped_at_body"])
        self.assertEqual(
            [entry["page"] for entry in projected["navigation"]["contents"]],
            [2, 3],
        )

    def test_delayed_dense_index_is_retained_before_cascade_body_stop(self) -> None:
        payload = [
            [title("1 Features", layout=detector("paragraph_title"))],
            [paragraph("Feature continuation")],
            [title("2 Applications")],
            [title("3 Description")],
            [title("3.1 Functional Block Diagram")],
            [index(
                "1 Features .... 1", "2 Applications .... 3",
                "3 Description .... 4", "4 Comparison .... 6",
                "5 Pins .... 10", "6 Specifications .... 76",
            )],
            [paragraph("Comparison table")],
            [title("4.1 Device Identification")],
            [paragraph("Related products")],
            [title("5 Terminal Configuration")],
            [title("6 Specifications")],
        ]

        report = classify_front_regions_v2(payload)
        projected = project_front_regions_v1(report)

        self.assertEqual(report["pages"][-1]["page"], 10)
        self.assertEqual(report["pages"][-1]["kind"], "body_start")
        self.assertTrue(report["stopped_at_body"])
        self.assertEqual(
            [(page["page"], page["kind"]) for page in report["pages"] if page["accepted"]],
            [(1, "body_start"), (6, "contents"), (10, "body_start")],
        )
        self.assertEqual(
            [entry["page"] for entry in projected["navigation"]["contents"]], [6]
        )

    def test_model_accepted_datasheet_lead_body_also_defers_stop(self) -> None:
        payload = [
            [
                title("1 Features", layout=detector("paragraph_title")),
                paragraph("Low-power precision ADC"),
            ],
            [title("Contents"), index("1 Features .... 1", "6 Description .... 4")],
            [index("7 Applications .... 12", "8 Specifications .... 19")],
            [title("6 Detailed Description"), paragraph("Converter details")],
        ]
        layout = SpyModel(
            Prediction((("body_start", 0.99), ("cover", 0.01))),
            "layout-body",
        )
        original_rule_locked = cascade._rule_locked

        def route_leading_body_to_model(
            kind: str | None,
            strength: float | None,
            evidence: list[object],
            navigation_blocks: dict[str, object],
        ) -> bool:
            if kind == "body_start" and "front_navigation_lookahead" in evidence:
                return False
            return original_rule_locked(
                kind, strength, evidence, navigation_blocks
            )

        with mock.patch.object(
            cascade,
            "_rule_locked",
            side_effect=route_leading_body_to_model,
        ):
            report = classify_front_regions_v2(payload, layout_model=layout)

        self.assertEqual(report["pages"][0]["decision_source"], "layout")
        self.assertEqual(
            [(page["page"], page["kind"]) for page in report["pages"]],
            [
                (1, "body_start"),
                (2, "contents"),
                (3, "contents"),
                (4, "body_start"),
            ],
        )
        self.assertTrue(report["stopped_at_body"])

    def test_strong_body_boundary_still_stops_before_later_pages(self) -> None:
        report = classify_front_regions_v2([
            [title("1 Introduction"), paragraph("Body")],
            [title("Contents"), index("Must not be used .... 2")],
        ])

        self.assertEqual(len(report["pages"]), 1)
        self.assertEqual(report["pages"][0]["kind"], "body_start")
        self.assertTrue(report["stopped_at_body"])

    def test_conflict_nan_ood_and_bad_artifact_abstain_safely(self) -> None:
        conflict_layout = SpyModel(Prediction((("abstract", 0.60), ("contents", 0.40))), "l")
        conflict_text = SpyModel(Prediction((("contents", 0.97), ("abstract", 0.03))), "t")
        conflict = classify_front_regions_v2(
            [[paragraph("ordinary", layout=detector())]],
            layout_model=conflict_layout,
            text_model=conflict_text,
        )["pages"][0]
        self.assertFalse(conflict["accepted"])
        self.assertEqual(conflict["evidence"]["abstain_reason"], "layout_text_conflict")

        nan_model = SpyModel(Prediction((("contents", math.nan),)), "nan")
        nan_page = classify_front_regions_v2(
            [[paragraph("ordinary", layout=detector())]], layout_model=nan_model
        )["pages"][0]
        self.assertFalse(nan_page["accepted"])
        self.assertEqual(nan_page["evidence"]["layout"]["reason"], "invalid_model_result")

        ood_model = SpyModel(Prediction((), True, "out_of_distribution"), "ood")
        ood_page = classify_front_regions_v2(
            [[paragraph("ordinary", layout=detector())]], layout_model=ood_model
        )["pages"][0]
        self.assertFalse(ood_page["accepted"])

        with tempfile.TemporaryDirectory() as temporary:
            broken = Path(temporary) / "layout.json"
            broken.write_text('{"schema":"wrong"}', encoding="utf-8")
            self.assertIsNone(load_model_artifact(broken, expected_kind="layout"))

    def test_json_artifact_round_trip_and_fingerprint(self) -> None:
        artifact = {
            "schema": ARTIFACT_SCHEMA,
            "kind": "layout",
            "classes": ["contents", "abstract"],
            "feature_names": ["layout.mean_score"],
            "weights": [[4.0], [-4.0]],
            "bias": [0.0, 0.0],
            "temperature": 1.0,
            "ood": {"min_known_fraction": 0.0, "min_feature_l1": 0.0, "max_feature_l1": 10.0},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_json_artifact(root / "layout.json", artifact)
            model = load_model_artifact(root / "layout.json", expected_kind="layout")
            self.assertIsNotNone(model)
            report = classify_front_regions_v2(
                [[paragraph('ordinary')]], model_dir=root
            )
            self.assertEqual(
                report['classifier']['fingerprint'], classifier_fingerprint(root)
            )
            self.assertEqual(model.kind, "layout")
            self.assertEqual(
                classifier_fingerprint(root), classifier_fingerprint(root)
            )

    def test_v1_projection_keeps_only_accepted_navigation_blocks(self) -> None:
        payload = [
            [title("Contents"), index("1 Start .... 1", "2 End .... 4")],
            [paragraph("unresolved ordinary page")],
        ]
        v2 = classify_front_regions_v2(payload)
        v1 = project_front_regions_v1(v2)

        self.assertEqual(v1["schema"], "pdf2md.front-regions.v1")
        self.assertEqual([page["kind"] for page in v1["pages"]], ["contents"])
        self.assertEqual(
            v1["navigation"]["contents"][0]["blocks"],
            [["1 Start .... 1", "2 End .... 4"]],
        )
        self.assertEqual(v1["pages"][0]["page"], 1)

    def test_abstract_page_keeps_mixed_navigation_through_v2_projection(self) -> None:
        payload = [[
            title("Abstract"),
            paragraph("This work studies an open quantum system."),
            title("Contents"),
            paragraph(
                "1 Introduction 1 1.1 Scope and structure 3 "
                "2 Experimental platforms 7"
            ),
            paragraph("Appendix A Conventions 21 References 24"),
        ]]

        v2 = classify_front_regions_v2(payload)
        page = v2["pages"][0]
        self.assertTrue(page["accepted"])
        self.assertEqual(page["kind"], "abstract")
        self.assertEqual(page["decision_source"], "rule")
        self.assertEqual(
            page["evidence"]["navigation_blocks"]["contents"],
            [
                [
                    "1 Introduction 1",
                    "1.1 Scope and structure 3",
                    "2 Experimental platforms 7",
                ],
                ["Appendix A Conventions 21", "References 24"],
            ],
        )

        v1 = project_front_regions_v1(v2)
        self.assertEqual(v1["pages"][0]["kind"], "abstract")
        self.assertEqual(
            v1["navigation"]["contents"][0],
            {
                "page": 1,
                "blocks": [
                    [
                        "1 Introduction 1",
                        "1.1 Scope and structure 3",
                        "2 Experimental platforms 7",
                    ],
                    ["Appendix A Conventions 21", "References 24"],
                ],
            },
        )

    def test_projection_keeps_distinct_navigation_kinds_on_the_same_page(self) -> None:
        v2 = classify_front_regions_v2([[
            title("Contents"),
            index("1 Introduction .... 1", "2 Methods .... 7"),
        ]])
        page = v2["pages"][0]
        self.assertTrue(page["accepted"])
        self.assertEqual(page["kind"], "contents")
        page["evidence"]["navigation_blocks"] = {
            "contents": [["1 Introduction .... 1", "2 Methods .... 7"]],
            "list_of_figures": [["Figure 1 .... 3", "Figure 2 .... 9"]],
        }

        v1 = project_front_regions_v1(v2)

        self.assertEqual(
            v1["navigation"]["contents"],
            [{"page": 1, "blocks": [["1 Introduction .... 1", "2 Methods .... 7"]]}],
        )
        self.assertEqual(
            v1["navigation"]["list_of_figures"],
            [{"page": 1, "blocks": [["Figure 1 .... 3", "Figure 2 .... 9"]]}],
        )

    def test_mixed_navigation_seeds_adjacent_index_continuation_and_body_stop(self) -> None:
        payload = [
            [
                title("Abstract"),
                paragraph("This work studies an open quantum system."),
                title("Contents"),
                paragraph("1 Introduction 1 2 Experimental platforms 7"),
            ],
            [index("2.1 Trapped ions .... 9", "2.2 Ultracold atoms .... 13")],
            [title("Chapter 1 Introduction")],
        ]

        v2 = classify_front_regions_v2(payload)
        self.assertEqual(
            [
                (page["page"], page["kind"], page["accepted"])
                for page in v2["pages"]
            ],
            [(1, "abstract", True), (2, "contents", True), (3, "body_start", True)],
        )
        self.assertIn(
            "navigation_continuation", v2["pages"][1]["evidence"]["rule"]
        )
        self.assertEqual(v2["pages"][1]["decision_source"], "rule")
        self.assertTrue(v2["stopped_at_body"])

        v1 = project_front_regions_v1(v2)
        self.assertEqual(
            [entry["page"] for entry in v1["navigation"]["contents"]],
            [1, 2],
        )
        self.assertEqual(v1["body_start_page"], 3)
        self.assertTrue(v1["stopped_at_body"])

    def test_ordinary_page_clears_mixed_navigation_continuation_context(self) -> None:
        payload = [
            [
                title("Abstract"), title("Contents"),
                paragraph("1 Introduction 1 2 Methods 7"),
            ],
            [paragraph("An ordinary intervening page.")],
            [index("3 Results .... 12", "4 Conclusions .... 19")],
        ]

        v2 = classify_front_regions_v2(payload)
        self.assertFalse(v2["pages"][2]["accepted"])
        self.assertNotIn(
            "navigation_continuation", v2["pages"][2]["evidence"]["rule"]
        )
        self.assertEqual(
            [entry["page"] for entry in project_front_regions_v1(v2)["navigation"]["contents"]],
            [1],
        )

    def test_projection_rejects_unknown_or_malformed_mixed_navigation_blocks(self) -> None:
        v2 = classify_front_regions_v2([[
            title("Abstract"), title("Contents"),
            index("1 Introduction .... 1", "2 Methods .... 7"),
        ]])
        mapping = v2["pages"][0]["evidence"]["navigation_blocks"]
        mapping["unknown"] = [["must not project"]]
        mapping["contents"] = [["1 Introduction .... 1"], [""]]

        self.assertEqual(project_front_regions_v1(v2)["navigation"], {})

    def test_rules_only_keeps_navigation_continuation_in_v1_projection(self) -> None:
        payload = [
            [title('Contents'), index('1 Start .... 1', '2 Setup .... 8')],
            [index('3 Results .... 20', '4 End .... 31')],
            [title('Chapter 1')],
        ]
        v2 = classify_front_regions_v2(payload)
        v1 = project_front_regions_v1(v2)

        self.assertEqual(
            [page['kind'] for page in v1['pages']],
            ['contents', 'contents', 'body_start'],
        )
        self.assertEqual(
            [entry['page'] for entry in v1['navigation']['contents']], [1, 2]
        )

    def test_rules_only_keeps_dense_figure_continuation_kind_in_v1_projection(self) -> None:
        payload = [
            [title('List of Figures'), index('Figure 1 .... 2')],
            [index(*(f'Figure {number} .... {number + 2}' for number in range(2, 8)))],
            [title('Chapter 1 Introduction')],
        ]

        v2 = classify_front_regions_v2(payload)
        v1 = project_front_regions_v1(v2)

        self.assertEqual(
            [page['kind'] for page in v1['pages']],
            ['list_of_figures', 'list_of_figures', 'body_start'],
        )
        self.assertEqual(
            [entry['page'] for entry in v1['navigation']['list_of_figures']],
            [1, 2],
        )
        self.assertNotIn('contents', v1['navigation'])

    def test_listing_of_figures_projects_as_figure_navigation(self) -> None:
        payload = [
            [title('Listing of Figures'), index('Figure 1 .... 2')],
            [index(*(f'Figure {number} .... {number + 2}' for number in range(2, 8)))],
            [title('Chapter 1 Introduction')],
        ]

        v2 = classify_front_regions_v2(payload)
        v1 = project_front_regions_v1(v2)

        self.assertEqual(
            [page['kind'] for page in v1['pages']],
            ['list_of_figures', 'list_of_figures', 'body_start'],
        )
        self.assertEqual(
            [entry['page'] for entry in v1['navigation']['list_of_figures']],
            [1, 2],
        )
        self.assertNotIn('contents', v1['navigation'])

    def test_report_contains_required_fingerprints_and_compact_evidence(self) -> None:
        report = classify_front_regions_v2([[paragraph("front")]])
        self.assertEqual(report["schema"], "pdf2md.front-regions.v2")
        self.assertEqual(len(report["classifier"]["fingerprint"]), 64)
        self.assertEqual(len(report["inputs"]["content_list_sha256"]), 64)
        self.assertIn("stage_counts", report["processing"])
        self.assertIn("timing_ms", report["processing"])
        self.assertIn("blocks", report["pages"][0])
        self.assertEqual(
            report["classifier"]["rules_version"], cascade.RULES_VERSION
        )
        self.assertEqual(
            report["classifier"]["features_version"], cascade.FEATURES_VERSION
        )

    def test_rule_and_feature_versions_change_classifier_fingerprint(self) -> None:
        baseline = classifier_fingerprint()
        with mock.patch.object(cascade, "RULES_VERSION", "rules-next"):
            rules_changed = classifier_fingerprint()
        with mock.patch.object(cascade, "FEATURES_VERSION", "features-next"):
            features_changed = classifier_fingerprint()

        self.assertNotEqual(rules_changed, baseline)
        self.assertNotEqual(features_changed, baseline)


if __name__ == "__main__":
    unittest.main()
