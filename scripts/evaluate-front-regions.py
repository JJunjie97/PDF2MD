#!/usr/bin/env python3
"""Offline gold-label evaluation for the PDF2MD front-region cascade.

Only human-verified physical pages already present in provenance-checked
content-list-v2 caches are evaluated. Only source-file hashing is performed:
the command never parses, renders or writes a PDF, starts OCR, or serializes
page text/blocks into its report.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, NamedTuple, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pdf2md_core as core  # noqa: E402
from pdf2md_front_regions import REGION_KINDS, RULES_VERSION  # noqa: E402
from pdf2md_region_cascade import (  # noqa: E402
    CASCADE_VERSION, classify_front_regions_v2, project_front_regions_v1,
)


TRAINER_SPEC = importlib.util.spec_from_file_location(
    "pdf2md_front_region_trainer_for_evaluation",
    ROOT / "scripts" / "train-front-region-model.py",
)
if TRAINER_SPEC is None or TRAINER_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("cannot load front-region training safety helpers")
trainer = importlib.util.module_from_spec(TRAINER_SPEC)
TRAINER_SPEC.loader.exec_module(trainer)

TRAINING_DATA_SPEC = importlib.util.spec_from_file_location(
    "pdf2md_front_training_data_for_evaluation",
    ROOT / "scripts" / "manage-front-training.py",
)
if TRAINING_DATA_SPEC is None or TRAINING_DATA_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("cannot load front-region annotation helpers")
training_data = importlib.util.module_from_spec(TRAINING_DATA_SPEC)
TRAINING_DATA_SPEC.loader.exec_module(training_data)


SCHEMA = "pdf2md.front-region-evaluation.v1"
DEFAULT_CORPUS = ROOT / "data" / "corpus.json"
DEFAULT_ANNOTATIONS = ROOT / "data" / "training" / "annotations.jsonl"
DEFAULT_NAVIGATION_ANNOTATIONS = (
    ROOT / "data" / "training" / "navigation-annotations.jsonl"
)
DEFAULT_MODEL_DIR = ROOT / "models" / "front-region" / "v1"
ABSTAIN = "__abstain__"
PRESENT = "present"
ABSENT = "absent"
NAVIGATION_EVALUATION_SCHEMA = "pdf2md.front-navigation-evaluation.v1"
NAVIGATION_KINDS = tuple(sorted(training_data.NAVIGATION_KINDS))
NAVIGATION_PRESENCES = (ABSENT, PRESENT)
NAVIGATION_GATE_REQUIREMENTS = {
    "minimum_present_per_kind": 20,
    "minimum_absent_per_kind": 20,
    "minimum_present_documents_per_kind": 5,
    "minimum_absent_documents_per_kind": 5,
}


class EvaluationError(RuntimeError):
    pass


class _ContextSelection(NamedTuple):
    """One provenance-bound cache selection used only inside evaluation."""

    status: str
    reason: str | None
    raw_window: list[Any] | None
    metadata: dict[str, Any] | None
    annotated_page_sha256: dict[int, str]


class _NavigationRun(NamedTuple):
    report: dict[str, Any]
    samples: list[dict[str, Any]]
    classifier: dict[str, Any] | None
    content_list_sha256: set[str]
    manifest_sha256: set[str]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise EvaluationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_hashed_json(path: Path, label: str) -> tuple[Any, str]:
    """Parse and hash the exact same bytes, avoiding a read/hash race."""
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read {label} {path}: {exc}") from exc
    return value, hashlib.sha256(payload).hexdigest()


def _load_context_selection(
    pdf: Path, annotated_pages: Sequence[int], expected_sha: str,
) -> _ContextSelection:
    """Choose one continuous cached selection without joining cache files.

    A multi-page selection with safe predecessor pages is preferred, then the
    smallest covering range and manifest order. Only the prefix ending at the
    last annotated page is returned to the classifier. A sole one-page cache
    remains traceable but is explicitly an isolated fallback, not context.
    """
    pages = sorted(set(annotated_pages))
    if not pages or any(
        not isinstance(page, int) or isinstance(page, bool) or page < 1
        for page in pages
    ):
        raise EvaluationError(f"{pdf}: invalid annotated physical pages")
    actual_sha = _sha256(pdf)
    if actual_sha != expected_sha:
        raise EvaluationError(f"{pdf}: local PDF SHA-256 mismatch")

    output = pdf.with_suffix(".pdf2md")
    manifest_path = output / "raw" / "manifest.json"
    manifest, manifest_sha = _read_hashed_json(manifest_path, "manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("selections"), list):
        raise EvaluationError(f"{manifest_path}: incompatible manifest")
    source = manifest.get("source")
    manifest_source_sha = source.get("sha256") if isinstance(source, dict) else None
    try:
        trusted_source_sha = trainer._strict_sha256(  # type: ignore[attr-defined]
            manifest_source_sha, f"{manifest_path} source",
        )
    except trainer.TrainingError as exc:
        raise EvaluationError(str(exc)) from exc
    if trusted_source_sha != expected_sha:
        raise EvaluationError(f"{manifest_path}: source SHA-256 mismatch")

    candidates: list[dict[str, Any]] = []
    for manifest_index, item in enumerate(manifest["selections"]):
        if not isinstance(item, dict) or "content_list_v2" not in item:
            continue
        try:
            content_path = trainer._safe_child(  # type: ignore[attr-defined]
                output, item["content_list_v2"],
            )
        except trainer.TrainingError as exc:
            raise EvaluationError(str(exc)) from exc
        value, content_sha = _read_hashed_json(content_path, "content-list-v2")
        if not isinstance(value, list) or not value:
            raise EvaluationError(
                f"{content_path}: content-list-v2 must be a non-empty page list"
            )
        try:
            start, end = trainer._selection_pages(  # type: ignore[attr-defined]
                item.get("pages"), len(value),
            )
        except trainer.TrainingError as exc:
            raise EvaluationError(str(exc)) from exc
        if all(start <= page <= end for page in pages):
            conversion = {
                key: item[key]
                for key in ("profile", "method", "requested_method", "language")
                if key in item
                and isinstance(item[key], (str, int, float, bool, type(None)))
            }
            candidates.append({
                "manifest_index": manifest_index,
                "raw_pages": value,
                "pages": str(item.get("pages")),
                "start": start,
                "end": end,
                "span": end - start + 1,
                "content_list_sha256": content_sha,
                "manifest_sha256": manifest_sha,
                "conversion": conversion,
            })

    if not candidates:
        return _ContextSelection(
            status="unavailable",
            reason="no_single_contiguous_selection_covers_all_annotated_pages",
            raw_window=None,
            metadata=None,
            annotated_page_sha256={},
        )

    multi_page = [item for item in candidates if item["span"] > 1]
    if multi_page:
        earliest = pages[0]
        predecessor = [item for item in multi_page if item["start"] < earliest]
        pool = predecessor or multi_page
        chosen = min(pool, key=lambda item: (item["span"], item["manifest_index"]))
        status = "scored"
        reason = None
    else:
        chosen = min(candidates, key=lambda item: item["manifest_index"])
        status = "isolated_fallback"
        reason = "only_single_page_cached_selection"

    window_end = pages[-1]
    window_count = window_end - chosen["start"] + 1
    raw_window = chosen["raw_pages"][:window_count]
    if len(raw_window) != window_count:
        raise EvaluationError(f"{pdf}: context window/page mapping mismatch")
    page_hashes: dict[int, str] = {}
    for physical_page in pages:
        offset = physical_page - chosen["start"]
        if offset < 0 or offset >= len(chosen["raw_pages"]):
            raise EvaluationError(f"{pdf}: annotated physical page mapping mismatch")
        page_hashes[physical_page] = _json_sha256(chosen["raw_pages"][offset])
    metadata = {
        "pages": chosen["pages"],
        "selection_start_page": chosen["start"],
        "selection_end_page": chosen["end"],
        "selection_page_count": chosen["span"],
        "context_start_page": chosen["start"],
        "context_end_page": window_end,
        "context_page_count": window_count,
        "manifest_index": chosen["manifest_index"],
        "content_list_sha256": chosen["content_list_sha256"],
        "manifest_sha256": chosen["manifest_sha256"],
        "conversion": chosen["conversion"],
    }
    return _ContextSelection(
        status=status,
        reason=reason,
        raw_window=raw_window if status == "scored" else None,
        metadata=metadata,
        annotated_page_sha256=page_hashes,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _class_metrics(
    gold: Sequence[str], predicted: Sequence[str],
) -> dict[str, dict[str, int | float | None]]:
    rows: dict[str, dict[str, int | float | None]] = {}
    for kind in REGION_KINDS:
        support = sum(item == kind for item in gold)
        predicted_count = sum(item == kind for item in predicted)
        true_positive = sum(
            expected == kind and observed == kind
            for expected, observed in zip(gold, predicted)
        )
        precision = _ratio(true_positive, predicted_count)
        recall = _ratio(true_positive, support)
        f1 = (
            round(2.0 * precision * recall / (precision + recall), 6)
            if precision is not None and recall is not None and precision + recall
            else None
        )
        rows[kind] = {
            "support": support,
            "predicted": predicted_count,
            "true_positive": true_positive,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    return rows


def _confusion(
    gold: Sequence[str], predicted: Sequence[str],
) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for expected, observed in zip(gold, predicted):
        counts[expected][observed] += 1
    return {
        expected: dict(sorted(counts[expected].items()))
        for expected in REGION_KINDS
        if counts[expected]
    }


def _summary(gold: Sequence[str], predicted: Sequence[str]) -> dict[str, Any]:
    total = len(gold)
    accepted = sum(item != ABSTAIN for item in predicted)
    correct = sum(
        observed != ABSTAIN and observed == expected
        for expected, observed in zip(gold, predicted)
    )
    return {
        "total_samples": total,
        "accepted": accepted,
        "abstained": total - accepted,
        "correct": correct,
        "coverage": _ratio(accepted, total),
        "accepted_accuracy": _ratio(correct, accepted),
        # Abstentions deliberately remain incorrect in this denominator.
        "overall_accuracy": _ratio(correct, total),
    }


def _navigation_prediction_state(
    report: Mapping[str, Any],
) -> dict[int, dict[str, Any]]:
    """Project exactly the navigation that production can publish.

    Raw navigation candidates are deliberately ignored. A primary page
    rejection is a navigation abstention rather than an implicit negative.
    """
    try:
        projected = project_front_regions_v1(report)
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"cannot project navigation decisions: {exc}") from exc
    detected: set[tuple[int, str]] = set()
    navigation = projected.get("navigation", {})
    if isinstance(navigation, Mapping):
        for kind, entries in navigation.items():
            if kind not in NAVIGATION_KINDS or not isinstance(entries, list):
                continue
            for entry in entries:
                if (
                    isinstance(entry, Mapping)
                    and isinstance(entry.get("page"), int)
                    and not isinstance(entry.get("page"), bool)
                ):
                    detected.add((int(entry["page"]), str(kind)))

    states: dict[int, dict[str, Any]] = {}
    for page_result in report.get("pages", []):
        if (
            not isinstance(page_result, Mapping)
            or not isinstance(page_result.get("page"), int)
            or isinstance(page_result.get("page"), bool)
        ):
            continue
        page = int(page_result["page"])
        accepted = page_result.get("accepted") is True
        evidence = (
            page_result.get("evidence")
            if isinstance(page_result.get("evidence"), Mapping)
            else {}
        )
        states[page] = {
            "predictions": {
                kind: (
                    PRESENT
                    if (page, kind) in detected
                    else ABSENT
                    if accepted
                    else ABSTAIN
                )
                for kind in NAVIGATION_KINDS
            },
            "primary_accepted": accepted,
            "decision_source": page_result.get("decision_source", "abstain"),
            "abstain_reason": (
                evidence.get("abstain_reason")
                if not accepted and isinstance(evidence.get("abstain_reason"), str)
                else None
            ),
        }
    return states


def _navigation_binary_metrics(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    present_support = sum(item["gold"] == PRESENT for item in samples)
    absent_support = sum(item["gold"] == ABSENT for item in samples)
    predicted_present = sum(item["predicted"] == PRESENT for item in samples)
    predicted_absent = sum(item["predicted"] == ABSENT for item in samples)
    abstained = sum(item["predicted"] == ABSTAIN for item in samples)
    true_positive = sum(
        item["gold"] == PRESENT and item["predicted"] == PRESENT
        for item in samples
    )
    false_negative = sum(
        item["gold"] == PRESENT and item["predicted"] == ABSENT
        for item in samples
    )
    positive_abstain = sum(
        item["gold"] == PRESENT and item["predicted"] == ABSTAIN
        for item in samples
    )
    true_negative = sum(
        item["gold"] == ABSENT and item["predicted"] == ABSENT
        for item in samples
    )
    false_positive = sum(
        item["gold"] == ABSENT and item["predicted"] == PRESENT
        for item in samples
    )
    negative_abstain = sum(
        item["gold"] == ABSENT and item["predicted"] == ABSTAIN
        for item in samples
    )
    accepted = predicted_present + predicted_absent
    correct = true_positive + true_negative
    precision = _ratio(true_positive, true_positive + false_positive)
    positive_recall = _ratio(true_positive, present_support)
    negative_recall = _ratio(true_negative, absent_support)
    f1 = (
        round(
            2.0 * precision * positive_recall
            / (precision + positive_recall),
            6,
        )
        if (
            precision is not None
            and positive_recall is not None
            and precision + positive_recall
        )
        else 0.0
        if present_support and precision == 0.0 and positive_recall == 0.0
        else None
    )
    class_complete = present_support > 0 and absent_support > 0
    present_documents = len({
        str(item["document_id"]) for item in samples
        if item["gold"] == PRESENT
    })
    absent_documents = len({
        str(item["document_id"]) for item in samples
        if item["gold"] == ABSENT
    })
    return {
        "total_samples": len(samples),
        "support_present": present_support,
        "support_absent": absent_support,
        "support_present_documents": present_documents,
        "support_absent_documents": absent_documents,
        "predicted_present": predicted_present,
        "predicted_absent": predicted_absent,
        "accepted": accepted,
        "abstained": abstained,
        "correct": correct,
        "true_positive": true_positive,
        "true_negative": true_negative,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "positive_abstain": positive_abstain,
        "negative_abstain": negative_abstain,
        "coverage": _ratio(accepted, len(samples)),
        "accepted_accuracy": _ratio(correct, accepted),
        "overall_accuracy": _ratio(correct, len(samples)),
        "precision": precision,
        # Abstentions remain misses in both recall denominators.
        "positive_recall": positive_recall,
        "negative_recall": negative_recall,
        "false_positive_rate": _ratio(false_positive, absent_support),
        "false_negative_rate": _ratio(
            false_negative + positive_abstain, present_support,
        ),
        "f1": f1,
        "class_complete": class_complete,
        "balanced_accuracy": (
            round((positive_recall + negative_recall) / 2.0, 6)
            if (
                class_complete
                and positive_recall is not None
                and negative_recall is not None
            )
            else None
        ),
    }


def _navigation_confusion(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, dict[str, int]]]:
    result: dict[str, dict[str, dict[str, int]]] = {}
    for kind in NAVIGATION_KINDS:
        rows = [item for item in samples if item["kind"] == kind]
        result[kind] = {
            gold: {
                predicted: sum(
                    item["gold"] == gold and item["predicted"] == predicted
                    for item in rows
                )
                for predicted in (ABSENT, PRESENT, ABSTAIN)
            }
            for gold in NAVIGATION_PRESENCES
        }
    return result


def _navigation_exact_set(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    grouped: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[(str(sample["document_id"]), int(sample["page"]))].append(sample)
    complete = [
        rows for rows in grouped.values()
        if {str(item["kind"]) for item in rows} == set(NAVIGATION_KINDS)
    ]
    non_abstained = sum(
        all(item["predicted"] != ABSTAIN for item in rows) for rows in complete
    )
    exact = sum(
        all(item["predicted"] != ABSTAIN for item in rows)
        and {
            str(item["kind"]) for item in rows if item["gold"] == PRESENT
        }
        == {
            str(item["kind"]) for item in rows if item["predicted"] == PRESENT
        }
        for rows in complete
    )
    return {
        "fully_annotated_pages": len(complete),
        "non_abstained_pages": non_abstained,
        "exact": exact,
        # Abstained pages are deliberately not exact.
        "exact_accuracy": _ratio(exact, len(complete)),
    }


def _navigation_metric_bundle(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    per_kind = {
        kind: _navigation_binary_metrics(
            [item for item in samples if item["kind"] == kind]
        )
        for kind in NAVIGATION_KINDS
    }
    summary = _navigation_binary_metrics(samples)
    class_support_complete = bool(samples) and all(
        value["class_complete"] is True for value in per_kind.values()
    )
    release_gate_eligible = class_support_complete and all(
        value["support_present"]
        >= NAVIGATION_GATE_REQUIREMENTS["minimum_present_per_kind"]
        and value["support_absent"]
        >= NAVIGATION_GATE_REQUIREMENTS["minimum_absent_per_kind"]
        and value["support_present_documents"]
        >= NAVIGATION_GATE_REQUIREMENTS[
            "minimum_present_documents_per_kind"
        ]
        and value["support_absent_documents"]
        >= NAVIGATION_GATE_REQUIREMENTS[
            "minimum_absent_documents_per_kind"
        ]
        for value in per_kind.values()
    )
    for value in per_kind.values():
        value["release_gate_eligible"] = (
            value["class_complete"] is True
            and value["support_present"]
            >= NAVIGATION_GATE_REQUIREMENTS["minimum_present_per_kind"]
            and value["support_absent"]
            >= NAVIGATION_GATE_REQUIREMENTS["minimum_absent_per_kind"]
            and value["support_present_documents"]
            >= NAVIGATION_GATE_REQUIREMENTS[
                "minimum_present_documents_per_kind"
            ]
            and value["support_absent_documents"]
            >= NAVIGATION_GATE_REQUIREMENTS[
                "minimum_absent_documents_per_kind"
            ]
        )
    summary["class_support_complete"] = class_support_complete
    summary["release_gate_eligible"] = release_gate_eligible
    # A micro average across disjoint one-sided classes is not a valid
    # balanced accuracy. Keep it unavailable until every kind has both sides.
    if not class_support_complete:
        summary["balanced_accuracy"] = None
    macro: dict[str, Any] = {}
    for metric in (
        "precision", "positive_recall", "negative_recall", "f1",
        "balanced_accuracy",
    ):
        eligible = [
            kind for kind, value in per_kind.items()
            if value[metric] is not None
        ]
        macro[metric] = {
            "value": (
                round(
                    sum(float(per_kind[kind][metric]) for kind in eligible)
                    / len(eligible),
                    6,
                )
                if eligible else None
            ),
            "eligible_kinds": eligible,
        }
    return {
        "summary": summary,
        "per_kind": per_kind,
        "macro": macro,
        "confusion": _navigation_confusion(samples),
        "exact_set": _navigation_exact_set(samples),
    }


def _navigation_annotation_summary(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    verified = [item for item in records if item.get("status") == "verified"]
    pages = {
        (str(item["document_id"]), int(item["page"])) for item in verified
    }
    complete_pages = sum(
        {
            str(item["kind"])
            for item in verified
            if (
                str(item["document_id"]), int(item["page"])
            ) == page
        }
        == set(NAVIGATION_KINDS)
        for page in pages
    )
    return {
        "verified_labels": len(verified),
        "verified_pages": len(pages),
        "verified_documents": len({
            str(item["document_id"]) for item in verified
        }),
        "present": sum(item["presence"] == PRESENT for item in verified),
        "absent": sum(item["presence"] == ABSENT for item in verified),
        "fully_annotated_pages": complete_pages,
    }


def _body_start_metrics(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_document: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_document[str(sample["document_id"])].append(sample)
    gold_pages: dict[str, int] = {}
    predicted_pages: dict[str, int] = {}
    for document_id, rows in by_document.items():
        gold = sorted(int(row["page"]) for row in rows if row["gold"] == "body_start")
        predicted = sorted(
            int(row["page"])
            for row in rows
            if row["accepted"] is True and row["predicted"] == "body_start"
        )
        if gold:
            gold_pages[document_id] = gold[0]
        if predicted:
            predicted_pages[document_id] = predicted[0]
    comparable = sorted(set(gold_pages).intersection(predicted_pages))
    errors = [abs(predicted_pages[item] - gold_pages[item]) for item in comparable]
    exact = sum(predicted_pages[item] == gold_pages[item] for item in comparable)
    return {
        "gold_documents": len(gold_pages),
        "predicted_documents": len(predicted_pages),
        "comparable_documents": len(comparable),
        "missing_predictions": len(set(gold_pages) - set(predicted_pages)),
        "exact": exact,
        # Missing/abstained predictions are not exact.
        "exact_accuracy": _ratio(exact, len(gold_pages)),
        "mae": round(sum(errors) / len(errors), 6) if errors else None,
    }


def _approved_models(model_dir: Path) -> tuple[bool, dict[str, Any]]:
    policy = core._read_json(model_dir / "policy.json") or {}
    approved = core._front_region_models_approved(model_dir, policy)
    return approved, policy if approved else {}


def _navigation_report(
    records: Sequence[Mapping[str, Any]],
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    bundle = _navigation_metric_bundle(samples)
    grouped: dict[str, Any] = {}
    for document_id in sorted({
        str(item["document_id"]) for item in samples
    }):
        rows = [
            item for item in samples if item["document_id"] == document_id
        ]
        document_bundle = _navigation_metric_bundle(rows)
        grouped[document_id] = {
            "source_sha256": rows[0]["source_sha256"],
            **document_bundle,
            "samples": [
                {
                    key: row[key]
                    for key in (
                        "page", "kind", "gold", "predicted", "accepted",
                        "decision_source", "abstain_reason",
                    )
                }
                for row in rows
            ],
        }
    verified = [item for item in records if item.get("status") == "verified"]
    return {
        "schema": NAVIGATION_EVALUATION_SCHEMA,
        "status": "evaluated" if verified else "not_evaluated",
        "policy": {
            "labels": "human-verified-explicit-presence-only",
            "page_numbering": "physical-1-based",
            "unlabelled_page_kind_pairs": "unknown-not-negative",
            "prediction": "production-v1-projection-after-primary-acceptance",
            "raw_navigation_candidates": "ignored",
            "abstain_is_correct": False,
        },
        "gate_requirements": dict(NAVIGATION_GATE_REQUIREMENTS),
        "annotation_summary": _navigation_annotation_summary(records),
        **bundle,
        "documents": grouped,
    }


def _isolated_navigation_evaluation(
    records: Sequence[Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
    *,
    model_dir: Path | None,
    thresholds: Mapping[str, float] | None,
    margins: Mapping[str, float] | None,
    primary_classifier: Mapping[str, Any] | None,
) -> _NavigationRun:
    verified = sorted(
        (item for item in records if item.get("status") == "verified"),
        key=lambda item: (
            str(item["document_id"]), int(item["page"]), str(item["kind"]),
        ),
    )
    samples: list[dict[str, Any]] = []
    classifier: dict[str, Any] | None = None
    content_hashes: set[str] = set()
    manifest_hashes: set[str] = set()
    page_runs: dict[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]] = {}
    for record in verified:
        document_id = str(record["document_id"])
        physical_page = int(record["page"])
        cache_key = (document_id, physical_page)
        if cache_key not in page_runs:
            document = documents[document_id]
            expected_sha = document["sha256"]
            assert isinstance(expected_sha, str)
            try:
                raw_page, _evidence, cache_input = trainer.load_cached_page(
                    document["pdf"], physical_page, expected_sha,
                )
            except trainer.TrainingError as exc:
                raise EvaluationError(str(exc)) from exc
            report = classify_front_regions_v2(
                [raw_page],
                start_page=physical_page,
                max_pages=1,
                model_dir=model_dir,
                thresholds=thresholds,
                margins=margins,
            )
            observed_classifier = dict(report.get("classifier", {}))
            if classifier is None:
                classifier = observed_classifier
            elif observed_classifier.get("fingerprint") != classifier.get("fingerprint"):
                raise EvaluationError(
                    "navigation classifier fingerprint changed during evaluation"
                )
            if (
                primary_classifier is not None
                and observed_classifier.get("fingerprint")
                != primary_classifier.get("fingerprint")
            ):
                raise EvaluationError(
                    "primary/navigation classifier fingerprint mismatch"
                )
            content_hashes.add(str(cache_input["content_list_sha256"]))
            manifest_hashes.add(str(cache_input["manifest_sha256"]))
            page_runs[cache_key] = (
                _navigation_prediction_state(report), dict(cache_input)
            )

        states, cache_input = page_runs[cache_key]
        state = states.get(physical_page)
        if state is None:
            predicted = ABSTAIN
            decision_source = "abstain"
            abstain_reason = "page_not_returned_by_isolated_cascade"
        else:
            predicted = str(state["predictions"][str(record["kind"])])
            decision_source = str(state["decision_source"])
            abstain_reason = state["abstain_reason"]
        samples.append({
            "document_id": document_id,
            "source_sha256": str(record["source_sha256"]),
            "page": physical_page,
            "kind": str(record["kind"]),
            "gold": str(record["presence"]),
            "predicted": predicted,
            "accepted": predicted != ABSTAIN,
            "decision_source": decision_source,
            "abstain_reason": abstain_reason,
            "content_list_sha256": cache_input["content_list_sha256"],
            "manifest_sha256": cache_input["manifest_sha256"],
        })
    return _NavigationRun(
        report=_navigation_report(records, samples),
        samples=samples,
        classifier=classifier,
        content_list_sha256=content_hashes,
        manifest_sha256=manifest_hashes,
    )


def _context_navigation_evaluation(
    records: Sequence[Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
    isolated_samples: Sequence[Mapping[str, Any]],
    *,
    model_dir: Path | None,
    thresholds: Mapping[str, float] | None,
    margins: Mapping[str, float] | None,
    isolated_classifier: Mapping[str, Any] | None,
    primary_classifier: Mapping[str, Any] | None,
) -> dict[str, Any]:
    verified = [item for item in records if item.get("status") == "verified"]
    by_document: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in verified:
        by_document[str(record["document_id"])].append(record)
    isolated_manifest_sha = {
        document_id: {
            str(sample["manifest_sha256"])
            for sample in isolated_samples
            if sample["document_id"] == document_id
        }
        for document_id in by_document
    }

    scored_samples: list[dict[str, Any]] = []
    grouped: dict[str, Any] = {}
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    selected_content_sha: set[str] = set()
    selected_manifest_sha: set[str] = set()
    context_classifier: dict[str, Any] | None = None

    for document_id in sorted(by_document):
        rows = sorted(
            by_document[document_id],
            key=lambda item: (int(item["page"]), str(item["kind"])),
        )
        annotated_pages = sorted({int(item["page"]) for item in rows})
        document = documents[document_id]
        expected_sha = document["sha256"]
        assert isinstance(expected_sha, str)
        selection = _load_context_selection(
            Path(document["pdf"]), annotated_pages, expected_sha,
        )
        status_counts[selection.status] += 1
        if selection.reason:
            reason_counts[selection.reason] += 1
        document_report: dict[str, Any] = {
            "source_sha256": expected_sha,
            "status": selection.status,
            "reason": selection.reason,
            "annotated_pages": annotated_pages,
            "selection": selection.metadata,
        }
        if selection.metadata is not None:
            manifest_sha = str(selection.metadata["manifest_sha256"])
            if isolated_manifest_sha[document_id] != {manifest_sha}:
                raise EvaluationError(
                    f"{document_id}: manifest changed during navigation "
                    "isolated/context evaluation"
                )
            selected_manifest_sha.add(manifest_sha)
            selected_content_sha.add(
                str(selection.metadata["content_list_sha256"])
            )

        if selection.status != "scored":
            document_report["samples"] = [
                {
                    "page": int(item["page"]),
                    "kind": str(item["kind"]),
                    "gold": str(item["presence"]),
                    "scored": False,
                    "fallback": "isolated",
                    "reason": selection.reason,
                    **({
                        "selection_offset": (
                            int(item["page"])
                            - int(selection.metadata["selection_start_page"])
                        ),
                        "page_content_sha256": selection.annotated_page_sha256[
                            int(item["page"])
                        ],
                    } if selection.metadata is not None else {}),
                }
                for item in rows
            ]
            grouped[document_id] = document_report
            continue

        assert selection.raw_window is not None
        assert selection.metadata is not None
        report = classify_front_regions_v2(
            selection.raw_window,
            start_page=int(selection.metadata["context_start_page"]),
            max_pages=int(selection.metadata["context_page_count"]),
            model_dir=model_dir,
            thresholds=thresholds,
            margins=margins,
        )
        observed_classifier = dict(report.get("classifier", {}))
        if context_classifier is None:
            context_classifier = observed_classifier
        elif observed_classifier.get("fingerprint") != context_classifier.get("fingerprint"):
            raise EvaluationError(
                "navigation context classifier fingerprint changed during evaluation"
            )
        for expected_classifier in (isolated_classifier, primary_classifier):
            if (
                expected_classifier is not None
                and observed_classifier.get("fingerprint")
                != expected_classifier.get("fingerprint")
            ):
                raise EvaluationError(
                    "primary/navigation context classifier fingerprint mismatch"
                )
        states = _navigation_prediction_state(report)
        document_samples: list[dict[str, Any]] = []
        for item in rows:
            physical_page = int(item["page"])
            state = states.get(physical_page)
            if state is None:
                predicted = ABSTAIN
                decision_source = "abstain"
                abstain_reason = (
                    "not_examined_after_body_boundary"
                    if report.get("stopped_at_body") is True
                    else "page_not_returned_by_context_cascade"
                )
            else:
                predicted = str(state["predictions"][str(item["kind"])])
                decision_source = str(state["decision_source"])
                abstain_reason = state["abstain_reason"]
            sample = {
                "document_id": document_id,
                "source_sha256": expected_sha,
                "page": physical_page,
                "kind": str(item["kind"]),
                "gold": str(item["presence"]),
                "predicted": predicted,
                "accepted": predicted != ABSTAIN,
                "decision_source": decision_source,
                "abstain_reason": abstain_reason,
                "scored": True,
                "selection_offset": (
                    physical_page
                    - int(selection.metadata["selection_start_page"])
                ),
                "page_content_sha256": selection.annotated_page_sha256[
                    physical_page
                ],
            }
            scored_samples.append(sample)
            document_samples.append(sample)
        document_bundle = _navigation_metric_bundle(document_samples)
        document_report.update({
            **document_bundle,
            "samples": [
                {
                    key: item[key]
                    for key in (
                        "page", "kind", "gold", "predicted", "accepted",
                        "decision_source", "abstain_reason", "scored",
                        "selection_offset", "page_content_sha256",
                    )
                }
                for item in document_samples
            ],
        })
        grouped[document_id] = document_report

    result = _navigation_report(records, scored_samples)
    verified_pages = {
        (str(item["document_id"]), int(item["page"])) for item in verified
    }
    scored_pages = {
        (str(item["document_id"]), int(item["page"]))
        for item in scored_samples
    }
    result.update({
        "policy": {
            **result["policy"],
            "context_selection": "one-contiguous-cache-per-document",
            "unlabelled_pages": "classified-in-memory-not-scored-or-serialized",
            "single_page_cache": "isolated-fallback-not-context-scored",
        },
        "comparability": {
            "verified_documents": len(by_document),
            "scored_documents": status_counts["scored"],
            "unscored_documents": len(by_document) - status_counts["scored"],
            "isolated_fallback_documents": status_counts["isolated_fallback"],
            "unavailable_documents": status_counts["unavailable"],
            "verified_samples": len(verified),
            "scored_samples": len(scored_samples),
            "unscored_samples": len(verified) - len(scored_samples),
            "verified_pages": len(verified_pages),
            "scored_pages": len(scored_pages),
            "coverage": _ratio(len(scored_samples), len(verified)),
            "reasons": dict(sorted(reason_counts.items())),
        },
        "provenance": {
            "content_list_sha256": sorted(selected_content_sha),
            "manifest_sha256": sorted(selected_manifest_sha),
        },
        "documents": grouped,
    })
    return result


def _context_evaluation(
    examples: Sequence[Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
    isolated_samples: Sequence[Mapping[str, Any]],
    *,
    model_dir: Path | None,
    thresholds: Mapping[str, float] | None,
    margins: Mapping[str, float] | None,
    isolated_classifier: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Score verified pages after one production-like replay per document."""
    examples_by_document: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for example in examples:
        examples_by_document[str(example["document_id"])].append(example)
    isolated_manifest_sha = {
        document_id: {
            str(sample["manifest_sha256"])
            for sample in isolated_samples
            if sample["document_id"] == document_id
        }
        for document_id in examples_by_document
    }

    scored_samples: list[dict[str, Any]] = []
    grouped: dict[str, Any] = {}
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    selected_content_sha: set[str] = set()
    selected_manifest_sha: set[str] = set()
    context_classifier: dict[str, Any] | None = None

    for document_id in sorted(examples_by_document):
        rows = sorted(examples_by_document[document_id], key=lambda item: int(item["page"]))
        annotated_pages = [int(item["page"]) for item in rows]
        document = documents[document_id]
        expected_sha = document["sha256"]
        assert isinstance(expected_sha, str)
        selection = _load_context_selection(
            Path(document["pdf"]), annotated_pages, expected_sha,
        )
        status_counts[selection.status] += 1
        if selection.reason:
            reason_counts[selection.reason] += 1

        document_report: dict[str, Any] = {
            "source_sha256": expected_sha,
            "status": selection.status,
            "reason": selection.reason,
            "annotated_pages": annotated_pages,
            "selection": selection.metadata,
        }
        if selection.metadata is not None:
            manifest_sha = str(selection.metadata["manifest_sha256"])
            if isolated_manifest_sha[document_id] != {manifest_sha}:
                raise EvaluationError(
                    f"{document_id}: manifest changed during isolated/context evaluation"
                )
            selected_manifest_sha.add(manifest_sha)
            selected_content_sha.add(str(selection.metadata["content_list_sha256"]))

        if selection.status != "scored":
            document_report["samples"] = [
                {
                    "page": int(item["page"]),
                    "gold": str(item["kind"]),
                    "scored": False,
                    "fallback": "isolated",
                    "reason": selection.reason,
                    **({
                        "selection_offset": (
                            int(item["page"])
                            - int(selection.metadata["selection_start_page"])
                        ),
                        "page_content_sha256": selection.annotated_page_sha256[
                            int(item["page"])
                        ],
                    } if selection.metadata is not None else {}),
                }
                for item in rows
            ]
            grouped[document_id] = document_report
            continue

        assert selection.raw_window is not None
        assert selection.metadata is not None
        report = classify_front_regions_v2(
            selection.raw_window,
            start_page=int(selection.metadata["context_start_page"]),
            max_pages=int(selection.metadata["context_page_count"]),
            model_dir=model_dir,
            thresholds=thresholds,
            margins=margins,
        )
        observed_classifier = dict(report.get("classifier", {}))
        if context_classifier is None:
            context_classifier = observed_classifier
        elif observed_classifier.get("fingerprint") != context_classifier.get("fingerprint"):
            raise EvaluationError("context classifier fingerprint changed during evaluation")
        if (
            isolated_classifier is not None
            and observed_classifier.get("fingerprint")
            != isolated_classifier.get("fingerprint")
        ):
            raise EvaluationError("isolated/context classifier fingerprint mismatch")

        results_by_page = {
            int(item["page"]): item
            for item in report.get("pages", [])
            if isinstance(item, Mapping)
            and isinstance(item.get("page"), int)
            and not isinstance(item.get("page"), bool)
        }
        document_samples: list[dict[str, Any]] = []
        for item in rows:
            physical_page = int(item["page"])
            page_result = results_by_page.get(physical_page)
            accepted = bool(page_result and page_result.get("accepted") is True)
            predicted = (
                str(page_result["kind"])
                if accepted and page_result.get("kind") in REGION_KINDS
                else ABSTAIN
            )
            evidence = (
                page_result.get("evidence")
                if isinstance(page_result, Mapping) else None
            )
            abstain_reason = (
                evidence.get("abstain_reason")
                if isinstance(evidence, Mapping)
                and isinstance(evidence.get("abstain_reason"), str)
                else (
                    "not_examined_after_body_boundary"
                    if page_result is None and report.get("stopped_at_body") is True
                    else "page_not_returned_by_context_cascade"
                    if page_result is None
                    else None
                )
            )
            sample = {
                "document_id": document_id,
                "page": physical_page,
                "gold": str(item["kind"]),
                "predicted": predicted,
                "accepted": accepted,
                "decision_source": (
                    page_result.get("decision_source")
                    if isinstance(page_result, Mapping) else "abstain"
                ),
                "abstain_reason": abstain_reason,
                "scored": True,
                "selection_offset": (
                    physical_page
                    - int(selection.metadata["selection_start_page"])
                ),
                "page_content_sha256": selection.annotated_page_sha256[
                    physical_page
                ],
            }
            scored_samples.append(sample)
            document_samples.append({
                key: sample[key]
                for key in (
                    "page", "gold", "predicted", "accepted",
                    "decision_source", "abstain_reason", "scored",
                    "selection_offset", "page_content_sha256",
                )
            })
        doc_gold = [str(item["gold"]) for item in document_samples]
        doc_predicted = [str(item["predicted"]) for item in document_samples]
        document_report.update({
            **_summary(doc_gold, doc_predicted),
            "samples": document_samples,
        })
        grouped[document_id] = document_report

    gold = [str(item["gold"]) for item in scored_samples]
    predicted = [str(item["predicted"]) for item in scored_samples]
    verified_samples = len(examples)
    scored_documents = status_counts["scored"]
    verified_documents = len(examples_by_document)
    return {
        "policy": {
            "mode": "production-like-context",
            "labels": "human-verified-only",
            "scoring_scope": "verified-pages-only",
            "unlabelled_pages": "classified-in-memory-not-scored-or-serialized",
            "classification_calls": "once-per-scored-document",
            "selection_resolution": (
                "one-covering-selection; prefer-safe-predecessor; "
                "then-smallest-range; then-manifest-order"
            ),
            "context_window": "selection-start-through-last-verified-page",
            "disjoint_selections": "never-joined",
            "single_page_selection": "isolated-fallback-not-context-scored",
            "page_numbering": "physical-1-based",
            "ocr": "disabled",
            "abstain_is_correct": False,
        },
        "inputs": {
            "source_sha256": {
                document_id: str(documents[document_id]["sha256"])
                for document_id in sorted(examples_by_document)
            },
            "manifest_sha256": sorted(selected_manifest_sha),
            "content_list_sha256": sorted(selected_content_sha),
        },
        "classifier_fingerprint": (
            context_classifier.get("fingerprint")
            if context_classifier is not None
            else (
                isolated_classifier.get("fingerprint")
                if isolated_classifier is not None else None
            )
        ),
        "comparability": {
            "verified_documents": verified_documents,
            "scored_documents": scored_documents,
            "unscored_documents": verified_documents - scored_documents,
            "isolated_fallback_documents": status_counts["isolated_fallback"],
            "unavailable_documents": status_counts["unavailable"],
            "verified_samples": verified_samples,
            "scored_samples": len(scored_samples),
            "unscored_samples": verified_samples - len(scored_samples),
            "coverage": _ratio(len(scored_samples), verified_samples),
            "reasons": dict(sorted(reason_counts.items())),
        },
        "summary": _summary(gold, predicted),
        "per_class": _class_metrics(gold, predicted),
        "confusion": _confusion(gold, predicted),
        "body_start": _body_start_metrics(scored_samples),
        "documents": grouped,
    }


