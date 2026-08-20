from __future__ import annotations

from collections import Counter
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "manage_front_training", ROOT / "scripts" / "manage-front-training.py"
)
assert SPEC and SPEC.loader
training = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(training)

SHA = "a" * 64


def document(**changes):
    value = {
        "id": "sample",
        "title": "Sample",
        "language": "en",
        "document_type": "manual",
        "expected_sha256": SHA,
        "expected_front_regions": ["legal", "contents", "body_start"],
        "local_path": "sample.pdf",
    }
    value.update(changes)
    return value


def annotation(**changes):
    value = {
        "schema": training.ANNOTATION_SCHEMA,
        "document_id": "sample",
        "source_sha256": SHA,
        "page": 2,
        "kind": "contents",
        "status": "verified",
        "reviewer": "test-reviewer",
    }
    value.update(changes)
    return value


def navigation_annotation(**changes):
    value = {
        "schema": training.NAVIGATION_ANNOTATION_SCHEMA,
        "document_id": "sample",
        "source_sha256": SHA,
        "page": 2,
        "kind": "contents",
        "presence": "present",
        "status": "verified",
        "reviewer": "test-reviewer",
    }
    value.update(changes)
    return value


class FrontTrainingDataTests(unittest.TestCase):
    def write_corpus(self, root: Path, documents=None) -> Path:
        path = root / "corpus.json"
        path.write_text(
            json.dumps({
                "front_region_schema": "pdf2md.front-regions.v1",
                "documents": documents or [document()],
            }),
            encoding="utf-8",
        )
        return path

    def write_annotations(self, root: Path, records) -> Path:
        path = root / "annotations.jsonl"
        path.write_text(
            "".join(json.dumps(item) + "\n" for item in records),
            encoding="utf-8",
        )
        return path

    def write_inspect(self, root: Path, value: dict) -> Path:
        path = root / "sample.pdf2md" / "raw" / "inspect.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def write_synthetic_pdf(self, root: Path, pages: int = 3) -> tuple[Path, str]:
        path = root / "sample.pdf"
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=72, height=72)
        with path.open("wb") as stream:
            writer.write(stream)
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_synthetic_navigation_page_bounds_use_verified_pdf_without_inspect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf, digest = self.write_synthetic_pdf(root)
            item = document(
                document_type="synthetic-front-matter",
                expected_sha256=digest,
                expected_size=pdf.stat().st_size,
            )
            corpus = {"documents": [item]}
            record = navigation_annotation(source_sha256=digest, page=3)
            self.assertEqual(
                training.validate_navigation_annotations([record], corpus, data_dir=root),
                [record],
            )
            with self.assertRaisesRegex(training.TrainingDataError, "exceeds"):
                training.validate_navigation_annotations(
                    [{**record, "page": 4}], corpus, data_dir=root
                )
            pdf.write_bytes(pdf.read_bytes() + b"tampered")
            with self.assertRaisesRegex(training.TrainingDataError, "size does not match"):
                training.validate_navigation_annotations([record], corpus, data_dir=root)

    def test_synthetic_navigation_page_bounds_reject_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with outside.open("wb") as stream:
                writer.write(stream)
            linked = root / "sample.pdf"
            try:
                os.symlink(outside, linked)
            except OSError as error:
                self.skipTest(f"file symlinks unavailable: {error}")
            digest = hashlib.sha256(outside.read_bytes()).hexdigest()
            item = document(
                document_type="synthetic-front-matter",
                expected_sha256=digest,
                expected_size=outside.stat().st_size,
            )
            with self.assertRaisesRegex(training.TrainingDataError, "regular file"):
                training.validate_navigation_annotations(
                    [navigation_annotation(source_sha256=digest, page=1)],
                    {"documents": [item]},
                    data_dir=root,
                )

    def test_repository_sources_and_verified_seed_are_valid(self):
        sources = training.load_sources(ROOT / "data" / "training" / "sources.json")
        by_id = {item["id"]: item for item in sources["datasets"]}
        self.assertEqual(
            set(by_id),
            {"doclaynet", "grotoap2", "pmc-open-access-subset", "comphrdoc"},
        )
        self.assertTrue(all(item["download_policy"] == "manual-only" for item in by_id.values()))
        self.assertEqual(
            by_id["pmc-open-access-subset"]["training_use"],
            "conditional-per-item-license",
        )
        self.assertEqual(
            by_id["comphrdoc"]["training_use"],
            "conditional-image-license",
        )

        corpus = training.load_corpus(ROOT / "data" / "corpus.json")
        records = training.load_annotations(
            ROOT / "data" / "training" / "annotations.jsonl",
            corpus_path=ROOT / "data" / "corpus.json",
        )
        smoke = {
            item["id"] for item in corpus["documents"]
            if item.get("suite") == "smoke" and item.get("url") is not None
        }
        annotated_documents = {item["document_id"] for item in records}
        self.assertGreaterEqual(len(records), 37)
        self.assertTrue(smoke.issubset(annotated_documents))
        self.assertTrue({
            "lmu-tao-2025-strontium-tweezer-thesis",
            "ucl-palmer-2020-rydberg-interferometry-thesis",
            "harvard-wang-2025-dual-species-arrays-thesis",
            "adi-ad7606b-datasheet",
            "adi-ad7792-7793-datasheet-zh",
            "arxiv-yin-2024-heisenberg-metrology-v2",
            "scipost-girvin-2023-quantum-error-correction",
            "scipost-mbeng-2024-quantum-ising-chain",
            "scipost-fazio-2025-many-body-open-quantum-systems",
        }.issubset(annotated_documents))
        self.assertTrue(all(item["status"] == "verified" for item in records))
        self.assertTrue(all(set(item) == training.ANNOTATION_FIELDS for item in records))

        navigation = training.load_navigation_annotations(
            ROOT / "data" / "training" / "navigation-annotations.jsonl",
            corpus_path=ROOT / "data" / "corpus.json",
        )
        self.assertEqual(len(navigation), 27)
        self.assertEqual(
            {item["kind"] for item in navigation},
            {"contents", "list_of_figures", "list_of_tables"},
        )
        self.assertEqual(
            {item["document_id"] for item in navigation},
            {
                "espressif-esp32-trm-zh",
                "harvard-wang-2025-dual-species-arrays-thesis",
                "lmu-tao-2025-strontium-tweezer-thesis",
                "arxiv-yin-2024-heisenberg-metrology-v2",
                "scipost-girvin-2023-quantum-error-correction",
                "scipost-mbeng-2024-quantum-ising-chain",
                "scipost-fazio-2025-many-body-open-quantum-systems",
            },
        )
        self.assertTrue(all(item["status"] == "verified" for item in navigation))
        self.assertEqual(
            Counter(item["presence"] for item in navigation),
            {"present": 9, "absent": 18},
        )
        self.assertTrue(
            all(set(item) == training.NAVIGATION_ANNOTATION_FIELDS for item in navigation)
        )

    def test_navigation_labels_are_additive_to_primary_page_kind(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = training.load_corpus(self.write_corpus(root))
            self.write_inspect(root, {"page_count": 3, "source": {"sha256": SHA}})
            primary = annotation(kind="abstract")
            navigation = [
                navigation_annotation(kind="contents"),
                navigation_annotation(kind="list_of_figures", presence="absent"),
            ]
            self.assertEqual(
                training.validate_annotations([primary], corpus, data_dir=root),
                [primary],
            )
            self.assertEqual(
                training.validate_navigation_annotations(
                    navigation, corpus, data_dir=root
                ),
                navigation,
            )

    def test_navigation_contract_rejects_invalid_or_unbounded_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = training.load_corpus(self.write_corpus(root))
            bad_without_inspect = navigation_annotation()
            with self.assertRaisesRegex(training.TrainingDataError, "inspect metadata"):
                training.validate_navigation_annotations(
                    [bad_without_inspect], corpus, data_dir=root
                )

            self.write_inspect(root, {"page_count": 3, "source": {"sha256": SHA}})
            bad_values = [
                {"schema": "pdf2md.front-navigation-label.v0"},
                {"kind": "abstract"},
                {"presence": "unknown"},
                {"source_sha256": "A" * 64},
                {"source_sha256": "b" * 64},
                {"page": 0},
                {"page": True},
                {"page": 4},
                {"status": "gold"},
                {"reviewer": ""},
                {"reviewer": "auto:rule", "status": "verified"},
                {"text": "must not be stored"},
                {"bbox": [0, 0, 1, 1]},
                {"image": "must-not-exist.png"},
            ]
            for changes in bad_values:
                with self.subTest(changes=changes):
                    with self.assertRaises(training.TrainingDataError):
                        training.validate_navigation_annotations(
                            [navigation_annotation(**changes)], corpus, data_dir=root
                        )

    def test_navigation_rejects_same_kind_duplicate_but_allows_distinct_kinds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = training.load_corpus(self.write_corpus(root))
            self.write_inspect(root, {"page_count": 3, "source": {"sha256": SHA}})
            distinct = [
                navigation_annotation(kind="contents"),
                navigation_annotation(kind="list_of_tables", presence="absent"),
            ]
            self.assertEqual(
                training.validate_navigation_annotations(
                    distinct, corpus, data_dir=root
                ),
                distinct,
            )
            with self.assertRaisesRegex(training.TrainingDataError, "duplicate contents"):
                training.validate_navigation_annotations(
                    [navigation_annotation(), navigation_annotation(status="needs_review")],
                    corpus,
                    data_dir=root,
                )

    def test_annotation_contract_rejects_bad_schema_kind_hash_page_status_and_reviewer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = training.load_corpus(self.write_corpus(root))
            bad_values = [
                {"schema": "pdf2md.front-page-label.v0"},
                {"kind": "title"},
                {"source_sha256": "A" * 64},
                {"source_sha256": "b" * 64},
                {"page": 0},
                {"page": True},
                {"status": "gold"},
                {"reviewer": ""},
                {"reviewer": "auto:rule", "status": "verified"},
                {"text": "must not be stored"},
            ]
            for changes in bad_values:
                with self.subTest(changes=changes):
                    with self.assertRaises(training.TrainingDataError):
                        training.validate_annotations(
                            [annotation(**changes)], corpus, data_dir=root
                        )

    def test_annotation_rejects_duplicate_page_and_page_beyond_inspect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = training.load_corpus(self.write_corpus(root))
            self.write_inspect(root, {"page_count": 3, "source": {"sha256": SHA}})
            with self.assertRaisesRegex(training.TrainingDataError, "duplicate page"):
                training.validate_annotations(
                    [annotation(), annotation(kind="legal")], corpus, data_dir=root
                )
            with self.assertRaisesRegex(training.TrainingDataError, "exceeds"):
                training.validate_annotations(
                    [annotation(page=4)], corpus, data_dir=root
                )
            self.write_inspect(root, {"page_count": 3, "source": {"sha256": "b" * 64}})
            with self.assertRaisesRegex(training.TrainingDataError, "hash does not match"):
                training.validate_annotations([annotation()], corpus, data_dir=root)

    def test_local_pdf_path_rejects_parent_and_windows_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for local_path in ("../sample.pdf", "C:/sample.pdf", "C:\\sample.pdf"):
                with self.subTest(local_path=local_path):
                    with self.assertRaisesRegex(training.TrainingDataError, "unsafe"):
                        training.inspect_path(document(local_path=local_path), root)

    def test_bootstrap_uses_only_pinned_high_confidence_hints_as_needs_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = training.load_corpus(self.write_corpus(root))
            self.write_inspect(root, {
                "source": {"sha256": SHA},
                "page_count": 8,
                "toc_candidate_pages": [2, 99, "3"],
                "outline": [
                    {"title": "Copyright", "pdf_page": 1, "depth": 0},
                    {"title": "Table of Contents", "pdf_page": 2, "depth": 1},
                    {"title": "Introduction", "pdf_page": 5, "depth": 0},
                    {"title": "Background", "pdf_page": 6, "depth": 0},
                ],
            })
            existing = [annotation()]
            candidates, warnings = training.build_bootstrap_candidates(
                corpus, existing, data_dir=root
            )
            self.assertEqual(warnings, [])
            self.assertEqual(
                [(item["page"], item["kind"]) for item in candidates],
                [(1, "legal"), (5, "body_start")],
            )
            self.assertTrue(all(item["status"] == "needs_review" for item in candidates))
            self.assertTrue(all(item["reviewer"].startswith("auto:") for item in candidates))
            self.assertTrue(all(set(item) == training.ANNOTATION_FIELDS for item in candidates))

    def test_bootstrap_skips_mismatched_or_unpinned_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = training.load_corpus(self.write_corpus(root))
            self.write_inspect(root, {
                "source": {"sha256": "b" * 64},
                "page_count": 3,
                "toc_candidate_pages": [2],
                "outline": [],
            })
            candidates, warnings = training.build_bootstrap_candidates(
                corpus, [], data_dir=root
            )
            self.assertEqual(candidates, [])
            self.assertTrue(any("hash does not match" in warning for warning in warnings))

            unpinned = training.load_corpus(
                self.write_corpus(root, [document(expected_sha256=None)])
            )
            candidates, warnings = training.build_bootstrap_candidates(
                unpinned, [], data_dir=root
            )
            self.assertEqual(candidates, [])
            self.assertTrue(any("not hash-pinned" in warning for warning in warnings))

    def test_export_review_is_metadata_only_and_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus_path = self.write_corpus(root)
            annotations_path = self.write_annotations(root, [annotation()])
            candidate = annotation(
                page=1,
                kind="legal",
                status="needs_review",
                reviewer="auto:inspect-outline-v1",
            )
            candidates_path = self.write_annotations(root, [candidate])
            output = root / "local" / "review.json"
            result = training.main([
                "--corpus", str(corpus_path),
                "--annotations", str(annotations_path),
                "export-review",
                "--candidates", str(candidates_path),
                "--output", str(output),
            ])
            self.assertEqual(result, 0)
            queue = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(queue["schema"], training.REVIEW_SCHEMA)
            self.assertEqual(len(queue["items"]), 1)
            item = queue["items"][0]
            self.assertEqual(item["page"], 1)
            self.assertEqual(item["local_pdf"], "sample.pdf")
            self.assertEqual(item["inspect_json"], "sample.pdf2md/raw/inspect.json")
            forbidden = {"text", "image", "ocr", "content", "pixels"}
            self.assertTrue(forbidden.isdisjoint(item))


if __name__ == "__main__":
    unittest.main()
