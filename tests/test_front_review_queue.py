from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-front-review-queue.py"
SPEC = importlib.util.spec_from_file_location("front_review_queue", SCRIPT)
assert SPEC and SPEC.loader
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

SHA_A, SHA_B, FINGERPRINT = "a" * 64, "b" * 64, "f" * 64


def candidate(kind: str, probability: float,
              source: str = "text") -> dict:
    return {
        "kind": kind, "probability": probability, "source": source,
    }


def page(number: int, *, accepted: bool, source: str,
         candidates: list[dict], kind: str | None = None,
         evidence: dict | None = None) -> dict:
    return {
        "page": number, "kind": kind, "accepted": accepted,
        "decision_source": source,
        "calibrated_probability": (
            candidates[0]["probability"] if accepted and candidates else None
        ),
        "rule_strength": None, "top_candidates": candidates,
        "evidence": evidence or {},
        "blocks": [{
            "text_preview": "SECRET PDF TEXT", "image": "private.png",
        }],
    }


def report(pages: list[dict], source_sha: str = SHA_A) -> dict:
    return {
        "schema": "pdf2md.front-regions.v2",
        "classifier": {
            "version": "front-region-cascade-2",
            "rules_version": "r1", "features_version": "f1",
            "fingerprint": FINGERPRINT,
            "model_fingerprints": {"layout": None, "text": None},
            "thresholds": {}, "margins": {},
        },
        "inputs": {
            "content_list_sha256": source_sha, "start_page": 1,
            "max_pages": 64, "input_page_count": len(pages),
            "selected_page_count": len(pages),
        },
        "processing": {"stage_counts": {}, "timing_ms": {}},
        "stop_reason": "end", "limited_by_max_pages": False,
        "stopped_at_body": False, "pages": pages, "warnings": [],
    }


