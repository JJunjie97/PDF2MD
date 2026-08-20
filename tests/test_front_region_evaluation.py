from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evaluate_front_regions", ROOT / "scripts" / "evaluate-front-regions.py",
)
assert SPEC and SPEC.loader
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def spans(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "content": text}]


def title(text: str) -> dict:
    return {
        "type": "title",
        "bbox": [10, 20, 300, 60],
        "content": {"title_content": spans(text), "level": 1},
    }


def paragraph(text: str) -> dict:
    return {
        "type": "paragraph",
        "bbox": [10, 70, 500, 700],
        "content": {"paragraph_content": spans(text)},
    }


def index(*items: str) -> dict:
    return {
        "type": "index",
        "bbox": [20, 100, 500, 700],
        "content": {"list_items": [
            {"item_content": spans(item)} for item in items
        ]},
    }


class FrontRegionEvaluationTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        pdf = root / "sample.pdf"
        pdf.write_bytes(b"%PDF-fake-evaluation-fixture")
        source_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        output = pdf.with_suffix(".pdf2md")
        content_path = output / "raw" / "cache" / "content-lists" / "one.json"
        content_path.parent.mkdir(parents=True)
        pages = [
            [title("Contents"), index("1 Introduction .... 3")],
            [title("Abstract"), paragraph("SECRET ABSTRACT TEXT")],
            [title("Chapter 1"), paragraph("SECRET BODY TEXT")],
            [paragraph("SECRET UNRESOLVED FRONT TEXT")],
        ]
        content_path.write_text(json.dumps(pages), encoding="utf-8")
        manifest = {
            "schema_version": 2,
            "core": "PDF2MD",
            "core_version": "test-core",
            "cache_version": "test-cache",
            "source": {"sha256": source_sha, "name": pdf.name},
            "selections": [{
                "pages": "1-4",
                "content_list_v2": "raw/cache/content-lists/one.json",
                "profile": "balanced",
                "method": "hybrid",
                "requested_method": "auto",
                "language": "en",
            }],
        }
        (output / "raw" / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8",
        )
        (output / "raw" / "inspect.json").write_text(json.dumps({
            "source": {"sha256": source_sha},
            "page_count": len(pages),
        }), encoding="utf-8")
        corpus = root / "corpus.json"
        corpus.write_text(json.dumps({
            "front_region_schema": "pdf2md.front-regions.v1",
            "documents": [{
                "id": "sample",
                "local_path": pdf.name,
                "expected_sha256": source_sha,
                "training_eligible": False,
                "redistributable": False,
            }],
        }), encoding="utf-8")
        labels = (
            (1, "contents"),
            # Deliberately wrong gold kind to exercise accepted-error metrics.
            (2, "legal"),
            (3, "body_start"),
            (4, "other_front"),
        )
        annotations = root / "annotations.jsonl"
        annotations.write_text("".join(
            json.dumps({
                "schema": "pdf2md.front-page-label.v1",
                "document_id": "sample",
                "source_sha256": source_sha,
                "page": page,
                "kind": kind,
                "status": "verified",
                "reviewer": "human-test",
            }) + "\n"
            for page, kind in labels
        ), encoding="utf-8")
        return corpus, annotations, pdf, root / "missing-models"

    def write_labels(
        self, annotations: Path, pdf: Path, labels: tuple[tuple[int, str], ...],
    ) -> None:
        source_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        annotations.write_text("".join(
            json.dumps({
                "schema": "pdf2md.front-page-label.v1",
                "document_id": "sample",
                "source_sha256": source_sha,
                "page": page,
                "kind": kind,
                "status": "verified",
                "reviewer": "human-test",
            }) + "\n"
            for page, kind in labels
        ), encoding="utf-8")

    def write_navigation_labels(
        self,
        path: Path,
        pdf: Path,
        labels: tuple[tuple[int, str, str], ...],
    ) -> None:
        source_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        path.write_text("".join(
            json.dumps({
                "schema": "pdf2md.front-navigation-label.v1",
                "document_id": "sample",
                "source_sha256": source_sha,
                "page": page,
                "kind": kind,
                "presence": presence,
                "status": "verified",
                "reviewer": "human-test",
            }) + "\n"
            for page, kind, presence in labels
        ), encoding="utf-8")

    def test_rules_only_metrics_abstention_and_no_text_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus, annotations, pdf, model_dir = self.fixture(Path(temporary))
            before = pdf.read_bytes()
            report = evaluation.evaluate(
                corpus, annotations, model_dir=model_dir,
            )

            self.assertEqual(report["schema"], evaluation.SCHEMA)
            self.assertEqual(report["classifier"]["mode"], "rules-only")
            self.assertFalse(report["classifier"]["models_approved"])
            self.assertEqual(report["summary"]["total_samples"], 4)
            self.assertEqual(report["summary"]["accepted"], 3)
            self.assertEqual(report["summary"]["abstained"], 1)
            self.assertEqual(report["summary"]["correct"], 2)
            self.assertEqual(report["summary"]["coverage"], 0.75)
            self.assertEqual(report["summary"]["accepted_accuracy"], 0.666667)
            self.assertEqual(report["summary"]["overall_accuracy"], 0.5)
            self.assertEqual(report["confusion"]["legal"], {"abstract": 1})
            self.assertEqual(report["confusion"]["other_front"], {"__abstain__": 1})
            self.assertEqual(report["per_class"]["other_front"]["recall"], 0.0)
            self.assertEqual(report["body_start"]["exact_accuracy"], 1.0)
            self.assertEqual(report["body_start"]["mae"], 0.0)
            self.assertEqual(pdf.read_bytes(), before)
            self.assertEqual(
                report["navigation_presence"]["status"], "not_evaluated",
            )
            self.assertEqual(
                report["navigation_presence"]["summary"]["total_samples"], 0,
            )
            self.assertEqual(
                report["navigation_presence"]["annotation_summary"][
                    "verified_labels"
                ],
                0,
            )
            self.assertFalse(
                report["navigation_presence"]["summary"][
                    "release_gate_eligible"
                ],
            )

            context = report["production_context"]
            self.assertEqual(context["comparability"]["verified_samples"], 4)
            self.assertEqual(context["comparability"]["scored_samples"], 4)
            self.assertEqual(context["comparability"]["coverage"], 1.0)
            self.assertEqual(context["summary"], report["summary"])
            self.assertEqual(context["confusion"], report["confusion"])
            self.assertEqual(context["documents"]["sample"]["status"], "scored")
            self.assertEqual(
                context["documents"]["sample"]["selection"]["pages"], "1-4",
            )
            self.assertEqual(
                context["documents"]["sample"]["samples"][-1]["abstain_reason"],
                "not_examined_after_body_boundary",
            )
            self.assertEqual(
                context["policy"]["unlabelled_pages"],
                "classified-in-memory-not-scored-or-serialized",
            )
            content_path = (
                pdf.with_suffix(".pdf2md")
                / "raw" / "cache" / "content-lists" / "one.json"
            )
            self.assertEqual(
                context["documents"]["sample"]["selection"]["content_list_sha256"],
                hashlib.sha256(content_path.read_bytes()).hexdigest(),
            )
            self.assertTrue(all(
                len(item["page_content_sha256"]) == 64
                for item in context["documents"]["sample"]["samples"]
            ))

            serialized = evaluation._serialize(report)
            self.assertNotIn("SECRET", serialized)
            self.assertNotIn('"blocks"', serialized)
            self.assertNotIn('"text_preview"', serialized)
            self.assertNotIn('"navigation_candidates"', serialized)
            self.assertEqual(
                report,
                evaluation.evaluate(corpus, annotations, model_dir=model_dir),
            )

    def test_embedded_navigation_is_scored_independently_of_primary_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, annotations, pdf, model_dir = self.fixture(root)
            content_path = (
                pdf.with_suffix(".pdf2md")
                / "raw" / "cache" / "content-lists" / "one.json"
            )
            pages = json.loads(content_path.read_text(encoding="utf-8"))
            pages[1] = [
                title("Abstract"),
                paragraph("SECRET MIXED ABSTRACT TEXT"),
                title("Contents"),
                index("1 Introduction .... 3", "2 Methods .... 9"),
            ]
            content_path.write_text(json.dumps(pages), encoding="utf-8")
            self.write_labels(annotations, pdf, (
                (1, "contents"),
                (2, "abstract"),
                (3, "body_start"),
                (4, "other_front"),
            ))
            baseline = evaluation.evaluate(
                corpus, annotations, model_dir=model_dir,
            )
            navigation = root / "explicit-navigation.jsonl"
            self.write_navigation_labels(navigation, pdf, (
                (2, "contents", "present"),
                (2, "list_of_figures", "absent"),
                (2, "list_of_tables", "absent"),
            ))
            report = evaluation.evaluate(
                corpus,
                annotations,
                navigation_annotations_path=navigation,
                model_dir=model_dir,
            )

            self.assertEqual(report["summary"], baseline["summary"])
            self.assertEqual(report["confusion"], baseline["confusion"])
            self.assertEqual(
                report["production_context"]["summary"],
                baseline["production_context"]["summary"],
            )
            primary_page = next(
                item for item in report["documents"]["sample"]["samples"]
                if item["page"] == 2
            )
            self.assertEqual(primary_page["gold"], "abstract")
            self.assertEqual(primary_page["predicted"], "abstract")

            navigation_report = report["navigation_presence"]
            self.assertEqual(navigation_report["status"], "evaluated")
            self.assertEqual(
                navigation_report["annotation_summary"],
                {
                    "verified_labels": 3,
                    "verified_pages": 1,
                    "verified_documents": 1,
                    "present": 1,
                    "absent": 2,
                    "fully_annotated_pages": 1,
                },
            )
            self.assertEqual(navigation_report["summary"]["true_positive"], 1)
            self.assertEqual(navigation_report["summary"]["true_negative"], 2)
            self.assertEqual(navigation_report["summary"]["overall_accuracy"], 1.0)
            self.assertIsNone(
                navigation_report["summary"]["balanced_accuracy"],
            )
            self.assertFalse(
                navigation_report["summary"]["release_gate_eligible"],
            )
            self.assertEqual(
                navigation_report["gate_requirements"],
                {
                    "minimum_present_per_kind": 20,
                    "minimum_absent_per_kind": 20,
                    "minimum_present_documents_per_kind": 5,
                    "minimum_absent_documents_per_kind": 5,
                },
            )
            self.assertEqual(
                navigation_report["per_kind"]["contents"]["support_absent"], 0,
            )
            self.assertEqual(
                navigation_report["per_kind"]["list_of_figures"][
                    "support_present"
                ],
                0,
            )
            self.assertEqual(
                navigation_report["exact_set"]["exact_accuracy"], 1.0,
            )
            sample = next(
                item
                for item in navigation_report["documents"]["sample"]["samples"]
                if item["kind"] == "contents"
            )
            self.assertEqual(sample["predicted"], "present")
            context = report["production_context"]["navigation_presence"]
            self.assertEqual(context["comparability"]["scored_samples"], 3)
            self.assertEqual(context["summary"]["true_positive"], 1)
            self.assertEqual(
                context["documents"]["sample"]["selection"]["pages"], "1-4",
            )
            serialized = evaluation._serialize(report)
            self.assertNotIn("SECRET", serialized)
            self.assertNotIn('"blocks"', serialized)
            self.assertNotIn('"navigation_candidates"', serialized)

    def test_unlabelled_navigation_is_unknown_and_primary_rejection_abstains(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, annotations, pdf, model_dir = self.fixture(root)
            navigation = root / "explicit-navigation.jsonl"
            self.write_navigation_labels(
                navigation, pdf, ((4, "contents", "absent"),),
            )
            report = evaluation.evaluate(
                corpus,
                annotations,
                navigation_annotations_path=navigation,
                model_dir=model_dir,
            )
            navigation_report = report["navigation_presence"]
            self.assertEqual(
                navigation_report["annotation_summary"]["verified_labels"], 1,
            )
            self.assertEqual(
                navigation_report["per_kind"]["contents"]["total_samples"], 1,
            )
            self.assertEqual(
                navigation_report["per_kind"]["list_of_figures"][
                    "total_samples"
                ],
                0,
            )
            self.assertEqual(navigation_report["summary"]["abstained"], 1)
            self.assertEqual(navigation_report["summary"]["negative_abstain"], 1)
            self.assertEqual(navigation_report["summary"]["correct"], 0)
            self.assertEqual(navigation_report["summary"]["coverage"], 0.0)
            self.assertEqual(navigation_report["summary"]["overall_accuracy"], 0.0)
            sample = navigation_report["documents"]["sample"]["samples"][0]
            self.assertEqual(sample["predicted"], evaluation.ABSTAIN)
            self.assertEqual(sample["abstain_reason"], "model_unavailable")

            context = report["production_context"]["navigation_presence"]
            self.assertEqual(context["summary"]["abstained"], 1)
            context_sample = context["documents"]["sample"]["samples"][0]
            self.assertEqual(
                context_sample["abstain_reason"],
                "not_examined_after_body_boundary",
            )

    def test_navigation_confusion_counts_explicit_presence_and_abstention(self) -> None:
        samples = [
            {
                "document_id": "sample", "page": 1, "kind": "contents",
                "gold": "present", "predicted": "absent",
            },
            {
                "document_id": "sample", "page": 2, "kind": "contents",
                "gold": "absent", "predicted": "present",
            },
            {
                "document_id": "sample", "page": 3, "kind": "contents",
                "gold": "present", "predicted": evaluation.ABSTAIN,
            },
            {
                "document_id": "sample", "page": 4, "kind": "contents",
                "gold": "absent", "predicted": evaluation.ABSTAIN,
            },
        ]
        bundle = evaluation._navigation_metric_bundle(samples)
        metrics = bundle["per_kind"]["contents"]
        self.assertEqual(metrics["false_positive"], 1)
        self.assertEqual(metrics["false_negative"], 1)
        self.assertEqual(metrics["positive_abstain"], 1)
        self.assertEqual(metrics["negative_abstain"], 1)
        self.assertEqual(metrics["coverage"], 0.5)
        self.assertEqual(metrics["overall_accuracy"], 0.0)
        self.assertEqual(metrics["false_positive_rate"], 0.5)
        self.assertEqual(metrics["false_negative_rate"], 1.0)
        self.assertEqual(
            bundle["confusion"]["contents"]["present"],
            {"absent": 1, "present": 0, evaluation.ABSTAIN: 1},
        )
        self.assertEqual(
            bundle["confusion"]["contents"]["absent"],
            {"absent": 0, "present": 1, evaluation.ABSTAIN: 1},
        )

    def test_navigation_release_gate_requires_per_kind_counts_and_documents(self) -> None:
        samples = []
        for kind in evaluation.NAVIGATION_KINDS:
            for index_value in range(20):
                samples.extend((
                    {
                        "document_id": f"doc-{index_value % 5}",
                        "page": index_value + 1,
                        "kind": kind,
                        "gold": "present",
                        "predicted": "present",
                    },
                    {
                        "document_id": f"doc-{index_value % 5}",
                        "page": index_value + 101,
                        "kind": kind,
                        "gold": "absent",
                        "predicted": "absent",
                    },
                ))
        eligible = evaluation._navigation_metric_bundle(samples)
        self.assertTrue(eligible["summary"]["class_support_complete"])
        self.assertTrue(eligible["summary"]["release_gate_eligible"])
        self.assertTrue(all(
            value["release_gate_eligible"]
            for value in eligible["per_kind"].values()
        ))

        below_minimum = evaluation._navigation_metric_bundle(samples[:-1])
        self.assertTrue(below_minimum["summary"]["class_support_complete"])
        self.assertFalse(
            below_minimum["summary"]["release_gate_eligible"],
        )
        self.assertFalse(
            below_minimum["per_kind"]["list_of_tables"][
                "release_gate_eligible"
            ],
        )

    def test_invalid_navigation_presence_is_reported_as_evaluation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, annotations, pdf, model_dir = self.fixture(root)
            navigation = root / "explicit-navigation.jsonl"
            self.write_navigation_labels(
                navigation, pdf, ((1, "contents", "unknown"),),
            )
            with self.assertRaisesRegex(
                evaluation.EvaluationError,
                "unknown navigation presence",
            ):
                evaluation.evaluate(
                    corpus,
                    annotations,
                    navigation_annotations_path=navigation,
                    model_dir=model_dir,
                )

    def test_context_prefers_predecessor_range_over_single_page_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus, annotations, pdf, model_dir = self.fixture(Path(temporary))
            self.write_labels(annotations, pdf, ((2, "legal"),))
            output = pdf.with_suffix(".pdf2md")
            manifest_path = output / "raw" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            full_path = output / "raw" / "cache" / "content-lists" / "one.json"
            pages = json.loads(full_path.read_text(encoding="utf-8"))
            single_path = full_path.with_name("single-two.json")
            single_path.write_text(json.dumps([pages[1]]), encoding="utf-8")
            single = {
                **manifest["selections"][0],
                "pages": "2",
                "content_list_v2": "raw/cache/content-lists/single-two.json",
            }
            manifest["selections"] = [single, manifest["selections"][0]]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with mock.patch.object(
                evaluation,
                "classify_front_regions_v2",
                wraps=evaluation.classify_front_regions_v2,
            ) as classify:
                report = evaluation.evaluate(
                    corpus, annotations, model_dir=model_dir,
                )
            context = report["production_context"]
            selected = context["documents"]["sample"]["selection"]
            self.assertEqual(context["comparability"]["scored_samples"], 1)
            self.assertEqual(selected["pages"], "1-4")
            self.assertEqual(selected["manifest_index"], 1)
            self.assertEqual(selected["context_start_page"], 1)
            self.assertEqual(selected["context_end_page"], 2)
            self.assertEqual(selected["context_page_count"], 2)
            self.assertEqual(
                context["documents"]["sample"]["samples"][0]["selection_offset"], 1,
            )
            context_calls = [
                call for call in classify.call_args_list
                if call.kwargs.get("max_pages") == 2
            ]
            self.assertEqual(len(context_calls), 1)

    def test_single_page_cache_is_explicit_unscored_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus, annotations, pdf, model_dir = self.fixture(Path(temporary))
            self.write_labels(annotations, pdf, ((2, "legal"),))
            output = pdf.with_suffix(".pdf2md")
            manifest_path = output / "raw" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            content_path = output / "raw" / "cache" / "content-lists" / "one.json"
            pages = json.loads(content_path.read_text(encoding="utf-8"))
            content_path.write_text(json.dumps([pages[1]]), encoding="utf-8")
            manifest["selections"][0]["pages"] = "2"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            report = evaluation.evaluate(corpus, annotations, model_dir=model_dir)
            context = report["production_context"]
            document = context["documents"]["sample"]
            self.assertEqual(document["status"], "isolated_fallback")
            self.assertEqual(document["reason"], "only_single_page_cached_selection")
            self.assertEqual(document["samples"][0]["fallback"], "isolated")
            self.assertFalse(document["samples"][0]["scored"])
            self.assertEqual(context["summary"]["total_samples"], 0)
            self.assertEqual(context["comparability"], {
                "verified_documents": 1,
                "scored_documents": 0,
                "unscored_documents": 1,
                "isolated_fallback_documents": 1,
                "unavailable_documents": 0,
                "verified_samples": 1,
                "scored_samples": 0,
                "unscored_samples": 1,
                "coverage": 0.0,
                "reasons": {"only_single_page_cached_selection": 1},
            })
            self.assertNotIn(
                "SECRET", evaluation._serialize(report["production_context"]),
            )

    def test_disjoint_single_page_caches_are_never_joined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus, annotations, pdf, model_dir = self.fixture(Path(temporary))
            self.write_labels(
                annotations, pdf, ((1, "contents"), (3, "body_start")),
            )
            output = pdf.with_suffix(".pdf2md")
            manifest_path = output / "raw" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            original_path = (
                output / "raw" / "cache" / "content-lists" / "one.json"
            )
            pages = json.loads(original_path.read_text(encoding="utf-8"))
            page_one = original_path.with_name("page-one.json")
            page_three = original_path.with_name("page-three.json")
            page_one.write_text(json.dumps([pages[0]]), encoding="utf-8")
            page_three.write_text(json.dumps([pages[2]]), encoding="utf-8")
            base = manifest["selections"][0]
            manifest["selections"] = [
                {
                    **base,
                    "pages": "1",
                    "content_list_v2": "raw/cache/content-lists/page-one.json",
                },
                {
                    **base,
                    "pages": "3",
                    "content_list_v2": "raw/cache/content-lists/page-three.json",
                },
            ]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            report = evaluation.evaluate(corpus, annotations, model_dir=model_dir)
            context = report["production_context"]
            document = context["documents"]["sample"]
            self.assertEqual(document["status"], "unavailable")
            self.assertEqual(
                document["reason"],
                "no_single_contiguous_selection_covers_all_annotated_pages",
            )
            self.assertIsNone(document["selection"])
            self.assertEqual(context["comparability"]["scored_samples"], 0)
            self.assertEqual(context["comparability"]["unavailable_documents"], 1)
            self.assertEqual(context["summary"]["total_samples"], 0)

    def test_atomic_output_and_check_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, annotations, _pdf, _model_dir = self.fixture(root)
            output = root / "reports" / "front-evaluation.json"
            self.assertEqual(evaluation.main([
                "--corpus", str(corpus), "--annotations", str(annotations),
                "--output", str(output),
            ]), 0)
            first = output.read_bytes()
            self.assertEqual(evaluation.main([
                "--corpus", str(corpus), "--annotations", str(annotations),
                "--output", str(output), "--check",
            ]), 0)
            output.write_bytes(first + b" ")
            self.assertEqual(evaluation.main([
                "--corpus", str(corpus), "--annotations", str(annotations),
                "--output", str(output), "--check",
            ]), 1)
            self.assertEqual(output.read_bytes(), first + b" ")
            self.assertFalse(output.with_name(output.name + ".tmp").exists())

    def test_source_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus, annotations, pdf, model_dir = self.fixture(Path(temporary))
            pdf.write_bytes(b"changed")
            with self.assertRaisesRegex(evaluation.EvaluationError, "local PDF SHA-256 mismatch"):
                evaluation.evaluate(corpus, annotations, model_dir=model_dir)


if __name__ == "__main__":
    unittest.main()
