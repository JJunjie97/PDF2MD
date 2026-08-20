from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_front_region_synthetic",
    ROOT / "scripts" / "generate-front-region-synthetic.py",
)
assert SPEC and SPEC.loader
synthetic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = synthetic
SPEC.loader.exec_module(synthetic)

TRAINING_SPEC = importlib.util.spec_from_file_location(
    "manage_front_training_for_synthetic_test",
    ROOT / "scripts" / "manage-front-training.py",
)
assert TRAINING_SPEC and TRAINING_SPEC.loader
training = importlib.util.module_from_spec(TRAINING_SPEC)
sys.modules[TRAINING_SPEC.name] = training
TRAINING_SPEC.loader.exec_module(training)

CORPUS_SPEC = importlib.util.spec_from_file_location(
    "manage_corpus_for_synthetic_test",
    ROOT / "scripts" / "manage-corpus.py",
)
assert CORPUS_SPEC and CORPUS_SPEC.loader
corpus_manager = importlib.util.module_from_spec(CORPUS_SPEC)
sys.modules[CORPUS_SPEC.name] = corpus_manager
CORPUS_SPEC.loader.exec_module(corpus_manager)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_tree(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def write_inspect_metadata(output: Path, documents: list[dict[str, object]]) -> None:
    """Create the pinned minimum cache contract required by the validator."""
    for document in documents:
        pdf_path = output / str(document["pdf_path"])
        inspect_path = pdf_path.with_name(pdf_path.name + "2md") / "raw" / "inspect.json"
        inspect_path.parent.mkdir(parents=True)
        inspect_path.write_text(
            json.dumps(
                {
                    "source": {"sha256": document["pdf_sha256"]},
                    "page_count": document["page_count"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )


def downgrade_manifest_to_v1(output: Path) -> dict[str, object]:
    """Turn a generated v2 fixture into the exact legacy ownership shape."""
    provenance_path = output / synthetic.PROVENANCE_NAME
    manifest = json.loads(provenance_path.read_text(encoding="utf-8"))
    (output / synthetic.NAVIGATION_ANNOTATIONS_NAME).unlink()
    manifest["schema"] = synthetic.LEGACY_MANIFEST_SCHEMA
    manifest["generator"]["version"] = synthetic.LEGACY_GENERATOR_VERSION
    manifest.pop("navigation_annotation_schema")
    manifest.pop("navigation_kinds")
    manifest.pop("navigation_annotations")
    for document in manifest["documents"]:
        document.pop("page_navigation_labels")
    provenance_path.write_text(
        synthetic._stable_json(manifest) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


class FrontRegionSyntheticTests(unittest.TestCase):
    def test_small_corpus_is_complete_valid_and_reproducible(self):
        with tempfile.TemporaryDirectory() as first_temp, tempfile.TemporaryDirectory() as second_temp:
            first = Path(first_temp)
            second = Path(second_temp)
            manifest_a = synthetic.generate(first, documents=4, seed=731)
            manifest_b = synthetic.generate(second, documents=4, seed=731)

            self.assertEqual(manifest_a, manifest_b)
            self.assertEqual(manifest_a["schema"], "pdf2md.synthetic-front-corpus.v2")
            self.assertEqual(manifest_a["annotation_schema"], "pdf2md.front-page-label.v1")
            self.assertEqual(
                manifest_a["navigation_annotation_schema"],
                "pdf2md.front-navigation-label.v1",
            )
            self.assertEqual(manifest_a["navigation_kinds"], list(synthetic.NAVIGATION_KINDS))
            self.assertEqual(manifest_a["provenance"]["license"], "CC0-1.0")
            self.assertFalse(manifest_a["provenance"]["contains_third_party_content"])
            generator = manifest_a["generator"]
            self.assertEqual(generator["source"]["path"], "generate-front-region-synthetic.py")
            self.assertEqual(
                generator["source"]["sha256"],
                sha256(ROOT / "scripts" / "generate-front-region-synthetic.py"),
            )
            self.assertEqual(generator["runtime"]["python"]["implementation"], "CPython")
            self.assertRegex(generator["runtime"]["python"]["version"], r"^\d+\.\d+\.\d+$")
            self.assertRegex(generator["runtime"]["reportlab"]["version"], r"^\d+\.\d+")
            self.assertEqual(
                {item["name"] for item in generator["runtime"]["fonts"]},
                {"Helvetica", "Helvetica-Bold", "STSong-Light"},
            )

            documents = manifest_a["documents"]
            self.assertEqual({item["language"] for item in documents}, {"en", "zh-CN"})
            self.assertEqual({item["template"] for item in documents}, {"full", "no_toc"})
            full_by_language = {item["language"]: item for item in documents if item["template"] == "full"}
            self.assertEqual(set(full_by_language), {"en", "zh-CN"})
            for item in full_by_language.values():
                self.assertEqual(set(item["page_labels"]), set(synthetic.PAGE_ROLES))

            for item in documents:
                path_a = first / item["pdf_path"]
                path_b = second / item["pdf_path"]
                self.assertTrue(path_a.read_bytes().startswith(b"%PDF-"))
                self.assertEqual(sha256(path_a), item["pdf_sha256"])
                self.assertEqual(sha256(path_a), sha256(path_b))
                self.assertEqual(len(PdfReader(str(path_a)).pages), item["page_count"])

            rows = [json.loads(line) for line in (first / "annotations.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), sum(item["page_count"] for item in documents))
            navigation_path = first / synthetic.NAVIGATION_ANNOTATIONS_NAME
            navigation_rows = [
                json.loads(line)
                for line in navigation_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(navigation_rows), len(rows) * len(synthetic.NAVIGATION_KINDS))
            self.assertEqual(len(navigation_rows), 114)
            self.assertEqual(
                manifest_a["navigation_annotations"],
                {
                    "path": synthetic.NAVIGATION_ANNOTATIONS_NAME,
                    "sha256": sha256(navigation_path),
                    "records": len(navigation_rows),
                },
            )
            self.assertEqual(
                sha256(navigation_path),
                sha256(second / synthetic.NAVIGATION_ANNOTATIONS_NAME),
            )
            by_document = {item["document_id"]: item for item in documents}
            corpus = {
                "documents": [
                    {
                        "id": item["document_id"],
                        "expected_sha256": item["pdf_sha256"],
                        "local_path": item["pdf_path"],
                    }
                    for item in documents
                ]
            }
            self.assertEqual(training.validate_annotations(rows, corpus, data_dir=first), rows)
            write_inspect_metadata(first, documents)
            self.assertEqual(
                training.validate_navigation_annotations(
                    navigation_rows,
                    corpus,
                    data_dir=first,
                ),
                navigation_rows,
            )
            generated_corpus = corpus_manager.load_manifest(first / "corpus.json")
            self.assertTrue(all(item["training_eligible"] for item in generated_corpus["documents"]))
            self.assertEqual(
                {item["license_class"] for item in generated_corpus["documents"]},
                {"cc0-1.0"},
            )
            self.assertEqual(manifest_a["training_corpus"]["license"], "CC0-1.0")
            self.assertEqual(
                manifest_a["training_corpus"]["sha256"],
                sha256(first / manifest_a["training_corpus"]["path"]),
            )
            pages_by_document: dict[str, list[int]] = {}
            for row in rows:
                self.assertEqual(row["schema"], "pdf2md.front-page-label.v1")
                self.assertEqual(
                    set(row),
                    {"schema", "document_id", "source_sha256", "page", "kind", "status", "reviewer"},
                )
                self.assertEqual(row["status"], "verified")
                self.assertFalse(row["reviewer"].startswith("auto:"))
                document = by_document[row["document_id"]]
                self.assertEqual(row["source_sha256"], document["pdf_sha256"])
                self.assertIn(row["kind"], synthetic.PAGE_ROLES)
                self.assertGreaterEqual(row["page"], 1)
                self.assertLessEqual(row["page"], document["page_count"])
                pages_by_document.setdefault(row["document_id"], []).append(row["page"])
            for document_id, pages in pages_by_document.items():
                self.assertEqual(pages, list(range(1, by_document[document_id]["page_count"] + 1)))

            navigation_by_page: dict[tuple[str, int], dict[str, str]] = {}
            for row in navigation_rows:
                self.assertEqual(row["schema"], "pdf2md.front-navigation-label.v1")
                self.assertEqual(
                    set(row),
                    {
                        "schema",
                        "document_id",
                        "source_sha256",
                        "page",
                        "kind",
                        "presence",
                        "status",
                        "reviewer",
                    },
                )
                self.assertEqual(row["status"], "verified")
                self.assertFalse(row["reviewer"].startswith("auto:"))
                document = by_document[row["document_id"]]
                self.assertEqual(row["source_sha256"], document["pdf_sha256"])
                self.assertIn(row["kind"], synthetic.NAVIGATION_KINDS)
                self.assertIn(row["presence"], {"present", "absent"})
                key = (row["document_id"], row["page"])
                navigation_by_page.setdefault(key, {})[row["kind"]] = row["presence"]

            for document in documents:
                for page, planned_kinds in enumerate(
                    document["page_navigation_labels"],
                    start=1,
                ):
                    labels = navigation_by_page[(document["document_id"], page)]
                    self.assertEqual(set(labels), set(synthetic.NAVIGATION_KINDS))
                    self.assertEqual(
                        {kind for kind, presence in labels.items() if presence == "present"},
                        set(planned_kinds),
                    )

    def test_rerun_removes_only_stale_generated_pdfs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            synthetic.generate(output, documents=4, seed=9)
            keep = output / "keep.txt"
            keep.write_text("unrelated", encoding="utf-8")
            synthetic.generate(output, documents=2, seed=9)
            self.assertTrue(keep.is_file())
            self.assertEqual(len(list(output.glob("*.pdf"))), 2)

    def test_unowned_target_names_are_never_overwritten(self):
        targets = (
            "synthetic-en-full-0001.pdf",
            synthetic.ANNOTATIONS_NAME,
            synthetic.NAVIGATION_ANNOTATIONS_NAME,
            synthetic.CORPUS_NAME,
            synthetic.PROVENANCE_NAME,
        )
        for target_name in targets:
            with self.subTest(target_name=target_name), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                (output / target_name).write_bytes(b"user-owned")
                (output / "keep.txt").write_bytes(b"unrelated")
                before = snapshot_tree(output)
                with self.assertRaises(synthetic.UnsafeOutputError):
                    synthetic.generate(output, documents=1, seed=9)
                self.assertEqual(snapshot_tree(output), before)

    def test_manifest_shape_alone_cannot_authorize_deletion(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            owned = output / "synthetic-en-full-0001.pdf"
            owned.write_bytes(b"user-owned")
            (output / synthetic.ANNOTATIONS_NAME).write_bytes(b"user annotations")
            (output / synthetic.CORPUS_NAME).write_bytes(b"user corpus")
            (output / synthetic.PROVENANCE_NAME).write_text(
                json.dumps(
                    {
                        "schema": synthetic.MANIFEST_SCHEMA,
                        "documents": [
                            {
                                "document_id": owned.stem,
                                "pdf_path": owned.name,
                                "pdf_sha256": sha256(owned),
                                "bytes": owned.stat().st_size,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            before = snapshot_tree(output)
            with self.assertRaises(synthetic.UnsafeOutputError):
                synthetic.generate(output, documents=1, seed=9)
            self.assertEqual(snapshot_tree(output), before)

    def test_modified_owned_artifacts_block_overwrite_and_stale_deletion(self):
        targets = (
            "synthetic-zh-full-0002.pdf",
            synthetic.ANNOTATIONS_NAME,
            synthetic.NAVIGATION_ANNOTATIONS_NAME,
            synthetic.CORPUS_NAME,
        )
        for target_name in targets:
            with self.subTest(target_name=target_name), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                synthetic.generate(output, documents=2, seed=9)
                target = output / target_name
                target.write_bytes(target.read_bytes() + b"user edit")
                before = snapshot_tree(output)
                # The second PDF would be stale on this shorter rerun.  It must
                # still be checked against the old manifest before any deletion.
                with self.assertRaises(synthetic.UnsafeOutputError):
                    synthetic.generate(output, documents=1, seed=9)
                self.assertEqual(snapshot_tree(output), before)

    def test_verified_legacy_v1_manifest_migrates_atomically_to_v2(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            synthetic.generate(output, documents=4, seed=731)
            expected_v2 = snapshot_tree(output)
            legacy = downgrade_manifest_to_v1(output)

            self.assertEqual(legacy["schema"], synthetic.LEGACY_MANIFEST_SCHEMA)
            self.assertFalse((output / synthetic.NAVIGATION_ANNOTATIONS_NAME).exists())
            migrated = synthetic.generate(output, documents=4, seed=731)

            self.assertEqual(migrated["schema"], synthetic.MANIFEST_SCHEMA)
            self.assertEqual(snapshot_tree(output), expected_v2)

    def test_legacy_v1_migration_fails_closed_for_missing_tampered_or_colliding_assets(self):
        cases = ("missing", "tampered", "navigation_collision")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                synthetic.generate(output, documents=2, seed=17)
                downgrade_manifest_to_v1(output)
                if case == "missing":
                    (output / synthetic.CORPUS_NAME).unlink()
                elif case == "tampered":
                    annotations = output / synthetic.ANNOTATIONS_NAME
                    annotations.write_bytes(annotations.read_bytes() + b"user edit")
                else:
                    (output / synthetic.NAVIGATION_ANNOTATIONS_NAME).write_bytes(
                        b"user-owned navigation labels"
                    )
                before = snapshot_tree(output)

                with self.assertRaises(synthetic.UnsafeOutputError):
                    synthetic.generate(output, documents=2, seed=17)
                self.assertEqual(snapshot_tree(output), before)

    def test_legacy_v1_migration_commit_failure_restores_v1_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "generated"
            synthetic.generate(output, documents=2, seed=17)
            downgrade_manifest_to_v1(output)
            before = snapshot_tree(output)
            real_replace = synthetic.os.replace
            injected = False

            def fail_once_after_navigation_install(source, destination):
                nonlocal injected
                source_path = Path(source)
                if (
                    not injected
                    and source_path.name == synthetic.CORPUS_NAME
                    and source_path.parent.name.startswith(".pdf2md-synthetic-stage-")
                ):
                    injected = True
                    raise OSError("injected migration commit failure")
                return real_replace(source, destination)

            with mock.patch.object(
                synthetic.os,
                "replace",
                side_effect=fail_once_after_navigation_install,
            ):
                with self.assertRaisesRegex(OSError, "injected migration commit failure"):
                    synthetic.generate(output, documents=2, seed=17)
            self.assertTrue(injected)
            self.assertEqual(snapshot_tree(output), before)
            self.assertFalse((output / synthetic.NAVIGATION_ANNOTATIONS_NAME).exists())
            self.assertEqual([item.name for item in root.iterdir()], ["generated"])

    def test_unrelated_and_unowned_non_target_files_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            unrelated = output / "notes.txt"
            unrelated.write_bytes(b"keep notes")
            extra_pdf = output / "synthetic-en-full-9999.pdf"
            extra_pdf.write_bytes(b"keep synthetic-shaped file")
            synthetic.generate(output, documents=2, seed=17)
            self.assertEqual(unrelated.read_bytes(), b"keep notes")
            self.assertEqual(extra_pdf.read_bytes(), b"keep synthetic-shaped file")
            self.assertEqual(len(list(output.glob("*.pdf"))), 3)

    def test_render_failure_leaves_existing_corpus_byte_for_byte_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "generated"
            synthetic.generate(output, documents=2, seed=9)
            before = snapshot_tree(output)
            real_write_pdf = synthetic._write_pdf
            calls = 0

            def fail_on_second_pdf(path, plan):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("injected render failure")
                return real_write_pdf(path, plan)

            with mock.patch.object(synthetic, "_write_pdf", side_effect=fail_on_second_pdf):
                with self.assertRaisesRegex(RuntimeError, "injected render failure"):
                    synthetic.generate(output, documents=3, seed=11)
            self.assertEqual(snapshot_tree(output), before)
            self.assertEqual([item.name for item in root.iterdir()], ["generated"])

    def test_commit_failure_rolls_back_all_replacements(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "generated"
            synthetic.generate(output, documents=2, seed=9)
            before = snapshot_tree(output)
            real_replace = synthetic.os.replace
            injected = False

            def fail_once_on_corpus(source, destination):
                nonlocal injected
                source_path = Path(source)
                if (
                    not injected
                    and source_path.name == synthetic.CORPUS_NAME
                    and source_path.parent.name.startswith(".pdf2md-synthetic-stage-")
                ):
                    injected = True
                    raise OSError("injected commit failure")
                return real_replace(source, destination)

            with mock.patch.object(synthetic.os, "replace", side_effect=fail_once_on_corpus):
                with self.assertRaisesRegex(OSError, "injected commit failure"):
                    synthetic.generate(output, documents=3, seed=11)
            self.assertTrue(injected)
            self.assertEqual(snapshot_tree(output), before)
            self.assertEqual([item.name for item in root.iterdir()], ["generated"])

    def test_invalid_document_count_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "at least 1"):
                synthetic.generate(Path(temporary), documents=0, seed=1)


if __name__ == "__main__":
    unittest.main()
