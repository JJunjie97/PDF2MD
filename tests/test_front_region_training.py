from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "train_front_region_model", ROOT / "scripts" / "train-front-region-model.py"
)
assert SPEC and SPEC.loader
trainer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trainer)

from pdf2md_region_models import resolve_artifact  # noqa: E402


def block(text: str, label: str = "text", score: float = 0.9) -> dict:
    return {
        "type": "paragraph", "bbox": [1, 2, 100, 200],
        "content": {"paragraph_content": [{"type": "text", "content": text}]},
        "_pdf2md.layout": {"label": label, "score": score, "index": 1},
    }


def navigation_annotation(
    sha: str,
    *,
    document_id: str = "sample",
    page: int = 4,
    kind: str = "contents",
    presence: str = "present",
) -> dict:
    return {
        "schema": trainer.NAVIGATION_ANNOTATION_SCHEMA,
        "document_id": document_id,
        "source_sha256": sha,
        "page": page,
        "kind": kind,
        "presence": presence,
        "status": "verified",
        "reviewer": "human-test",
    }


class FrontRegionTrainingTests(unittest.TestCase):
    def fixture(
        self, root: Path, *, document_id: str = "sample", pages: str = "3-4",
        training_eligible: bool = True, redistributable: bool | None = None,
        synthetic: bool = False,
    ) -> tuple[Path, Path, Path, str]:
        pdf = root / "sample.pdf"
        pdf.write_bytes(b"%PDF-fake-training-fixture")
        sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        output = pdf.with_suffix(".pdf2md")
        content = output / "raw" / "cache" / "content-lists" / "one.json"
        content.parent.mkdir(parents=True, exist_ok=True)
        content.write_text(
            json.dumps([[block("page three")], [block("Contents page four", "content", 0.98)]]),
            encoding="utf-8",
        )
        manifest = {
            "schema_version": 2, "core_version": "test-core", "cache_version": "test-cache",
            "source": {"sha256": sha, "name": pdf.name},
            "selections": [{
                "pages": pages, "content_list_v2": "raw/cache/content-lists/one.json",
                "profile": "balanced", "method": "hybrid", "requested_method": "auto",
                "language": "ch",
            }],
        }
        (output / "raw" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        redistributable = training_eligible if redistributable is None else redistributable
        normal_document = {
            "id": document_id, "local_path": pdf.name, "expected_sha256": sha,
            "training_eligible": training_eligible, "redistributable": redistributable,
        }
        corpus_value = {
            "front_region_schema": "pdf2md.front-regions.v1", "documents": [normal_document],
        }
        corpus = root / "corpus.json"
        corpus.write_text(json.dumps(corpus_value), encoding="utf-8")
        annotation = {
            "schema": trainer.ANNOTATION_SCHEMA, "document_id": document_id,
            "source_sha256": sha, "page": 4, "kind": "contents",
            "status": "verified", "reviewer": "human-test",
        }
        annotations = root / "annotations.jsonl"
        annotations.write_text(json.dumps(annotation) + "\n", encoding="utf-8")
        if synthetic:
            normal_document.update({
                "document_type": "synthetic-front-matter", "license_class": "cc0-1.0",
                "training_eligible": True, "redistributable": True,
            })
            bound_corpus = root / "training-corpus.json"
            bound_corpus.write_text(json.dumps(corpus_value), encoding="utf-8")
            corpus_value = {
                "schema": trainer.LEGACY_SYNTHETIC_SCHEMA,
                "annotation_schema": trainer.ANNOTATION_SCHEMA,
                "provenance": {
                    "license": "CC0-1.0", "contains_third_party_content": False,
                    "source": "project-generated",
                },
                "annotations": {
                    "path": annotations.name,
                    "sha256": hashlib.sha256(annotations.read_bytes()).hexdigest(),
                },
                "training_corpus": {
                    "path": bound_corpus.name,
                    "sha256": hashlib.sha256(bound_corpus.read_bytes()).hexdigest(),
                    "license": "CC0-1.0",
                },
                "documents": [{
                    "document_id": document_id, "pdf_path": pdf.name,
                    "pdf_sha256": sha, "language": "en", "page_count": 4,
                }],
            }
            corpus.write_text(json.dumps(corpus_value), encoding="utf-8")
        return corpus, annotations, pdf, sha

    def upgrade_synthetic_fixture_to_v2(
        self, corpus: Path, annotations: Path, sha: str, *, document_id: str = "sample",
    ) -> Path:
        manifest = json.loads(corpus.read_text(encoding="utf-8"))
        navigation = corpus.parent / "navigation-annotations.jsonl"
        rows = []
        page_navigation_labels = []
        for page in range(1, 5):
            positives = ["contents"] if page == 4 else []
            page_navigation_labels.append(positives)
            for kind in trainer.NAVIGATION_KINDS:
                rows.append(navigation_annotation(
                    sha, document_id=document_id, page=page, kind=kind,
                    presence="present" if kind in positives else "absent",
                ))
        navigation.write_text(
            "".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8",
        )
        manifest["schema"] = trainer.SYNTHETIC_SCHEMA
        manifest["annotations"]["records"] = sum(
            1 for line in annotations.read_text(encoding="utf-8").splitlines() if line.strip()
        )
        manifest["navigation_annotation_schema"] = trainer.NAVIGATION_ANNOTATION_SCHEMA
        manifest["navigation_kinds"] = list(trainer.NAVIGATION_KINDS)
        manifest["navigation_annotations"] = {
            "path": navigation.name,
            "sha256": hashlib.sha256(navigation.read_bytes()).hexdigest(),
            "records": len(rows),
        }
        manifest["documents"][0]["page_navigation_labels"] = page_navigation_labels
        corpus.write_text(json.dumps(manifest), encoding="utf-8")
        return navigation

    def test_physical_page_is_mapped_through_contiguous_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus, annotations, _pdf, _sha = self.fixture(Path(temporary))
            examples, metadata = trainer.load_examples(annotations, corpus)
            self.assertEqual(examples[0]["page"], 4)
            self.assertGreater(examples[0]["layout"].get("layout.count.content", 0), 0)
            self.assertEqual(metadata["source_ids"], ["sample"])
            inputs = metadata["inputs"]
            self.assertEqual(inputs["feature_schema_version"], trainer.FEATURES_VERSION)
            self.assertEqual(len(inputs["content_list_sha256"]), 1)
            self.assertEqual(inputs["selection_config"]["page_numbering"], "physical-1-based")
            selected = inputs["selection_config"]["selections"][0]
            self.assertEqual(selected["conversion"]["profile"], "balanced")
            self.assertEqual(selected["manifest"]["source"]["sha256"], _sha)

    def test_noncontiguous_selection_and_sha_mismatch_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, annotations, _pdf, _sha = self.fixture(root, pages="3,4")
            with self.assertRaisesRegex(trainer.TrainingError, "non-contiguous"):
                trainer.load_examples(annotations, corpus)
            corpus, annotations, pdf, _sha = self.fixture(root)
            pdf.write_bytes(b"changed")
            with self.assertRaisesRegex(trainer.TrainingError, "local PDF SHA-256 mismatch"):
                trainer.load_examples(annotations, corpus)

    def test_regression_only_requires_flag_and_marks_artifact_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus, annotations, _pdf, _sha = self.fixture(Path(temporary), training_eligible=False)
            with self.assertRaisesRegex(trainer.TrainingError, "allow-regression-only"):
                trainer.load_examples(annotations, corpus)
            _examples, metadata = trainer.load_examples(annotations, corpus, allow_regression_only=True)
            self.assertFalse(metadata["redistributable"])
            self.assertFalse(metadata["training_eligible"])
            self.assertTrue(metadata["experiment_only"])

    def test_training_eligible_also_requires_redistributable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus, annotations, _pdf, _sha = self.fixture(
                Path(temporary), training_eligible=True, redistributable=False,
            )
            with self.assertRaisesRegex(trainer.TrainingError, "allow-regression-only"):
                trainer.load_examples(annotations, corpus)
            _examples, metadata = trainer.load_examples(
                annotations, corpus, allow_regression_only=True,
            )
            self.assertFalse(metadata["training_eligible"])
            self.assertFalse(metadata["redistributable"])

    def test_synthetic_provenance_is_training_eligible_and_redistributable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus, annotations, _pdf, _sha = self.fixture(Path(temporary), synthetic=True)
            examples, metadata = trainer.load_examples(annotations, corpus)
            self.assertEqual(len(examples), 1)
            self.assertTrue(metadata["redistributable"])
            self.assertTrue(metadata["training_eligible"])
            self.assertEqual(metadata["source_kinds"], ["synthetic-cc0"])
            self.assertIn("provenance_sha256", metadata["inputs"])

    def test_absolute_and_traversal_paths_are_rejected(self) -> None:
        for bad_path in ("../outside.pdf", "C:/outside.pdf", "/outside.pdf"):
            with self.subTest(path=bad_path), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                corpus, annotations, _pdf, _sha = self.fixture(root)
                value = json.loads(corpus.read_text(encoding="utf-8"))
                value["documents"][0]["local_path"] = bad_path
                corpus.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(trainer.TrainingError, "clean relative path"):
                    trainer.load_examples(annotations, corpus)
    def test_symlink_escape_path_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside_temp:
            root, outside = Path(temporary), Path(outside_temp)
            corpus, annotations, _pdf, sha = self.fixture(root)
            outside_pdf = outside / "escaped.pdf"
            outside_pdf.write_bytes(b"outside")
            link = root / "linked.pdf"
            try:
                os.symlink(outside_pdf, link)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            value = json.loads(corpus.read_text(encoding="utf-8"))
            value["documents"][0].update({"local_path": link.name, "expected_sha256": sha})
            corpus.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(trainer.TrainingError, "escapes its corpus directory"):
                trainer.load_examples(annotations, corpus)

    def test_annotations_require_strict_hash_known_kind_and_human_reviewer(self) -> None:
        cases = (("source_sha256", "A" * 64), ("kind", "invented"), ("reviewer", "AUTO:guess"))
        for field, value in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                corpus, annotations, _pdf, _sha = self.fixture(Path(temporary))
                item = json.loads(annotations.read_text(encoding="utf-8"))
                item[field] = value
                annotations.write_text(json.dumps(item) + "\n", encoding="utf-8")
                with self.assertRaises(trainer.TrainingError):
                    trainer.load_examples(annotations, corpus)

    def test_navigation_labels_are_explicit_masked_and_load_each_page_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, primary, _pdf, sha = self.fixture(root)
            rows = [
                navigation_annotation(sha, kind="contents", presence="present"),
                navigation_annotation(sha, kind="list_of_figures", presence="absent"),
            ]
            navigation = root / "navigation-annotations.jsonl"
            navigation.write_text(
                "".join(json.dumps(item) + "\n" for item in rows),
                encoding="utf-8",
            )
            with mock.patch.object(
                trainer, "_page_from_cache", wraps=trainer._page_from_cache,
            ) as load_page:
                examples, metadata = trainer.load_navigation_examples(
                    navigation, corpus, primary_annotations_path=primary,
                )
            self.assertEqual(load_page.call_count, 1)
            self.assertEqual(len(examples), 1)
            self.assertEqual(
                examples[0]["targets"],
                {"contents": 1, "list_of_figures": 0},
            )
            self.assertNotIn("list_of_tables", examples[0]["targets"])
            self.assertEqual(
                metadata["inputs"]["navigation_annotations_sha256"],
                hashlib.sha256(navigation.read_bytes()).hexdigest(),
            )

    def test_navigation_annotations_reject_invalid_or_duplicate_explicit_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _corpus, _primary, _pdf, sha = self.fixture(root)
            base = navigation_annotation(sha)
            cases = [
                [{**base, "presence": "unknown"}],
                [base, dict(base)],
                [{**base, "kind": "abstract"}],
                [{**base, "reviewer": "AUTO:guess"}],
            ]
            for index, rows in enumerate(cases):
                with self.subTest(index=index):
                    path = root / f"navigation-{index}.jsonl"
                    path.write_text(
                        "".join(json.dumps(item) + "\n" for item in rows),
                        encoding="utf-8",
                    )
                    with self.assertRaises(trainer.TrainingError):
                        trainer.load_navigation_annotations(path)

    def test_custom_corpus_only_discovers_sibling_navigation_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "custom-corpus.json"
            corpus.write_text("{}", encoding="utf-8")
            self.assertIsNone(trainer.resolve_navigation_annotations(corpus, None))
            sibling = root / "navigation-annotations.jsonl"
            sibling.write_text("", encoding="utf-8")
            self.assertEqual(
                trainer.resolve_navigation_annotations(corpus, None),
                sibling,
            )
            explicit = root / "chosen.jsonl"
            self.assertEqual(
                trainer.resolve_navigation_annotations(corpus, explicit),
                explicit,
            )

    def test_synthetic_provenance_binds_claims_corpus_annotations_and_pdfs(self) -> None:
        mutations = ("claims", "corpus", "annotations", "pdf")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                corpus, annotations, pdf, _sha = self.fixture(Path(temporary), synthetic=True)
                manifest = json.loads(corpus.read_text(encoding="utf-8"))
                if mutation == "claims":
                    manifest["provenance"]["contains_third_party_content"] = True
                    corpus.write_text(json.dumps(manifest), encoding="utf-8")
                elif mutation == "corpus":
                    (corpus.parent / manifest["training_corpus"]["path"]).write_text("{}", encoding="utf-8")
                elif mutation == "annotations":
                    annotations.write_text(annotations.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                else:
                    pdf.write_bytes(b"tampered")
                with self.assertRaises(trainer.TrainingError):
                    trainer.load_examples(annotations, corpus)

    def test_synthetic_v2_binds_selected_navigation_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus, annotations, _pdf, sha = self.fixture(Path(temporary), synthetic=True)
            navigation = self.upgrade_synthetic_fixture_to_v2(corpus, annotations, sha)
            documents, input_hashes = trainer.load_documents(
                corpus, annotations, navigation,
            )
            expected_sha = hashlib.sha256(navigation.read_bytes()).hexdigest()
            self.assertEqual(set(documents), {"sample"})
            self.assertEqual(input_hashes["navigation_annotations_sha256"], expected_sha)
            _primary, primary_metadata = trainer.load_examples(annotations, corpus)
            self.assertEqual(
                primary_metadata["inputs"]["navigation_annotations_sha256"], expected_sha,
            )

            copied = corpus.parent / "copied-navigation.jsonl"
            copied.write_bytes(navigation.read_bytes())
            with self.assertRaisesRegex(trainer.TrainingError, "not bound"):
                trainer.load_documents(corpus, annotations, copied)

    def test_synthetic_v2_navigation_tampering_and_contract_mismatch_fail_closed(self) -> None:
        mutations = ("bytes", "records", "inventory", "schema")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                corpus, annotations, _pdf, sha = self.fixture(Path(temporary), synthetic=True)
                navigation = self.upgrade_synthetic_fixture_to_v2(corpus, annotations, sha)
                manifest = json.loads(corpus.read_text(encoding="utf-8"))
                if mutation == "bytes":
                    navigation.write_text(
                        navigation.read_text(encoding="utf-8") + "\n", encoding="utf-8",
                    )
                elif mutation == "records":
                    manifest["navigation_annotations"]["records"] += 1
                    corpus.write_text(json.dumps(manifest), encoding="utf-8")
                elif mutation == "inventory":
                    manifest["documents"][0]["page_navigation_labels"][3] = []
                    corpus.write_text(json.dumps(manifest), encoding="utf-8")
                else:
                    manifest["navigation_annotation_schema"] = "invented"
                    corpus.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(trainer.TrainingError):
                    trainer.load_examples(annotations, corpus)

    def test_legacy_synthetic_provenance_cannot_authorize_navigation_training(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus, annotations, _pdf, sha = self.fixture(Path(temporary), synthetic=True)
            navigation = corpus.parent / "navigation-annotations.jsonl"
            navigation.write_text(
                json.dumps(navigation_annotation(sha)) + "\n", encoding="utf-8",
            )
            with self.assertRaisesRegex(trainer.TrainingError, "legacy synthetic provenance"):
                trainer.load_navigation_examples(
                    navigation, corpus, primary_annotations_path=annotations,
                )

    def test_reproducible_artifacts_load_predict_and_have_document_splits(self) -> None:
        examples = []
        wanted = {"train": 4, "calibration": 2, "test": 2}
        chosen: dict[str, list[str]] = {key: [] for key in wanted}
        index = 0
        while any(len(chosen[key]) < wanted[key] for key in wanted):
            document_id = f"document-{index:04d}"
            split = trainer.split_document(document_id, 73)
            if len(chosen[split]) < wanted[split]:
                chosen[split].append(document_id)
            index += 1
        for ids in chosen.values():
            for offset, document_id in enumerate(ids):
                for repeat in range(2):
                    kind = "contents" if (offset + repeat) % 2 == 0 else "body_start"
                    positive = 1.0 if kind == "contents" else -1.0
                    examples.append({
                        "document_id": document_id, "page": repeat + 1, "kind": kind,
                        "layout": {"layout.mean_score": 0.9, "layout.count.content": max(0.0, positive)},
                        "text": {"text.hash.1": positive, "text.log1p_chars": 3.0, "text.token_count_log1p": 2.0},
                    })
        source_ids = sorted(item for ids in chosen.values() for item in ids)
        source = {"source_ids": source_ids, "redistributable": True, "training_eligible": True, "source_kinds": ["test"]}
        with tempfile.TemporaryDirectory() as first_temp, tempfile.TemporaryDirectory() as second_temp:
            first, second = Path(first_temp), Path(second_temp)
            for field in ("layout", "text"):
                (first / f"navigation-{field}.npz").write_bytes(b"stale")
            metrics = trainer.train(examples, source, output=first, seed=73, epochs=80, allow_small=True)
            trainer.train(examples, source, output=second, seed=73, epochs=80, allow_small=True)
            self.assertEqual((first / "layout.json").read_bytes(), (second / "layout.json").read_bytes())
            self.assertEqual((first / "text.json").read_bytes(), (second / "text.json").read_bytes())
            model = trainer.load_model_artifact(first / "text.json", expected_kind="text")
            self.assertIsNotNone(model)
            self.assertIsNotNone(model.predict(examples[0]["text"]).top)
            artifact = json.loads((first / "text.json").read_text(encoding="utf-8"))
            self.assertTrue(artifact["metadata"]["experimental"])
            self.assertFalse(artifact["metadata"]["approved_for_auto_action"])
            split_docs = [set(metrics["splits"][name]["documents"]) for name in ("train", "calibration", "test")]
            self.assertFalse(split_docs[0] & split_docs[1])
            self.assertFalse(split_docs[0] & split_docs[2])
            self.assertFalse(split_docs[1] & split_docs[2])
            policy = json.loads((first / "policy.json").read_text(encoding="utf-8"))
            self.assertFalse(policy["approved_for_auto_action"])
            self.assertTrue(policy["experimental"])
            self.assertIn("thresholds", policy)
            self.assertIn("margins", policy)
            self.assertEqual(policy["artifact_sha256"], metrics["artifact_sha256"])
            self.assertIn("coverage_risk", metrics["models"]["text"]["test"])
            self.assertEqual(
                metrics["navigation_auxiliary"]["status"],
                "no_explicit_labels",
            )
            self.assertFalse(policy["navigation_auxiliary"]["approved_for_auto_action"])
            self.assertFalse((first / "navigation-layout.npz").exists())
            self.assertFalse((first / "navigation-text.npz").exists())

    def test_navigation_auxiliary_npz_is_masked_deterministic_and_never_approved(self) -> None:
        seed = 73
        wanted = {"train": 4, "calibration": 2, "test": 2}
        chosen: dict[str, list[str]] = {key: [] for key in wanted}
        number = 0
        while any(len(chosen[key]) < wanted[key] for key in wanted):
            document_id = f"aux-document-{number:04d}"
            split = trainer.split_document(document_id, seed)
            if len(chosen[split]) < wanted[split]:
                chosen[split].append(document_id)
            number += 1
        primary = []
        navigation = []
        for ids in chosen.values():
            for document_offset, document_id in enumerate(ids):
                for repeat in range(2):
                    primary_kind = "contents" if repeat == 0 else "body_start"
                    signal = 1.0 if repeat == 0 else -1.0
                    features = {
                        "layout": {
                            "layout.mean_score": 0.9,
                            "layout.count.content": float(repeat == 0),
                        },
                        "text": {
                            "text.hash.1": signal,
                            "text.log1p_chars": 3.0,
                            "text.token_count_log1p": 2.0,
                        },
                    }
                    primary.append({
                        "document_id": document_id,
                        "page": repeat + 1,
                        "kind": primary_kind,
                        **features,
                    })
                    targets = {
                        kind: (repeat + kind_index + document_offset) % 2
                        for kind_index, kind in enumerate(trainer.NAVIGATION_KINDS)
                    }
                    navigation.append({
                        "document_id": document_id,
                        "page": repeat + 1,
                        "targets": targets,
                        **features,
                    })
        # One omitted page/kind is unknown and must not enter support.
        del navigation[0]["targets"]["list_of_tables"]
        source_ids = sorted(item for ids in chosen.values() for item in ids)
        source = {
            "source_ids": source_ids,
            "redistributable": True,
            "training_eligible": True,
            "source_kinds": ["test"],
        }
        navigation_source = {
            **source,
            "inputs": {"navigation_annotations_sha256": "0" * 64},
        }
        with (
            tempfile.TemporaryDirectory() as first_temp,
            tempfile.TemporaryDirectory() as second_temp,
            tempfile.TemporaryDirectory() as primary_only_temp,
        ):
            first, second, primary_only = (
                Path(first_temp), Path(second_temp), Path(primary_only_temp),
            )
            metrics = trainer.train(
                primary,
                source,
                output=first,
                navigation_examples=navigation,
                navigation_source_metadata=navigation_source,
                seed=seed,
                epochs=40,
                allow_small=True,
            )
            trainer.train(
                primary,
                source,
                output=second,
                navigation_examples=navigation,
                navigation_source_metadata=navigation_source,
                seed=seed,
                epochs=40,
                allow_small=True,
            )
            trainer.train(
                primary,
                source,
                output=primary_only,
                seed=seed,
                epochs=40,
                allow_small=True,
            )
            for field in ("layout", "text"):
                artifact = first / f"navigation-{field}.npz"
                self.assertEqual(
                    artifact.read_bytes(),
                    (second / artifact.name).read_bytes(),
                )
                with trainer.np.load(artifact, allow_pickle=False) as archive:
                    self.assertEqual(
                        archive["schema"].item(),
                        trainer.NAVIGATION_AUXILIARY_SCHEMA,
                    )
                    self.assertEqual(
                        archive["navigation_kinds"].tolist(),
                        list(trainer.NAVIGATION_KINDS),
                    )
                    self.assertEqual(archive["weights"].shape[0], 3)
                    self.assertEqual(archive["bias"].shape, (3,))
                    self.assertEqual(archive["temperature"].shape, (3,))
                    self.assertTrue(archive["trained"].all())
                    self.assertTrue(trainer.np.isfinite(archive["weights"]).all())
                    self.assertTrue(trainer.np.isfinite(archive["bias"]).all())
                    self.assertTrue(trainer.np.isfinite(archive["temperature"]).all())
                    self.assertTrue((archive["temperature"] > 0.0).all())
                    self.assertTrue(
                        all(not archive[name].dtype.hasobject for name in archive.files)
                    )
                    metadata = json.loads(archive["metadata_json"].item())
                    self.assertFalse(metadata["approved_for_auto_action"])
                    self.assertTrue(metadata["experimental"])
                    self.assertEqual(metadata["missing_label_semantics"], "unknown")
                self.assertEqual(
                    resolve_artifact(first, field),
                    first / f"{field}.json",
                )
                self.assertIsNone(
                    trainer.load_model_artifact(artifact, expected_kind=field)
                )
            # Adding auxiliary heads does not change either primary artifact.
            self.assertEqual(
                (first / "layout.json").read_bytes(),
                (primary_only / "layout.json").read_bytes(),
            )
            self.assertEqual(
                (first / "text.json").read_bytes(),
                (primary_only / "text.json").read_bytes(),
            )
            auxiliary = metrics["navigation_auxiliary"]
            self.assertEqual(auxiliary["explicit_labels"], len(navigation) * 3 - 1)
            split_docs = [
                set(auxiliary["splits"][name]["documents"])
                for name in ("train", "calibration", "test")
            ]
            self.assertFalse(split_docs[0] & split_docs[1])
            self.assertFalse(split_docs[0] & split_docs[2])
            self.assertFalse(split_docs[1] & split_docs[2])
            policy = json.loads((first / "policy.json").read_text(encoding="utf-8"))
            auxiliary_policy = policy["navigation_auxiliary"]
            self.assertFalse(auxiliary_policy["approved_for_auto_action"])
            self.assertTrue(auxiliary_policy["experimental"])
            for field in ("layout", "text"):
                for kind in trainer.NAVIGATION_KINDS:
                    self.assertFalse(
                        auxiliary_policy["models"][field][kind]["auto_action_gate"]
                    )
                    self.assertEqual(
                        auxiliary_policy["models"][field][kind]["probability"],
                        1.0,
                    )

    def test_default_output_is_staged_outside_live_v1(self) -> None:
        self.assertEqual(trainer.DEFAULT_OUTPUT.name, "candidate")

    def test_allow_small_still_rejects_a_class_unseen_in_train(self) -> None:
        seed = 19
        ids: dict[str, str] = {}
        number = 0
        while set(ids) != {"train", "calibration", "test"}:
            document_id = f"split-{number}"
            ids.setdefault(trainer.split_document(document_id, seed), document_id)
            number += 1
        examples = [
            {"document_id": ids["train"], "page": 1, "kind": "contents", "layout": {"x": 1.0}, "text": {"text.hash.1": 1.0}},
            {"document_id": ids["calibration"], "page": 1, "kind": "body_start", "layout": {"x": -1.0}, "text": {"text.hash.1": -1.0}},
            {"document_id": ids["test"], "page": 1, "kind": "contents", "layout": {"x": 1.0}, "text": {"text.hash.1": 1.0}},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(trainer.TrainingError, "absent from the training-document split"):
                trainer.train(examples, {"source_ids": list(ids.values())}, output=Path(temporary), seed=seed, allow_small=True)


if __name__ == "__main__":
    unittest.main()