def evaluate(
    corpus_path: Path = DEFAULT_CORPUS,
    annotations_path: Path = DEFAULT_ANNOTATIONS,
    *,
    navigation_annotations_path: Path | None = None,
    model_dir: Path = DEFAULT_MODEL_DIR,
) -> dict[str, Any]:
    """Evaluate verified annotations without extracting source PDF content."""
    corpus_path = corpus_path.resolve()
    annotations_path = annotations_path.resolve()
    if navigation_annotations_path is None:
        sibling = annotations_path.with_name(DEFAULT_NAVIGATION_ANNOTATIONS.name)
        resolved_navigation_annotations = sibling.resolve() if sibling.is_file() else None
    else:
        resolved_navigation_annotations = navigation_annotations_path.resolve()
    try:
        # This is the canonical loader: it validates annotation shape/reviewer,
        # corpus bindings, local PDF hashes, manifest source hashes, cache paths,
        # selection ranges and content-list hashes before returning any example.
        examples, provenance = trainer.load_examples(
            annotations_path, corpus_path, allow_regression_only=True,
        )
        documents, _input_hashes = trainer.load_documents(
            corpus_path, annotations_path,
        )
    except trainer.TrainingError as exc:
        raise EvaluationError(str(exc)) from exc
    if resolved_navigation_annotations is None:
        navigation_records: list[dict[str, Any]] = []
    else:
        try:
            navigation_records = training_data.load_navigation_annotations(
                resolved_navigation_annotations, corpus_path=corpus_path,
            )
        except training_data.TrainingDataError as exc:
            raise EvaluationError(str(exc)) from exc

    approved, policy = _approved_models(model_dir)
    active_model_dir = model_dir if approved else None
    thresholds = policy.get("thresholds") if approved and isinstance(policy.get("thresholds"), dict) else None
    margins = policy.get("margins") if approved and isinstance(policy.get("margins"), dict) else None

    samples: list[dict[str, Any]] = []
    classifier: dict[str, Any] | None = None
    for example in sorted(examples, key=lambda item: (item["document_id"], item["page"])):
        document_id = str(example["document_id"])
        physical_page = int(example["page"])
        document = documents[document_id]
        expected_sha = document["sha256"]
        assert isinstance(expected_sha, str)
        try:
            raw_page, _evidence, cache_input = trainer.load_cached_page(
                document["pdf"], physical_page, expected_sha,
            )
        except trainer.TrainingError as exc:
            raise EvaluationError(str(exc)) from exc
        report = classify_front_regions_v2(
            [raw_page],
            start_page=physical_page,
            max_pages=1,
            model_dir=active_model_dir,
            thresholds=thresholds,
            margins=margins,
        )
        if classifier is None:
            classifier = dict(report.get("classifier", {}))
        page_result = next(
            (
                item for item in report.get("pages", [])
                if isinstance(item, Mapping) and item.get("page") == physical_page
            ),
            None,
        )
        accepted = bool(page_result and page_result.get("accepted") is True)
        predicted = (
            str(page_result["kind"])
            if accepted and page_result.get("kind") in REGION_KINDS
            else ABSTAIN
        )
        evidence = page_result.get("evidence") if isinstance(page_result, Mapping) else None
        abstain_reason = (
            evidence.get("abstain_reason")
            if isinstance(evidence, Mapping) and isinstance(evidence.get("abstain_reason"), str)
            else None
        )
        samples.append({
            "document_id": document_id,
            "source_sha256": expected_sha,
            "page": physical_page,
            "gold": str(example["kind"]),
            "predicted": predicted,
            "accepted": accepted,
            "decision_source": (
                page_result.get("decision_source")
                if isinstance(page_result, Mapping) else "abstain"
            ),
            "abstain_reason": abstain_reason,
            "content_list_sha256": cache_input["content_list_sha256"],
            "manifest_sha256": cache_input["manifest_sha256"],
        })

    gold = [str(item["gold"]) for item in samples]
    predicted = [str(item["predicted"]) for item in samples]
    grouped: dict[str, Any] = {}
    for document_id in sorted({str(item["document_id"]) for item in samples}):
        rows = [item for item in samples if item["document_id"] == document_id]
        doc_gold = [str(item["gold"]) for item in rows]
        doc_predicted = [str(item["predicted"]) for item in rows]
        grouped[document_id] = {
            **_summary(doc_gold, doc_predicted),
            "source_sha256": rows[0]["source_sha256"],
            "samples": [
                {
                    key: row[key]
                    for key in (
                        "page", "gold", "predicted", "accepted",
                        "decision_source", "abstain_reason",
                    )
                }
                for row in rows
            ],
        }

    inputs = provenance.get("inputs", {}) if isinstance(provenance, Mapping) else {}
    selection = inputs.get("selection_config", {}) if isinstance(inputs, Mapping) else {}
    safe_selection = {
        key: selection.get(key)
        for key in (
            "schema", "page_numbering", "accepted_ranges", "overlap_resolution",
        )
    }
    production_context = _context_evaluation(
        examples,
        documents,
        samples,
        model_dir=active_model_dir,
        thresholds=thresholds,
        margins=margins,
        isolated_classifier=classifier,
    )
    navigation_run = _isolated_navigation_evaluation(
        navigation_records,
        documents,
        model_dir=active_model_dir,
        thresholds=thresholds,
        margins=margins,
        primary_classifier=classifier,
    )
    production_context["navigation_presence"] = _context_navigation_evaluation(
        navigation_records,
        documents,
        navigation_run.samples,
        model_dir=active_model_dir,
        thresholds=thresholds,
        margins=margins,
        isolated_classifier=navigation_run.classifier,
        primary_classifier=classifier,
    )
    return {
        "schema": SCHEMA,
        "evaluation_policy": {
            "labels": "human-verified-only",
            "page_numbering": "physical-1-based",
            "scope": "annotated-pages-only",
            "primary_page_kind": "mutually-exclusive",
            "navigation_presence": "independent-explicit-binary-labels",
            "unlabelled_navigation": "unknown-not-negative",
            "ocr": "disabled",
            "abstain_is_correct": False,
        },
        "inputs": {
            "corpus_sha256": _sha256(corpus_path),
            "annotations_sha256": _sha256(annotations_path),
            "source_sha256": inputs.get("source_sha256", {}),
            "content_list_sha256": inputs.get("content_list_sha256", []),
            "selection": safe_selection,
            "navigation": {
                "annotations_sha256": (
                    _sha256(resolved_navigation_annotations)
                    if resolved_navigation_annotations is not None else None
                ),
                "content_list_sha256": sorted(
                    navigation_run.content_list_sha256
                ),
                "manifest_sha256": sorted(navigation_run.manifest_sha256),
            },
        },
        "classifier": {
            "mode": "rules+approved-models" if approved else "rules-only",
            "models_approved": approved,
            "unresolved_pages": "abstain",
            "version": CASCADE_VERSION,
            "rules_version": RULES_VERSION,
            "fingerprint": classifier.get("fingerprint") if classifier else None,
            "model_fingerprints": classifier.get("model_fingerprints", {}) if classifier else {},
        },
        "summary": _summary(gold, predicted),
        "per_class": _class_metrics(gold, predicted),
        "confusion": _confusion(gold, predicted),
        "body_start": _body_start_metrics(samples),
        "documents": grouped,
        "navigation_presence": navigation_run.report,
        "production_context": production_context,
    }


def _serialize(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate verified front-region labels from existing PDF2MD caches",
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument(
        "--navigation-annotations",
        type=Path,
        help=(
            "explicit navigation-presence labels; defaults to "
            "navigation-annotations.jsonl beside --annotations when present"
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check", action="store_true",
        help="compare the deterministic report with --output without writing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check and args.output is None:
        print("evaluation error: --check requires --output", file=sys.stderr)
        return 2
    try:
        report = evaluate(
            args.corpus,
            args.annotations,
            navigation_annotations_path=args.navigation_annotations,
        )
        payload = _serialize(report)
        if args.check:
            try:
                current = args.output.read_text(encoding="utf-8")
            except OSError as exc:
                raise EvaluationError(f"cannot read check target {args.output}: {exc}") from exc
            if current != payload:
                print(f"evaluation check failed: {args.output} is stale", file=sys.stderr)
                return 1
        elif args.output is not None:
            _atomic_write(args.output, payload)
        else:
            sys.stdout.write(payload)
    except EvaluationError as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
