"""Conservative rule -> layout -> text cascade for document front regions."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Mapping

from pdf2md_front_regions import (
    REGION_KINDS, RULES_VERSION, classify_content_list_v2,
)
from pdf2md_region_evidence import (
    FEATURES_VERSION, PageEvidence, extract_region_evidence,
    hashed_text_features, layout_features,
)
from pdf2md_region_models import (
    LinearRegionModel, Prediction, artifact_fingerprint, load_model_artifact,
    resolve_artifact,
)


SCHEMA = "pdf2md.front-regions.v2"
CASCADE_VERSION = "front-region-cascade-2"
_NAV = {"contents", "list_of_figures", "list_of_tables"}
_DEFAULT_THRESHOLDS = {
    "layout": 0.86,
    "text": 0.82,
    "cover": 0.90,
    "legal": 0.88,
    "revision_history": 0.90,
    "preface": 0.88,
    "abstract": 0.90,
    "acknowledgements": 0.90,
    "contents": 0.92,
    "list_of_figures": 0.93,
    "list_of_tables": 0.93,
    "abbreviations": 0.90,
    "nomenclature": 0.90,
    "body_start": 0.93,
    "other_front": 0.94,
}
_DEFAULT_MARGINS = {"layout": 0.20, "text": 0.15}


def classifier_fingerprint(
    model_dir: str | Path | None = None,
    thresholds: Mapping[str, float] | None = None,
    margins: Mapping[str, float] | None = None,
) -> str:
    """Fingerprint code policy, thresholds, and artifact bytes for cache keys."""
    resolved_thresholds = _numeric_policy(_DEFAULT_THRESHOLDS, thresholds)
    resolved_margins = _numeric_policy(_DEFAULT_MARGINS, margins)
    layout_path = resolve_artifact(model_dir, "layout")
    text_path = resolve_artifact(model_dir, "text")
    value = {
        "version": CASCADE_VERSION,
        "rules_version": RULES_VERSION,
        "features_version": FEATURES_VERSION,
        "thresholds": resolved_thresholds,
        "margins": resolved_margins,
        "layout_artifact_sha256": artifact_fingerprint(layout_path),
        "text_artifact_sha256": artifact_fingerprint(text_path),
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def classify_front_regions_v2(
    source: Any,
    *,
    start_page: int = 1,
    max_pages: int = 64,
    model_dir: str | Path | None = None,
    layout_model: LinearRegionModel | Any | None = None,
    text_model: LinearRegionModel | Any | None = None,
    thresholds: Mapping[str, float] | None = None,
    margins: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Classify conservatively, abstaining whenever evidence is unsafe."""
    started = time.perf_counter()
    threshold_policy = _numeric_policy(_DEFAULT_THRESHOLDS, thresholds)
    margin_policy = _numeric_policy(_DEFAULT_MARGINS, margins)
    evidence_started = time.perf_counter()
    document = extract_region_evidence(source, start_page=start_page, max_pages=max_pages)
    evidence_ms = _elapsed_ms(evidence_started)
    rule_started = time.perf_counter()
    v1 = classify_content_list_v2(
        source,
        start_page=start_page,
        max_pages=max_pages,
        stop_at_body=False,
    )
    rule_ms = _elapsed_ms(rule_started)

    layout_path = resolve_artifact(model_dir, "layout")
    text_path = resolve_artifact(model_dir, "text")
    direct_layout_model = layout_model is not None
    direct_text_model = text_model is not None
    if layout_model is None:
        layout_model = load_model_artifact(layout_path, expected_kind="layout")
    if text_model is None:
        text_model = load_model_artifact(text_path, expected_kind="text")
    direct_fingerprints = {
        "layout": _model_fingerprint(layout_model),
        "text": _model_fingerprint(text_model),
    }
    base_fingerprint = classifier_fingerprint(model_dir, threshold_policy, margin_policy)
    if (
        (direct_layout_model and direct_fingerprints['layout'])
        or (direct_text_model and direct_fingerprints['text'])
    ):
        payload = json.dumps(
            {"base": base_fingerprint, "direct": direct_fingerprints},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        used_fingerprint = hashlib.sha256(payload).hexdigest()
    else:
        used_fingerprint = base_fingerprint

    v1_by_page = {item.get("page"): item for item in v1.get("pages", []) if isinstance(item, dict)}
    navigation_by_page = _navigation_by_page(v1)
    stage_counts = {
        "pages": 0, "rule_locked": 0, "layout_called": 0,
        "layout_accepted": 0, "text_called": 0, "text_accepted": 0,
        "accepted": 0, "abstained": 0,
    }
    timings = {"evidence_ms": evidence_ms, "rules_ms": rule_ms, "layout_ms": 0.0, "text_ms": 0.0}
    pages = []
    stopped_at_body = False
    for page in document.pages:
        if page.page not in v1_by_page:
            continue
        stage_counts["pages"] += 1
        rule = v1_by_page[page.page]
        page_result = _classify_page(
            page, rule, navigation_by_page.get(page.page, {}), layout_model,
            text_model, threshold_policy, margin_policy, stage_counts, timings,
        )
        if page_result["accepted"]:
            stage_counts["accepted"] += 1
        else:
            stage_counts["abstained"] += 1
        pages.append(page_result)
        if _accepted_body_boundary(page_result):
            stopped_at_body = True
            break

    total_ms = _elapsed_ms(started)
    timings["total_ms"] = total_ms
    warnings = list(dict.fromkeys(document.warnings + list(v1.get("warnings", []))))
    if layout_path is not None and layout_model is None:
        warnings.append("invalid_layout_model")
    if text_path is not None and text_model is None:
        warnings.append("invalid_text_model")
    limited_by_max_pages = bool(v1.get("limited_by_max_pages"))
    stop_reason = (
        "body_boundary"
        if stopped_at_body
        else "page_limit"
        if limited_by_max_pages
        else "end"
    )
    return {
        "schema": SCHEMA,
        "classifier": {
            "version": CASCADE_VERSION,
            "rules_version": RULES_VERSION,
            "features_version": FEATURES_VERSION,
            "fingerprint": used_fingerprint,
            "model_fingerprints": direct_fingerprints,
            "thresholds": threshold_policy,
            "margins": margin_policy,
        },
        "inputs": {
            "content_list_sha256": document.sha256,
            "start_page": start_page if isinstance(start_page, int) and not isinstance(start_page, bool) and start_page >= 1 else 1,
            "max_pages": max_pages if isinstance(max_pages, int) and not isinstance(max_pages, bool) and max_pages >= 0 else 64,
            "input_page_count": document.input_page_count,
            "selected_page_count": document.selected_page_count,
        },
        "processing": {
            "stage_counts": stage_counts,
            "timing_ms": {key: round(value, 3) for key, value in timings.items()},
        },
        "stop_reason": stop_reason,
        "limited_by_max_pages": limited_by_max_pages,
        "stopped_at_body": stopped_at_body,
        "pages": pages,
        "warnings": warnings,
    }


def project_front_regions_v1(report: Mapping[str, Any]) -> dict[str, Any]:
    """Project only accepted v2 decisions to the existing navigation schema."""
    if not isinstance(report, Mapping) or report.get("schema") != SCHEMA:
        raise ValueError("expected pdf2md.front-regions.v2 report")
    inputs = report.get("inputs", {}) if isinstance(report.get("inputs"), Mapping) else {}
    accepted = [
        page for page in report.get("pages", [])
        if isinstance(page, Mapping) and page.get("accepted") is True
        and page.get("kind") in REGION_KINDS
    ]
    pages: list[dict[str, Any]] = []
    navigation: dict[str, list[dict[str, Any]]] = {kind: [] for kind in sorted(_NAV)}
    for page in accepted:
        probability = page.get("calibrated_probability")
        strength = page.get("rule_strength")
        confidence = probability if _finite_probability(probability) else strength
        if not _finite_probability(confidence):
            confidence = 0.0
        evidence = page.get("evidence", {}) if isinstance(page.get("evidence"), Mapping) else {}
        rule_evidence = evidence.get("rule", [])
        item = {
            "page": int(page["page"]), "kind": page["kind"],
            "confidence": round(float(confidence), 4),
            "evidence": list(rule_evidence) if isinstance(rule_evidence, list) else [],
            "stats": dict(evidence.get("stats", {})) if isinstance(evidence.get("stats"), Mapping) else {},
        }
        pages.append(item)
        blocks_by_kind = evidence.get("navigation_blocks", {})
        if isinstance(blocks_by_kind, Mapping):
            valid_blocks = {
                navigation_kind: blocks
                for navigation_kind in sorted(_NAV)
                if (
                    blocks := _validated_navigation_blocks(
                        blocks_by_kind.get(navigation_kind)
                    )
                )
            }
            final_kind = page["kind"]
            if final_kind in _NAV:
                # A model can retype one legacy navigation block after the
                # rules labelled it as another navigation kind. Keep the old
                # compatibility behaviour: a lone old block follows the final
                # accepted kind. The cascade may also retain the same block
                # under both old and final keys; discard only that exact alias.
                # Distinct valid blocks remain independent multi-label output.
                if final_kind not in valid_blocks and len(valid_blocks) == 1:
                    valid_blocks = {
                        final_kind: next(iter(valid_blocks.values()))
                    }
                elif final_kind in valid_blocks:
                    final_blocks = valid_blocks[final_kind]
                    valid_blocks = {
                        navigation_kind: blocks
                        for navigation_kind, blocks in valid_blocks.items()
                        if navigation_kind == final_kind or blocks != final_blocks
                    }
            for navigation_kind, blocks in valid_blocks.items():
                navigation[navigation_kind].append(
                    {"page": item["page"], "blocks": blocks}
                )
    pages.sort(key=lambda item: item["page"])
    input_count = int(inputs.get("input_page_count", 0)) if isinstance(inputs.get("input_page_count", 0), int) else 0
    selected_count = int(inputs.get("selected_page_count", 0)) if isinstance(inputs.get("selected_page_count", 0), int) else 0
    start_page = int(inputs.get("start_page", 1)) if isinstance(inputs.get("start_page", 1), int) else 1
    stopped = bool(report.get("stopped_at_body")) and any(page["kind"] == "body_start" for page in pages)
    return {
        "schema": "pdf2md.front-regions.v1",
        "start_page": start_page,
        "input_page_count": input_count,
        "examined_page_count": selected_count,
        "reported_page_count": len(pages),
        "stop_reason": report.get("stop_reason", "end"),
        "limited_by_max_pages": bool(report.get("limited_by_max_pages")),
        "stopped_at_body": stopped,
        "total_pages": input_count,
        "page_count": len(pages),
        "scanned_pages": len(pages),
        "truncated": input_count > len(pages),
        "body_start_page": next((page["page"] for page in pages if page["kind"] == "body_start"), None),
        "pages": pages,
        "regions": _merge_regions(pages),
        "navigation": {kind: items for kind, items in navigation.items() if items},
        "warnings": list(report.get("warnings", [])) if isinstance(report.get("warnings"), list) else [],
    }


def _classify_page(
    page: PageEvidence,
    rule: Mapping[str, Any],
    navigation_blocks: Mapping[str, Any],
    layout_model: Any,
    text_model: Any,
    thresholds: Mapping[str, float],
    margins: Mapping[str, float],
    counts: dict[str, int],
    timings: dict[str, float],
) -> dict[str, Any]:
    rule_kind = rule.get("kind") if rule.get("kind") in REGION_KINDS else None
    rule_strength = rule.get("confidence")
    rule_strength = float(rule_strength) if _finite_probability(rule_strength) else None
    rule_evidence = rule.get("evidence", []) if isinstance(rule.get("evidence"), list) else []
    locked = _rule_locked(
        rule_kind, rule_strength, rule_evidence, navigation_blocks
    )
    evidence = {
        "rule": list(rule_evidence),
        "stats": dict(rule.get("stats", {})) if isinstance(rule.get("stats"), Mapping) else {},
        "navigation_blocks": dict(navigation_blocks),
        "navigation_candidates": [
            list(block) for block in page.navigation_candidate_blocks
        ],
        "warnings": list(page.warnings),
    }
    base = {
        "page": page.page,
        "kind": rule_kind if locked else None,
        "accepted": locked,
        "decision_source": "rule" if locked else "abstain",
        "calibrated_probability": None,
        "rule_strength": rule_strength,
        "top_candidates": ([{"kind": rule_kind, "strength": rule_strength, "source": "rule"}] if rule_kind else []),
        "evidence": evidence,
        "blocks": [block.compact() for block in page.blocks],
    }
    if locked:
        counts["rule_locked"] += 1
        _attach_navigation_candidates(
            evidence, rule_kind, page.navigation_candidate_blocks
        )
        return base

    layout_prediction = Prediction((), True, "model_unavailable")
    if layout_model is not None:
        if not page.valid_layout_blocks:
            layout_prediction = Prediction(
                (), True, "layout_evidence_unavailable"
            )
        else:
            counts["layout_called"] += 1
            then = time.perf_counter()
            layout_prediction = _safe_predict(
                layout_model, layout_features(page)
            )
            timings["layout_ms"] += _elapsed_ms(then)
            base["top_candidates"] = _candidate_dicts(
                layout_prediction, "layout"
            )
        evidence["layout"] = {"ood": layout_prediction.ood, "reason": layout_prediction.reason}
        if _accepted_prediction(layout_prediction, "layout", thresholds, margins):
            kind, probability = layout_prediction.candidates[0]
            if kind in REGION_KINDS:
                counts["layout_accepted"] += 1
                _attach_navigation_candidates(
                    evidence, kind, page.navigation_candidate_blocks
                )
                base.update({
                    "kind": kind, "accepted": True, "decision_source": "layout",
                    "calibrated_probability": round(probability, 6),
                })
                return base

    if text_model is None:
        evidence["abstain_reason"] = layout_prediction.reason or "no_accepted_model"
        return base
    counts["text_called"] += 1
    then = time.perf_counter()
    text_prediction = _safe_predict(text_model, hashed_text_features(page))
    timings["text_ms"] += _elapsed_ms(then)
    base["top_candidates"] += _candidate_dicts(text_prediction, "text")
    evidence["text"] = {"ood": text_prediction.ood, "reason": text_prediction.reason}
    if not _accepted_prediction(text_prediction, "text", thresholds, margins):
        evidence["abstain_reason"] = text_prediction.reason or "text_below_threshold"
        return base
    text_kind, text_probability = text_prediction.candidates[0]
    layout_top = layout_prediction.top
    if layout_top is not None and layout_top[0] != text_kind:
        evidence["abstain_reason"] = "layout_text_conflict"
        return base
    if text_kind not in REGION_KINDS:
        evidence["abstain_reason"] = "unknown_model_class"
        return base
    counts["text_accepted"] += 1
    _attach_navigation_candidates(
        evidence, text_kind, page.navigation_candidate_blocks
    )
    base.update({
        "kind": text_kind, "accepted": True, "decision_source": "text",
        "calibrated_probability": round(text_probability, 6),
    })
    return base


def _accepted_body_boundary(page: Mapping[str, Any]) -> bool:
    if (
        page.get("accepted") is not True
        or page.get("kind") != "body_start"
    ):
        return False
    evidence = page.get("evidence")
    rule_evidence = (
        evidence.get("rule", [])
        if isinstance(evidence, Mapping)
        else []
    )
    # Some datasheets place numbered feature sections before their real TOC.
    # The v1 bounded lookahead marks that first page explicitly; retain the
    # accepted page label but defer the stop until the post-navigation body.
    return not (
        isinstance(rule_evidence, list)
        and "front_navigation_lookahead" in rule_evidence
    )


def _attach_navigation_candidates(
    evidence: dict[str, Any],
    kind: Any,
    candidates: list[list[str]],
) -> None:
    if kind not in _NAV or not candidates:
        return
    mapping = evidence.get("navigation_blocks")
    if not isinstance(mapping, dict):
        mapping = {}
        evidence["navigation_blocks"] = mapping
    existing = mapping.get(kind)
    blocks = existing if isinstance(existing, list) else []
    clean_blocks = [
        list(block)
        for block in blocks
        if isinstance(block, list)
        and all(isinstance(value, str) for value in block)
    ]
    seen = {tuple(block) for block in clean_blocks}
    for block in candidates:
        if (
            not isinstance(block, list)
            or not block
            or not all(isinstance(value, str) and value for value in block)
        ):
            continue
        key = tuple(block)
        if key in seen:
            continue
        seen.add(key)
        clean_blocks.append(list(block))
    if clean_blocks:
        mapping[kind] = clean_blocks


def _rule_locked(
    kind: str | None,
    strength: float | None,
    evidence: list[Any],
    navigation_blocks: Mapping[str, Any],
) -> bool:
    if kind is None or strength is None:
        return False
    if (
        kind in _NAV
        and strength >= 0.69
        and 'navigation_continuation' in evidence
        and isinstance(navigation_blocks.get(kind), list)
        and bool(navigation_blocks[kind])
    ):
        return True
    strong_evidence = {
        "explicit_title", "explicit_heading", "structured_index_navigation",
        "page_header_navigation",
    }
    if kind == "body_start" and strength >= 0.90:
        strong_evidence.update({
            "body_heading", "split_body_heading",
            "post_navigation_body_boundary", "post_navigation_body_heading",
            "short_chinese_manual_body",
        })
    return strength >= 0.90 and bool(strong_evidence.intersection(item for item in evidence if isinstance(item, str)))


def _safe_predict(model: Any, features: Mapping[str, float]) -> Prediction:
    try:
        result = model.predict(features)
    except Exception:
        return Prediction((), True, "model_error")
    if not isinstance(result, Prediction):
        return Prediction((), True, "invalid_model_result")
    clean = []
    for item in result.candidates:
        if (
            not isinstance(item, (tuple, list)) or len(item) != 2
            or not isinstance(item[0], str) or item[0] not in REGION_KINDS
            or not _finite_probability(item[1])
        ):
            return Prediction((), True, "invalid_model_result")
        clean.append((item[0], float(item[1])))
    clean.sort(key=lambda item: (-item[1], item[0]))
    if clean and sum(probability for _, probability in clean) > 1.00001:
        return Prediction((), True, "invalid_probabilities")
    return Prediction(tuple(clean), bool(result.ood), result.reason)


def _accepted_prediction(prediction: Prediction, stage: str, thresholds: Mapping[str, float], margins: Mapping[str, float]) -> bool:
    if prediction.ood or not prediction.candidates:
        return False
    kind, probability = prediction.candidates[0]
    runner_up = prediction.candidates[1][1] if len(prediction.candidates) > 1 else 0.0
    required = max(float(thresholds.get(stage, 1.0)), float(thresholds.get(kind, 1.0)))
    required_margin = max(
        float(margins.get(stage, 1.0)), float(margins.get(kind, 0.0))
    )
    return probability >= required and probability - runner_up >= required_margin


def _candidate_dicts(prediction: Prediction, source: str) -> list[dict[str, Any]]:
    return [
        {"kind": kind, "probability": round(probability, 6), "source": source}
        for kind, probability in prediction.candidates[:5]
    ]


def _navigation_by_page(v1: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    navigation = v1.get("navigation", {})
    if not isinstance(navigation, Mapping):
        return result
    for kind, entries in navigation.items():
        if kind not in _NAV or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
                continue
            blocks = _validated_navigation_blocks(entry.get("blocks"))
            if blocks:
                result.setdefault(entry["page"], {})[kind] = blocks
    return result


def _validated_navigation_blocks(value: Any) -> list[list[str]]:
    """Copy a complete, non-empty list-of-non-empty-string-lists or reject it."""
    if not isinstance(value, list) or not value:
        return []
    result: list[list[str]] = []
    for block in value:
        if (
            not isinstance(block, list)
            or not block
            or not all(isinstance(item, str) and bool(item.strip()) for item in block)
        ):
            return []
        result.append(list(block))
    return result


def _merge_regions(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    for page in pages:
        if regions and regions[-1]["kind"] == page["kind"] and regions[-1]["end_page"] + 1 == page["page"]:
            region = regions[-1]
            count = region.pop("_count")
            region["end_page"] = page["page"]
            region["confidence"] = round((region["confidence"] * count + page["confidence"]) / (count + 1), 4)
            region["_count"] = count + 1
        else:
            regions.append({
                "kind": page["kind"], "start_page": page["page"],
                "end_page": page["page"], "confidence": page["confidence"], "_count": 1,
            })
    for region in regions:
        region.pop("_count", None)
    return regions


def _numeric_policy(defaults: Mapping[str, float], override: Mapping[str, float] | None) -> dict[str, float]:
    result = dict(defaults)
    if isinstance(override, Mapping):
        for key, value in override.items():
            if isinstance(key, str) and _finite_probability(value):
                result[key] = float(value)
    return dict(sorted(result.items()))


def _model_fingerprint(model: Any) -> str | None:
    value = getattr(model, "fingerprint", None)
    return value if isinstance(value, str) and value else None


def _finite_probability(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


__all__ = [
    "CASCADE_VERSION", "SCHEMA", "classifier_fingerprint",
    "classify_front_regions_v2", "project_front_regions_v1",
]