class FrontReviewQueueTests(unittest.TestCase):
    def write_report(self, root: Path, name: str, value: dict) -> Path:
        path = (
            root / f"{name}.pdf2md" / "raw" / "cache"
            / "front-regions" / "report.json"
        )
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_ranks_deduplicates_and_redacts_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.write_report(root, "alpha", report([
                page(
                    1, accepted=True, source="text", kind="abstract",
                    candidates=[
                        candidate("abstract", .91),
                        candidate("cover", .09),
                    ],
                ),
                page(
                    2, accepted=False, source="abstain", candidates=[
                        candidate("contents", .51),
                        candidate("list_of_tables", .49),
                    ],
                    evidence={
                        "abstain_reason": "layout_text_conflict",
                        "layout": {"ood": False, "reason": None},
                        "text": {
                            "ood": True,
                            "reason": "out_of_distribution",
                        },
                        "navigation_blocks": {"contents": ["PRIVATE"]},
                    },
                ),
            ]))
            duplicate = self.write_report(root, "duplicate", report([
                page(
                    2, accepted=False, source="abstain", candidates=[
                        candidate("contents", .8),
                        candidate("abstract", .2),
                    ],
                    evidence={"abstain_reason": "text_below_threshold"},
                ),
            ]))
            queue = tool.build_queue(
                [duplicate, first, first], threshold=.2, limit=10
            )
            self.assertEqual(queue["count"], 1)
            item = queue["items"][0]
            self.assertEqual(item["document_id"], "alpha")
            self.assertEqual(item["content_list_sha256"], SHA_A)
            self.assertIsNone(item["source_sha256"])
            self.assertEqual(
                item["report_path"],
                "alpha.pdf2md/raw/cache/front-regions/report.json",
            )
            self.assertEqual(item["margin"], .02)
            self.assertEqual(item["evidence"]["ood"], ["text"])
            self.assertEqual(
                item["evidence"]["conflict"], "layout_text_conflict"
            )
            serialized = json.dumps(queue)
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("SECRET PDF TEXT", serialized)
            self.assertNotIn("PRIVATE", serialized)
            self.assertNotIn("navigation_blocks", serialized)

    def test_real_source_hash_is_not_confused_with_content_list_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = report([
                page(1, accepted=False, source="abstain", candidates=[]),
            ], SHA_A)
            value["inputs"]["source_sha256"] = SHA_B
            path = self.write_report(Path(temporary), "source-bound", value)

            item = tool.build_queue([path])["items"][0]

            self.assertEqual(item["content_list_sha256"], SHA_A)
            self.assertEqual(item["source_sha256"], SHA_B)

    def test_margin_entropy_selection_and_confident_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_report(Path(temporary), "beta", report([
                page(
                    1, accepted=True, source="text", kind="cover",
                    candidates=[
                        candidate("cover", .9), candidate("legal", .1),
                    ],
                ),
                page(
                    2, accepted=True, source="text", kind="contents",
                    candidates=[
                        candidate("contents", .55),
                        candidate("list_of_figures", .45),
                    ],
                ),
            ], SHA_B))
            queue = tool.build_queue([path], threshold=.15, limit=10)
            self.assertEqual([item["page"] for item in queue["items"]], [2])
            self.assertGreater(queue["items"][0]["evidence"]["entropy"], .9)

    def test_confident_navigation_debris_and_residue_enter_metadata_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            debris = "SECRET TITLE " + "." * 260
            current = page(
                7, accepted=True, source="text", kind="contents",
                candidates=[
                    candidate("contents", .99), candidate("abstract", .01),
                ],
                evidence={
                    "navigation_blocks": {"contents": [[debris]]},
                    "navigation_candidates": [[
                        "PRIVATE RESIDUAL A 10", "PRIVATE RESIDUAL B 20",
                        "PRIVATE RESIDUAL C 30",
                    ]],
                    "stats": {"index_items": 8},
                },
            )
            path = self.write_report(root, "structural", report([current]))
            queue = tool.build_queue([path], threshold=.05, limit=10)
            self.assertEqual(queue["count"], 1)
            evidence = queue["items"][0]["evidence"]
            self.assertEqual(
                evidence["structure_anomalies"],
                [
                    "navigation_candidate_residue",
                    "navigation_leader_debris",
                    "navigation_long_entry",
                ],
            )
            self.assertEqual(
                evidence["structure_metrics"]["leader_debris_entry_count"], 1
            )
            serialized = json.dumps(queue)
            self.assertNotIn("SECRET TITLE", serialized)
            self.assertNotIn("PRIVATE RESIDUAL", serialized)

    def test_duplicate_and_separated_navigation_runs_are_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entries = [f"PRIVATE CHAPTER {number} {number}" for number in range(1, 6)]
            first = page(
                3, accepted=True, source="text", kind="contents",
                candidates=[candidate("contents", .99)],
                evidence={"navigation_blocks": {"contents": [entries]}},
            )
            separator = page(
                4, accepted=True, source="text", kind="abstract",
                candidates=[candidate("abstract", .99)],
            )
            repeated = page(
                5, accepted=True, source="text", kind="contents",
                candidates=[candidate("contents", .99)],
                evidence={"navigation_blocks": {"contents": [entries]}},
            )
            path = self.write_report(
                root, "repeated", report([first, separator, repeated])
            )
            queue = tool.build_queue([path], threshold=.05, limit=10)
            self.assertEqual([item["page"] for item in queue["items"]], [3, 5])
            for item in queue["items"]:
                self.assertEqual(
                    item["evidence"]["structure_anomalies"],
                    ["duplicate_navigation_run", "separated_navigation_runs"],
                )
            self.assertNotIn("PRIVATE CHAPTER", json.dumps(queue))

    def test_deterministic_atomic_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.write_report(root, "gamma", report([
                page(
                    3, accepted=False, source="abstain",
                    candidates=[
                        candidate("preface", .5),
                        candidate("abstract", .5),
                    ],
                ),
            ]))
            first = tool.build_queue([path], threshold=.2, limit=1)
            self.assertEqual(first, tool.build_queue([path], limit=1))
            output = root / "queue.json"
            with mock.patch.object(
                tool.os, "replace", wraps=tool.os.replace
            ) as replace:
                tool._atomic_write(output, first)
            replace.assert_called_once()
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")), first
            )
            self.assertEqual(list(root.glob("queue.json.*.tmp")), [])

    def test_strict_schema_duplicate_keys_size_and_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = self.write_report(root, "good", report([]))
            value = json.loads(good.read_text(encoding="utf-8"))
            value["extra"] = True
            bad = self.write_report(root, "bad", value)
            with self.assertRaises(tool.QueueError):
                tool.build_queue([bad])
            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"schema":"x","schema":"y"}', encoding="utf-8"
            )
            with self.assertRaisesRegex(tool.QueueError, "duplicate JSON key"):
                tool.build_queue([duplicate])
            oversized = root / "oversized.json"
            oversized.write_text("{}", encoding="utf-8")
            with mock.patch.object(tool, "MAX_REPORT_BYTES", 1):
                with self.assertRaisesRegex(tool.QueueError, "report size"):
                    tool.build_queue([oversized])
            with self.assertRaisesRegex(tool.QueueError, "overwrite"):
                tool._output_path(good, [good])

    def test_check_validates_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.write_report(root, "delta", report([
                page(
                    1, accepted=False, source="abstain", candidates=[]
                ),
            ]))
            output = root / "must-not-exist.json"
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT), str(path),
                    "--output", str(output), "--check",
                ],
                capture_output=True, text=True, encoding="utf-8",
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("valid: 1 review items", completed.stdout)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
