from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-front-eval-plan.py"
SPEC = importlib.util.spec_from_file_location("build_front_eval_plan", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FrontEvalPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "data"
        self.data.mkdir()
        self.manifest = self.data / "corpus.json"

    def _document(
        self,
        document_id: str,
        *,
        page_count: int = 100,
        pdf_kind: str = "text",
        toc_candidate_pages: list[int] | None = None,
        with_inspect: bool = True,
        inspect_digest: str | None = None,
        suite: str = "core",
        expected_front_regions: list[str] | None = None,
    ) -> dict[str, object]:
        relative = Path("downloads") / f"{document_id}.pdf"
        pdf = self.data / relative
        pdf.parent.mkdir(parents=True, exist_ok=True)
        body = (f"binary fixture for {document_id}\n").encode("utf-8")
        pdf.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        if with_inspect:
            inspect = pdf.with_name(pdf.name + "2md") / "raw" / "inspect.json"
            inspect.parent.mkdir(parents=True, exist_ok=True)
            inspect.write_text(json.dumps({
                "ok": True,
                "command": "inspect",
                "source": {"sha256": inspect_digest or digest},
                "page_count": page_count,
                "pdf_kind": pdf_kind,
                "toc_candidate_pages": toc_candidate_pages or [],
            }), encoding="utf-8")
        item: dict[str, object] = {
            "id": document_id,
            "suite": suite,
            "local_path": relative.as_posix(),
            "expected_sha256": digest,
        }
        if expected_front_regions is not None:
            item["expected_front_regions"] = expected_front_regions
        return item

    def _write_manifest(self, documents: list[dict[str, object]]) -> None:
        self.manifest.write_text(json.dumps({
            "schema_version": 1,
            "front_region_schema": "pdf2md.front-regions.v1",
            "documents": documents,
        }), encoding="utf-8")

    def test_hash_mismatch_is_rejected(self) -> None:
        item = self._document("stale", inspect_digest="0" * 64)
        self._write_manifest([item])
        with self.assertRaisesRegex(MODULE.PlanError, "inspect source SHA-256 does not match"):
            MODULE.build_plan(self.manifest)

    def test_missing_inspect_is_rejected(self) -> None:
        self._write_manifest([self._document("missing", with_inspect=False)])
        with self.assertRaisesRegex(MODULE.PlanError, "missing inspect metadata"):
            MODULE.build_plan(self.manifest, require_all=True)

    def test_missing_local_and_inspect_are_reported_by_default(self) -> None:
        missing_pdf = self._document("missing-pdf")
        (self.data / missing_pdf["local_path"]).unlink()
        missing_inspect = self._document("missing-inspect", with_inspect=False)
        present = self._document("present", page_count=4)
        self._write_manifest([missing_pdf, missing_inspect, present])

        plan = MODULE.build_plan(self.manifest)

        self.assertEqual(plan["document_count"], 1)
        self.assertEqual(plan["skipped_count"], 2)
        self.assertEqual([item["id"] for item in plan["documents"]], ["present"])
        self.assertEqual(plan["skipped"], [
            {"id": "missing-inspect", "reason": "missing_inspect_metadata"},
            {"id": "missing-pdf", "reason": "missing_local_pdf"},
        ])

    def test_long_toc_document_is_capped_at_page_40(self) -> None:
        self._write_manifest([
            self._document("long", page_count=1400, toc_candidate_pages=[7, 36]),
        ])
        document = MODULE.build_plan(self.manifest)["documents"][0]
        self.assertEqual(document["selection"], [{"start": 1, "end": 40}])
        self.assertEqual(document["role"], "toc-positive")

    def test_scanned_document_uses_24_front_pages(self) -> None:
        self._write_manifest([self._document("scan", page_count=80, pdf_kind="scanned")])
        document = MODULE.build_plan(self.manifest)["documents"][0]
        self.assertEqual(document["selection"], [{"start": 1, "end": 24}])
        self.assertEqual(document["role"], "scanned-front")

    def test_text_without_toc_is_bounded_negative(self) -> None:
        self._write_manifest([
            self._document(
                "negative",
                page_count=90,
                expected_front_regions=["cover", "abstract", "body_start"],
            ),
        ])
        document = MODULE.build_plan(self.manifest)["documents"][0]
        self.assertEqual(document["selection"], [{"start": 1, "end": 12}])
        self.assertEqual(document["role"], "toc-negative")

    def test_expected_contents_without_candidate_is_hard_positive(self) -> None:
        self._write_manifest([
            self._document(
                "missed-contents",
                page_count=90,
                expected_front_regions=["cover", "contents", "body_start"],
            ),
        ])
        document = MODULE.build_plan(self.manifest)["documents"][0]
        self.assertEqual(document["selection"], [{"start": 1, "end": 12}])
        self.assertEqual(document["role"], "toc-expected-undetected")
        self.assertEqual(document["priority"], "high")
        self.assertIn("hard positive", document["reasons"][-1])

    def test_expected_list_region_without_candidate_is_hard_positive(self) -> None:
        self._write_manifest([
            self._document(
                "missed-list",
                page_count=80,
                pdf_kind="scanned",
                expected_front_regions=["list_of_figures", "list_of_tables"],
            ),
        ])
        document = MODULE.build_plan(self.manifest)["documents"][0]
        self.assertEqual(document["selection"], [{"start": 1, "end": 24}])
        self.assertEqual(document["role"], "toc-expected-undetected")
        self.assertNotEqual(document["role"], "scanned-front")

    def test_detected_candidate_remains_positive_without_manifest_expectation(self) -> None:
        self._write_manifest([
            self._document(
                "detected",
                page_count=30,
                toc_candidate_pages=[3],
                expected_front_regions=["cover", "body_start"],
            ),
        ])
        document = MODULE.build_plan(self.manifest)["documents"][0]
        self.assertEqual(document["role"], "toc-positive")

    def test_invalid_expected_front_regions_is_rejected(self) -> None:
        item = self._document("invalid-regions")
        item["expected_front_regions"] = "contents"
        self._write_manifest([item])
        with self.assertRaisesRegex(MODULE.PlanError, "expected_front_regions"):
            MODULE.build_plan(self.manifest)

    def test_plan_is_deterministic_and_sorted_by_id(self) -> None:
        self._write_manifest([
            self._document("z-last", page_count=5),
            self._document("a-first", page_count=7, toc_candidate_pages=[2]),
        ])
        first = MODULE.build_plan(self.manifest)
        second = MODULE.build_plan(self.manifest)
        self.assertEqual(first, second)
        self.assertEqual([item["id"] for item in first["documents"]], ["a-first", "z-last"])

    def test_check_validates_but_does_not_write(self) -> None:
        self._write_manifest([self._document("checked")])
        output = Path(self.temporary.name) / "must-not-exist.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = MODULE.run(["--check", "--output", str(output)], manifest_path=self.manifest)
        self.assertEqual(result, 0)
        self.assertFalse(output.exists())
        self.assertIn("no file written", stdout.getvalue())

    def test_unsafe_local_path_is_rejected(self) -> None:
        self._write_manifest([{
            "id": "escape",
            "suite": "core",
            "local_path": "../escape.pdf",
        }])
        with self.assertRaisesRegex(MODULE.PlanError, "unsafe local_path"):
            MODULE.build_plan(self.manifest)


if __name__ == "__main__":
    unittest.main()
