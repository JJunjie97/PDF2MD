#!/usr/bin/env python3
"""Train the two dependency-light linear heads used by the front-page cascade.

The command deliberately consumes only reviewed page labels and PDF2MD's local
content-list-v2 caches.  It never renders a PDF or invokes OCR while training.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from pathlib import PurePosixPath
import re
import sys
from typing import Any, Callable, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pdf2md_region_evidence import (  # noqa: E402
    FEATURES_VERSION, PageEvidence, extract_region_evidence, hashed_text_features, layout_features,
)
from pdf2md_front_regions import REGION_KINDS  # noqa: E402
from pdf2md_region_models import ARTIFACT_SCHEMA, load_model_artifact, save_json_artifact  # noqa: E402


ANNOTATION_SCHEMA = "pdf2md.front-page-label.v1"
LEGACY_SYNTHETIC_SCHEMA = "pdf2md.synthetic-front-corpus.v1"
SYNTHETIC_SCHEMA = "pdf2md.synthetic-front-corpus.v2"
SYNTHETIC_SCHEMAS = frozenset({LEGACY_SYNTHETIC_SCHEMA, SYNTHETIC_SCHEMA})
DEFAULT_CORPUS = ROOT / "data" / "corpus.json"
DEFAULT_ANNOTATIONS = ROOT / "data" / "training" / "annotations.jsonl"
DEFAULT_NAVIGATION_ANNOTATIONS = ROOT / "data" / "training" / "navigation-annotations.jsonl"
# Training writes a candidate, never the live cascade directory. Promotion to
# ``v1`` is an explicit, human-reviewed release action outside this command.
DEFAULT_OUTPUT = ROOT / "models" / "front-region" / "candidate"
DEFAULT_THRESHOLD = 0.98
DEFAULT_MARGIN = 0.15
NAVIGATION_ANNOTATION_SCHEMA = "pdf2md.front-navigation-label.v1"
NAVIGATION_AUXILIARY_SCHEMA = "pdf2md.navigation-aux-linear.v1"
NAVIGATION_KINDS = ("contents", "list_of_figures", "list_of_tables")
SELECTION_CONFIG = {
    "schema": "pdf2md.training-cache-selection.v1",
    "page_numbering": "physical-1-based",
    "accepted_ranges": "all-or-contiguous",
    "overlap_resolution": "smallest-covering-range-then-manifest-order",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class TrainingError(RuntimeError):
    pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TrainingError(f"cannot read JSON {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise TrainingError(f"cannot hash source PDF {path}: {exc}") from exc
    return digest.hexdigest()


def _strict_sha256(value: Any, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise TrainingError(f"{label}: SHA-256 must be 64 lowercase hexadecimal characters")
    return value


def _safe_relative_child(root: Path, relative: Any, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise TrainingError(f"{label}: missing relative path")
    normalized = relative.replace("\\", "/")
    parts = normalized.split("/")
    pure = PurePosixPath(normalized)
    if (
        pure.is_absolute() or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(char) < 32 for char in normalized)
    ):
        raise TrainingError(f"{label}: path must be a clean relative path")
    base = root.resolve()
    candidate = base.joinpath(*parts).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise TrainingError(f"{label}: resolved path escapes its corpus directory") from exc
    return candidate


def _validate_synthetic_provenance(
    manifest_path: Path, value: Mapping[str, Any], annotations_path: Path | None,
    *, expected_corpus_path: Path | None = None,
    navigation_annotations_path: Path | None = None,
) -> dict[str, str]:
    schema = value.get("schema")
    if schema not in SYNTHETIC_SCHEMAS or value.get("annotation_schema") != ANNOTATION_SCHEMA:
        raise TrainingError(f"{manifest_path}: invalid synthetic provenance schema")
    provenance = value.get("provenance")
    if not isinstance(provenance, dict) or {
        "source": provenance.get("source"),
        "contains_third_party_content": provenance.get("contains_third_party_content"),
        "license": provenance.get("license"),
    } != {"source": "project-generated", "contains_third_party_content": False, "license": "CC0-1.0"}:
        raise TrainingError(f"{manifest_path}: synthetic provenance is not project-generated CC0-1.0")
    annotation_ref = value.get("annotations")
    corpus_ref = value.get("training_corpus")
    if not isinstance(annotation_ref, dict) or not isinstance(corpus_ref, dict):
        raise TrainingError(f"{manifest_path}: synthetic provenance lacks bound corpus/annotations")
    annotation_sha = _strict_sha256(annotation_ref.get("sha256"), "synthetic annotations")
    corpus_sha = _strict_sha256(corpus_ref.get("sha256"), "synthetic training corpus")
    if corpus_ref.get("license") != "CC0-1.0":
        raise TrainingError(f"{manifest_path}: synthetic training corpus must be CC0-1.0")
    bound_annotations = _safe_relative_child(manifest_path.parent, annotation_ref.get("path"), "synthetic annotations")
    bound_corpus = _safe_relative_child(manifest_path.parent, corpus_ref.get("path"), "synthetic training corpus")
    if _sha256(bound_annotations) != annotation_sha or _sha256(bound_corpus) != corpus_sha:
        raise TrainingError(f"{manifest_path}: synthetic bound-file SHA-256 mismatch")
    if annotations_path is None or _sha256(annotations_path.resolve()) != annotation_sha:
        raise TrainingError(f"{manifest_path}: selected annotations are not bound by synthetic provenance")
    if expected_corpus_path is not None and bound_corpus != expected_corpus_path.resolve():
        raise TrainingError(f"{manifest_path}: provenance is bound to a different training corpus")
    corpus_value = _read_json(bound_corpus)
    if not isinstance(corpus_value, dict) or corpus_value.get("front_region_schema") != "pdf2md.front-regions.v1":
        raise TrainingError(f"{bound_corpus}: invalid bound training corpus schema")
    bound_documents = {
        item.get("id"): item for item in corpus_value.get("documents", []) if isinstance(item, dict)
    }
    manifest_documents = value.get("documents")
    if not isinstance(manifest_documents, list) or len(bound_documents) != len(manifest_documents):
        raise TrainingError(f"{manifest_path}: synthetic document inventory mismatch")
    for item in manifest_documents:
        if not isinstance(item, dict) or not isinstance(item.get("document_id"), str):
            raise TrainingError(f"{manifest_path}: malformed synthetic document")
        document_id = item["document_id"]
        expected = _strict_sha256(item.get("pdf_sha256"), f"{document_id} synthetic PDF")
        pdf = _safe_relative_child(manifest_path.parent, item.get("pdf_path"), f"{document_id} synthetic PDF")
        bound = bound_documents.get(document_id)
        if (
            not isinstance(bound, dict)
            or bound.get("local_path") != item.get("pdf_path")
            or bound.get("expected_sha256") != expected
            or bound.get("training_eligible") is not True
            or bound.get("redistributable") is not True
            or bound.get("license_class") != "cc0-1.0"
            or _sha256(pdf) != expected
        ):
            raise TrainingError(f"{document_id}: synthetic provenance/corpus/PDF mismatch")
    result = {
        "provenance_sha256": _sha256(manifest_path),
        "corpus_sha256": corpus_sha,
        "annotations_sha256": annotation_sha,
    }
    if schema == LEGACY_SYNTHETIC_SCHEMA:
        if navigation_annotations_path is not None:
            raise TrainingError(
                f"{manifest_path}: legacy synthetic provenance does not bind navigation annotations"
            )
        return result

    navigation_ref = value.get("navigation_annotations")
    if (
        value.get("navigation_annotation_schema") != NAVIGATION_ANNOTATION_SCHEMA
        or value.get("navigation_kinds") != list(NAVIGATION_KINDS)
        or not isinstance(navigation_ref, dict)
        or set(navigation_ref) != {"path", "sha256", "records"}
    ):
        raise TrainingError(f"{manifest_path}: invalid bound navigation annotation contract")
    navigation_sha = _strict_sha256(
        navigation_ref.get("sha256"), "synthetic navigation annotations"
    )
    record_count = navigation_ref.get("records")
    if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count < 1:
        raise TrainingError(f"{manifest_path}: invalid navigation annotation record count")
    bound_navigation = _safe_relative_child(
        manifest_path.parent, navigation_ref.get("path"), "synthetic navigation annotations"
    )
    if _sha256(bound_navigation) != navigation_sha:
        raise TrainingError(f"{manifest_path}: synthetic navigation annotation SHA-256 mismatch")
    try:
        actual_records = sum(
            1 for line in bound_navigation.read_text(encoding="utf-8-sig").splitlines() if line.strip()
        )
    except (OSError, UnicodeError) as exc:
        raise TrainingError(f"cannot read navigation annotations {bound_navigation}: {exc}") from exc
    if actual_records != record_count:
        raise TrainingError(f"{manifest_path}: synthetic navigation annotation record count mismatch")
    if navigation_annotations_path is not None:
        selected_navigation = navigation_annotations_path.resolve()
        if selected_navigation != bound_navigation or _sha256(selected_navigation) != navigation_sha:
            raise TrainingError(
                f"{manifest_path}: selected navigation annotations are not bound by synthetic provenance"
            )

    for item in manifest_documents:
        page_count = item.get("page_count")
        page_navigation = item.get("page_navigation_labels")
        if (
            not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1
            or not isinstance(page_navigation, list) or len(page_navigation) != page_count
        ):
            raise TrainingError(f"{manifest_path}: malformed synthetic page navigation inventory")
        for labels in page_navigation:
            if (
                not isinstance(labels, list)
                or len(labels) != len(set(labels))
                or any(kind not in NAVIGATION_KINDS for kind in labels)
            ):
                raise TrainingError(f"{manifest_path}: malformed synthetic page navigation labels")

    expected_records = sum(
        int(item["page_count"]) * len(NAVIGATION_KINDS) for item in manifest_documents
    )
    if record_count != expected_records:
        raise TrainingError(
            f"{manifest_path}: synthetic navigation annotations must explicitly label every page/kind"
        )
    navigation_rows = load_navigation_annotations(bound_navigation)
    if len(navigation_rows) != record_count:
        raise TrainingError(
            f"{manifest_path}: synthetic navigation annotations must all be human-verified"
        )
    inventory = {item["document_id"]: item for item in manifest_documents}
    for row in navigation_rows:
        document = inventory.get(row["document_id"])
        if (
            document is None
            or row["source_sha256"] != document["pdf_sha256"]
            or row["page"] > document["page_count"]
        ):
            raise TrainingError(f"{manifest_path}: navigation annotation/inventory mismatch")
        expected_presence = (
            "present"
            if row["kind"] in document["page_navigation_labels"][row["page"] - 1]
            else "absent"
        )
        if row["presence"] != expected_presence:
            raise TrainingError(f"{manifest_path}: navigation annotation/presence inventory mismatch")
    result["navigation_annotations_sha256"] = navigation_sha
    return result


def load_documents(
    corpus_path: Path, annotations_path: Path | None = None,
    navigation_annotations_path: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Normalize either the project corpus or synthetic provenance."""
    value = _read_json(corpus_path)
    if not isinstance(value, dict) or not isinstance(value.get("documents"), list):
        raise TrainingError(f"{corpus_path}: unsupported corpus schema")
    synthetic = value.get("schema") in SYNTHETIC_SCHEMAS
    normal = value.get("front_region_schema") == "pdf2md.front-regions.v1"
    if not synthetic and not normal:
        raise TrainingError(f"{corpus_path}: unsupported corpus schema")
    input_hashes: dict[str, str]
    synthetic_normal = normal and any(
        isinstance(item, dict) and item.get("document_type") == "synthetic-front-matter"
        for item in value["documents"]
    )
    if synthetic:
        input_hashes = _validate_synthetic_provenance(
            corpus_path, value, annotations_path,
            navigation_annotations_path=navigation_annotations_path,
        )
    elif synthetic_normal:
        provenance_path = corpus_path.parent / "provenance.json"
        provenance_value = _read_json(provenance_path)
        if not isinstance(provenance_value, dict):
            raise TrainingError(f"{provenance_path}: invalid synthetic provenance")
        input_hashes = _validate_synthetic_provenance(
            provenance_path, provenance_value, annotations_path, expected_corpus_path=corpus_path,
            navigation_annotations_path=navigation_annotations_path,
        )
    else:
        input_hashes = {"corpus_sha256": _sha256(corpus_path)}
    result: dict[str, dict[str, Any]] = {}
    for raw in value["documents"]:
        if not isinstance(raw, dict):
            raise TrainingError(f"{corpus_path}: malformed document")
        document_id = raw.get("document_id" if synthetic else "id")
        relative = raw.get("pdf_path" if synthetic else "local_path")
        expected = raw.get("pdf_sha256" if synthetic else "expected_sha256")
        if not isinstance(document_id, str) or not document_id or document_id in result:
            raise TrainingError(f"{corpus_path}: invalid or duplicate document id")
        pdf = _safe_relative_child(corpus_path.parent, relative, f"{document_id} PDF")
        expected = _strict_sha256(expected, f"{document_id} expected source", optional=not synthetic)
        redistributable = True if synthetic else raw.get("redistributable") is True
        training_eligible = True if synthetic else (
            raw.get("training_eligible") is True and redistributable
        )
        result[document_id] = {
            "id": document_id,
            "pdf": pdf,
            "sha256": expected,
            "training_eligible": training_eligible,
            "redistributable": redistributable,
            "source_kind": "synthetic-cc0" if synthetic or synthetic_normal else "corpus",
        }
    return result, input_hashes


def load_annotations(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise TrainingError(f"cannot read annotations {path}: {exc}") from exc
    exact_keys = {"schema", "document_id", "source_sha256", "page", "kind", "status", "reviewer"}
    seen: set[tuple[str, int]] = set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TrainingError(f"{path}:{number}: invalid JSON: {exc}") from exc
        if not isinstance(item, dict) or set(item) != exact_keys or item.get("schema") != ANNOTATION_SCHEMA:
            raise TrainingError(f"{path}:{number}: incompatible seven-field annotation")
        if item.get("status") not in {"verified", "needs_review"}:
            raise TrainingError(f"{path}:{number}: invalid annotation status")
        if not isinstance(item.get("document_id"), str) or not item["document_id"]:
            raise TrainingError(f"{path}:{number}: invalid document_id")
        _strict_sha256(item.get("source_sha256"), f"{path}:{number} source_sha256")
        if not isinstance(item.get("page"), int) or isinstance(item["page"], bool) or item["page"] < 1:
            raise TrainingError(f"{path}:{number}: invalid physical page")
        if item.get("kind") not in REGION_KINDS:
            raise TrainingError(f"{path}:{number}: invalid kind")
        reviewer = item.get("reviewer")
        if not isinstance(reviewer, str) or not reviewer.strip() or any(ord(char) < 32 for char in reviewer):
            raise TrainingError(f"{path}:{number}: reviewer must be printable")
        if item["status"] != "verified":
            continue
        if re.match(r"^auto(?:$|[:/_-])", reviewer.strip(), flags=re.IGNORECASE):
            raise TrainingError(f"{path}:{number}: verified labels require a human reviewer")
        key = (item["document_id"], item["page"])
        if key in seen:
            raise TrainingError(f"{path}:{number}: duplicate document/page label")
        seen.add(key)
        records.append(item)
    if not records:
        raise TrainingError("no verified annotations")
    return records


def load_navigation_annotations(path: Path) -> list[dict[str, Any]]:
    """Load only explicit, human-verified per-kind navigation labels.

    A missing document/page/kind row is unknown. In particular, this loader
    never turns an omitted row into an absent training target.
    """
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise TrainingError(f"cannot read navigation annotations {path}: {exc}") from exc
    exact_keys = {
        "schema", "document_id", "source_sha256", "page", "kind",
        "presence", "status", "reviewer",
    }
    seen: set[tuple[str, int, str]] = set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TrainingError(f"{path}:{number}: invalid JSON: {exc}") from exc
        if (
            not isinstance(item, dict)
            or set(item) != exact_keys
            or item.get("schema") != NAVIGATION_ANNOTATION_SCHEMA
        ):
            raise TrainingError(f"{path}:{number}: incompatible eight-field navigation annotation")
        if item.get("status") not in {"verified", "needs_review"}:
            raise TrainingError(f"{path}:{number}: invalid annotation status")
        if not isinstance(item.get("document_id"), str) or not item["document_id"]:
            raise TrainingError(f"{path}:{number}: invalid document_id")
        _strict_sha256(item.get("source_sha256"), f"{path}:{number} source_sha256")
        if not isinstance(item.get("page"), int) or isinstance(item["page"], bool) or item["page"] < 1:
            raise TrainingError(f"{path}:{number}: invalid physical page")
        if item.get("kind") not in NAVIGATION_KINDS:
            raise TrainingError(f"{path}:{number}: invalid navigation kind")
        if item.get("presence") not in {"present", "absent"}:
            raise TrainingError(f"{path}:{number}: invalid navigation presence")
        reviewer = item.get("reviewer")
        if not isinstance(reviewer, str) or not reviewer.strip() or any(ord(char) < 32 for char in reviewer):
            raise TrainingError(f"{path}:{number}: reviewer must be printable")
        if item["status"] != "verified":
            continue
        if re.match(r"^auto(?:$|[:/_-])", reviewer.strip(), flags=re.IGNORECASE):
            raise TrainingError(f"{path}:{number}: verified labels require a human reviewer")
        key = (item["document_id"], item["page"], item["kind"])
        if key in seen:
            raise TrainingError(f"{path}:{number}: duplicate document/page/navigation-kind label")
        seen.add(key)
        records.append(item)
    return records


def _selection_pages(value: Any, page_count: int) -> tuple[int, int]:
    text = str(value).strip().casefold()
    if text == "all":
        return 1, page_count
    import re
    match = re.fullmatch(r"([1-9][0-9]*)(?:-([1-9][0-9]*))?", text)
    if not match:
        raise TrainingError(f"non-contiguous or invalid cached selection: {value!r}")
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    if end < start or end - start + 1 != page_count:
        raise TrainingError(f"selection/content-list page count mismatch: {value!r} vs {page_count}")
    return start, end


def _safe_child(root: Path, relative: Any) -> Path:
    return _safe_relative_child(root, relative, "cached content-list")


def load_cached_page(
    pdf: Path, physical_page: int, expected_sha: str,
) -> tuple[Any, PageEvidence, dict[str, Any]]:
    """Load one physical page through the training cache trust boundary.

    The returned raw value is exactly one annotated page. Callers can replay
    the production cascade without classifying neighbouring or unlabelled
    pages, while retaining the manifest, source-hash and smallest-covering-
    selection checks used by training.
    """
    output = pdf.with_suffix(".pdf2md")
    manifest_path = output / "raw" / "manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("selections"), list):
        raise TrainingError(f"{manifest_path}: incompatible manifest")
    source = manifest.get("source")
    manifest_source_sha = source.get("sha256") if isinstance(source, dict) else None
    if _strict_sha256(manifest_source_sha, f"{manifest_path} source") != expected_sha:
        raise TrainingError(f"{manifest_path}: source SHA-256 mismatch")
    manifest_sha = _sha256(manifest_path)
    manifest_source = {
        key: item for key, item in source.items()
        if isinstance(key, str) and isinstance(item, (str, int, float, bool, type(None)))
    }
    manifest_contract = {
        "schema_version": manifest.get("schema_version"),
        "core": manifest.get("core"),
        "core_version": manifest.get("core_version"),
        "cache_version": manifest.get("cache_version"),
        "source": manifest_source,
    }
    matches: list[tuple[int, int, Any, PageEvidence, dict[str, Any]]] = []
    for manifest_index, item in enumerate(manifest["selections"]):
        if not isinstance(item, dict) or "content_list_v2" not in item:
            continue
        content_path = _safe_child(output, item["content_list_v2"])
        value = _read_json(content_path)
        if not isinstance(value, list) or not value:
            raise TrainingError(f"{content_path}: content-list-v2 must be a non-empty page list")
        start, end = _selection_pages(item.get("pages"), len(value))
        if start <= physical_page <= end:
            raw_page = value[physical_page - start]
            evidence = extract_region_evidence([raw_page], start_page=physical_page, max_pages=1)
            if len(evidence.pages) != 1:
                raise TrainingError(f"{content_path}: cannot decode physical page {physical_page}")
            conversion = {
                key: item[key] for key in ("profile", "method", "requested_method", "language")
                if key in item and isinstance(item[key], (str, int, float, bool, type(None)))
            }
            matches.append((end - start + 1, manifest_index, raw_page, evidence.pages[0], {
                "physical_page": physical_page,
                "pages": str(item.get("pages")),
                "content_list_sha256": _sha256(content_path),
                "manifest_sha256": manifest_sha,
                "conversion": conversion,
                "manifest": manifest_contract,
            }))
    if not matches:
        raise TrainingError(f"{pdf}: physical page {physical_page} is absent from cached selections")
    matches.sort(key=lambda pair: (pair[0], pair[1]))
    return matches[0][2], matches[0][3], matches[0][4]


def _page_from_cache(
    pdf: Path, physical_page: int, expected_sha: str,
) -> tuple[PageEvidence, dict[str, Any]]:
    _raw_page, evidence, cache_input = load_cached_page(
        pdf, physical_page, expected_sha,
    )
    return evidence, cache_input


def load_examples(
    annotations_path: Path, corpus_path: Path, *, allow_regression_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    documents, input_hashes = load_documents(corpus_path, annotations_path)
    annotations = load_annotations(annotations_path)
    used_documents: dict[str, dict[str, Any]] = {}
    examples: list[dict[str, Any]] = []
    verified_hashes: dict[str, str] = {}
    selected_inputs: list[dict[str, Any]] = []
    for item in annotations:
        document_id = item["document_id"]
        document = documents.get(document_id)
        if document is None:
            raise TrainingError(f"annotation refers to unknown document {document_id}")
        annotation_sha = item["source_sha256"]
        if document["sha256"] is None:
            raise TrainingError(f"{document_id}: corpus source is not hash-pinned")
        if annotation_sha != document["sha256"]:
            raise TrainingError(f"{document_id}: annotation/corpus SHA-256 mismatch")
        if not document["training_eligible"] and not allow_regression_only:
            raise TrainingError(
                f"{document_id}: training_eligible=false; pass --allow-regression-only only for local experiments"
            )
        if document_id not in verified_hashes:
            verified_hashes[document_id] = _sha256(document["pdf"])
        actual = verified_hashes[document_id]
        if actual != annotation_sha:
            raise TrainingError(f"{document_id}: local PDF SHA-256 mismatch")
        page, cache_input = _page_from_cache(document["pdf"], item["page"], actual)
        examples.append({
            "document_id": document_id,
            "page": item["page"],
            "kind": item["kind"],
            "layout": layout_features(page),
            "text": hashed_text_features(page),
        })
        selected_inputs.append({"document_id": document_id, **cache_input})
        used_documents[document_id] = document
    source_ids = sorted(used_documents)
    metadata = {
        "source_ids": source_ids,
        "redistributable": all(used_documents[item]["redistributable"] for item in source_ids),
        "training_eligible": all(used_documents[item]["training_eligible"] for item in source_ids),
        "source_kinds": sorted({used_documents[item]["source_kind"] for item in source_ids}),
        "inputs": {
            **input_hashes,
            "annotations_sha256": _sha256(annotations_path),
            "source_sha256": {item: verified_hashes[item] for item in source_ids},
            "content_list_sha256": sorted({item["content_list_sha256"] for item in selected_inputs}),
            "feature_schema_version": FEATURES_VERSION,
            "selection_config": {**SELECTION_CONFIG, "selections": selected_inputs},
        },
    }
    if allow_regression_only and any(not used_documents[item]["training_eligible"] for item in source_ids):
        metadata["redistributable"] = False
        metadata["training_eligible"] = False
        metadata["experiment_only"] = True
    return examples, metadata


def load_navigation_examples(
    navigation_annotations_path: Path,
    corpus_path: Path,
    *,
    primary_annotations_path: Path | None = None,
    allow_regression_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Join explicit per-kind labels to one cached page feature vector.

    Multiple labels for the same physical page share one cache read. Missing
    kinds remain absent from targets and are therefore unknown, not negative.
    """
    documents, input_hashes = load_documents(
        corpus_path, primary_annotations_path, navigation_annotations_path
    )
    annotations = load_navigation_annotations(navigation_annotations_path)
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for item in annotations:
        key = (item["document_id"], item["page"])
        group = grouped.setdefault(key, {
            "document_id": item["document_id"],
            "page": item["page"],
            "source_sha256": item["source_sha256"],
            "targets": {},
        })
        if group["source_sha256"] != item["source_sha256"]:
            raise TrainingError(
                f"{item['document_id']} page {item['page']}: inconsistent navigation source SHA-256"
            )
        group["targets"][item["kind"]] = 1 if item["presence"] == "present" else 0

    examples: list[dict[str, Any]] = []
    used_documents: dict[str, dict[str, Any]] = {}
    verified_hashes: dict[str, str] = {}
    selected_inputs: list[dict[str, Any]] = []
    for (document_id, physical_page), group in sorted(grouped.items()):
        document = documents.get(document_id)
        if document is None:
            raise TrainingError(f"navigation annotation refers to unknown document {document_id}")
        annotation_sha = group["source_sha256"]
        if document["sha256"] is None:
            raise TrainingError(f"{document_id}: corpus source is not hash-pinned")
        if annotation_sha != document["sha256"]:
            raise TrainingError(f"{document_id}: navigation annotation/corpus SHA-256 mismatch")
        if not document["training_eligible"] and not allow_regression_only:
            raise TrainingError(
                f"{document_id}: training_eligible=false; pass --allow-regression-only only for local experiments"
            )
        if document_id not in verified_hashes:
            verified_hashes[document_id] = _sha256(document["pdf"])
        actual = verified_hashes[document_id]
        if actual != annotation_sha:
            raise TrainingError(f"{document_id}: local PDF SHA-256 mismatch")
        page, cache_input = _page_from_cache(document["pdf"], physical_page, actual)
        examples.append({
            "document_id": document_id,
            "page": physical_page,
            "targets": {
                kind: int(group["targets"][kind])
                for kind in NAVIGATION_KINDS if kind in group["targets"]
            },
            "layout": layout_features(page),
            "text": hashed_text_features(page),
        })
        selected_inputs.append({"document_id": document_id, **cache_input})
        used_documents[document_id] = document

    source_ids = sorted(used_documents)
    metadata = {
        "source_ids": source_ids,
        "redistributable": all(used_documents[item]["redistributable"] for item in source_ids),
        "training_eligible": all(used_documents[item]["training_eligible"] for item in source_ids),
        "source_kinds": sorted({used_documents[item]["source_kind"] for item in source_ids}),
        "inputs": {
            **input_hashes,
            "navigation_annotations_sha256": _sha256(navigation_annotations_path),
            "source_sha256": {item: verified_hashes[item] for item in source_ids},
            "content_list_sha256": sorted({item["content_list_sha256"] for item in selected_inputs}),
            "feature_schema_version": FEATURES_VERSION,
            "selection_config": {**SELECTION_CONFIG, "selections": selected_inputs},
        },
    }
    if allow_regression_only and any(not used_documents[item]["training_eligible"] for item in source_ids):
        metadata["redistributable"] = False
        metadata["training_eligible"] = False
        metadata["experiment_only"] = True
    return examples, metadata


def split_document(document_id: str, seed: int) -> str:
    bucket = int.from_bytes(
        hashlib.sha256(f"{seed}\0{document_id}".encode("utf-8")).digest()[:8], "big"
    ) % 100
    return "train" if bucket < 70 else ("calibration" if bucket < 85 else "test")


def _matrix(examples: Sequence[dict[str, Any]], field: str, names: Sequence[str]) -> np.ndarray:
    return np.asarray(
        [[float(item[field].get(name, 0.0)) for name in names] for item in examples],
        dtype=np.float64,
    )


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=1, keepdims=True)
    values = np.exp(np.clip(shifted, -745.0, 0.0))
    return values / values.sum(axis=1, keepdims=True)


def _fit_linear(
    x: np.ndarray, y: np.ndarray, class_count: int, *, seed: int,
    epochs: int, learning_rate: float, l2: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if len(x) == 0:
        raise TrainingError("document hash split produced no training pages; change --seed or add documents")
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale < 1e-8] = 1.0
    z = (x - mean) / scale
    rng = np.random.default_rng(seed)
    weights = rng.normal(0.0, 1e-6, size=(class_count, x.shape[1]))
    bias = np.zeros(class_count, dtype=np.float64)
    counts = np.bincount(y, minlength=class_count).astype(np.float64)
    class_weights = len(y) / (class_count * np.maximum(counts, 1.0))
    sample_weights = class_weights[y]
    sample_weights /= sample_weights.mean()
    targets = np.eye(class_count, dtype=np.float64)[y]
    for epoch in range(epochs):
        probabilities = _softmax(z @ weights.T + bias)
        delta = (probabilities - targets) * sample_weights[:, None] / len(y)
        gradient_w = delta.T @ z + l2 * weights
        gradient_b = delta.sum(axis=0)
        rate = learning_rate / math.sqrt(1.0 + epoch / 100.0)
        weights -= rate * gradient_w
        bias -= rate * gradient_b
    # Fold standardization into the portable raw-feature linear head.
    raw_weights = weights / scale[None, :]
    raw_bias = bias - raw_weights @ mean
    return raw_weights, raw_bias, mean, scale


def _predict(x: np.ndarray, weights: np.ndarray, bias: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    if len(x) == 0:
        return np.empty((0, len(bias)), dtype=np.float64)
    return _softmax((x @ weights.T + bias) / temperature)


def _sigmoid(logits: np.ndarray) -> np.ndarray:
    bounded = np.clip(logits, -745.0, 709.0)
    result = np.empty_like(bounded, dtype=np.float64)
    positive = bounded >= 0.0
    result[positive] = 1.0 / (1.0 + np.exp(-bounded[positive]))
    exp_value = np.exp(bounded[~positive])
    result[~positive] = exp_value / (1.0 + exp_value)
    return result


def _fit_binary(
    x: np.ndarray,
    y: np.ndarray,
    *,
    seed: int,
    epochs: int,
    learning_rate: float,
    l2: float,
) -> tuple[np.ndarray, float]:
    if len(x) == 0 or set(map(int, y.tolist())) != {0, 1}:
        raise TrainingError("binary navigation head requires explicit present and absent training labels")
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale < 1e-8] = 1.0
    z = (x - mean) / scale
    rng = np.random.default_rng(seed)
    weights = rng.normal(0.0, 1e-6, size=x.shape[1])
    bias = 0.0
    counts = np.bincount(y, minlength=2).astype(np.float64)
    class_weights = len(y) / (2.0 * np.maximum(counts, 1.0))
    sample_weights = class_weights[y]
    sample_weights /= sample_weights.mean()
    for epoch in range(epochs):
        probabilities = _sigmoid(z @ weights + bias)
        delta = (probabilities - y) * sample_weights / len(y)
        rate = learning_rate / math.sqrt(1.0 + epoch / 100.0)
        weights -= rate * (z.T @ delta + l2 * weights)
        bias -= rate * float(delta.sum())
    raw_weights = weights / scale
    raw_bias = float(bias - raw_weights @ mean)
    return raw_weights, raw_bias


def _predict_binary(
    x: np.ndarray, weights: np.ndarray, bias: float, temperature: float = 1.0,
) -> np.ndarray:
    if len(x) == 0:
        return np.empty(0, dtype=np.float64)
    return _sigmoid((x @ weights + bias) / temperature)


def _binary_temperature(logits: np.ndarray, y: np.ndarray) -> float:
    if len(y) == 0 or len(set(map(int, y.tolist()))) < 2:
        return 1.0
    best = (float("inf"), 1.0)
    for temperature in np.geomspace(0.35, 4.0, 81):
        probabilities = _sigmoid(logits / temperature)
        loss = -(
            y * np.log(np.clip(probabilities, 1e-15, 1.0))
            + (1 - y) * np.log(np.clip(1.0 - probabilities, 1e-15, 1.0))
        ).mean()
        candidate = (float(loss), float(temperature))
        if candidate < best:
            best = candidate
    return best[1]


def _binary_auc(probabilities: np.ndarray, y: np.ndarray) -> float | None:
    positive_count = int((y == 1).sum())
    negative_count = int((y == 0).sum())
    if not positive_count or not negative_count:
        return None
    order = np.argsort(probabilities, kind="stable")
    sorted_probabilities = probabilities[order]
    ranks = np.empty(len(probabilities), dtype=np.float64)
    start = 0
    while start < len(order):
        end = start + 1
        while (
            end < len(order)
            and sorted_probabilities[end] == sorted_probabilities[start]
        ):
            end += 1
        ranks[order[start:end]] = (start + 1 + end) / 2.0
        start = end
    positive_rank_sum = float(ranks[y == 1].sum())
    return (
        positive_rank_sum - positive_count * (positive_count + 1) / 2.0
    ) / (positive_count * negative_count)


def _average_precision(probabilities: np.ndarray, y: np.ndarray) -> float | None:
    positives = int((y == 1).sum())
    if positives == 0:
        return None
    order = np.argsort(-probabilities, kind="stable")
    ranked = y[order]
    precision = np.cumsum(ranked == 1) / np.arange(1, len(ranked) + 1)
    return float(precision[ranked == 1].sum() / positives)


def _binary_evaluation(probabilities: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    support = int(len(y))
    positive = int((y == 1).sum())
    negative = support - positive
    predicted = probabilities >= 0.5
    tp = int(((predicted == 1) & (y == 1)).sum())
    fp = int(((predicted == 1) & (y == 0)).sum())
    fn = int(((predicted == 0) & (y == 1)).sum())
    tn = int(((predicted == 0) & (y == 0)).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    if support:
        brier = float(np.mean((probabilities - y) ** 2))
        ece = 0.0
        for low in np.linspace(0.0, 0.9, 10):
            mask = (probabilities >= low) & (probabilities < low + 0.1 + 1e-12)
            if mask.any():
                ece += float(mask.mean()) * abs(float(y[mask].mean()) - float(probabilities[mask].mean()))
    else:
        brier, ece = 0.0, 0.0
    return {
        "support": support, "positive": positive, "negative": negative,
        "confusion": {"true_positive": tp, "false_positive": fp, "false_negative": fn, "true_negative": tn},
        "precision": precision, "recall": recall, "f1": f1,
        "roc_auc": _binary_auc(probabilities, y),
        "average_precision": _average_precision(probabilities, y),
        "ece": ece, "brier": brier,
    }


def _temperature(probs_logits: np.ndarray, y: np.ndarray) -> float:
    if len(y) == 0:
        return 1.0
    best = (float("inf"), 1.0)
    for temperature in np.geomspace(0.35, 4.0, 81):
        probabilities = _softmax(probs_logits / temperature)
        loss = -np.log(np.clip(probabilities[np.arange(len(y)), y], 1e-15, 1.0)).mean()
        candidate = (float(loss), float(temperature))
        if candidate < best:
            best = candidate
    return best[1]


def _evaluation(probabilities: np.ndarray, y: np.ndarray, classes: Sequence[str]) -> dict[str, Any]:
    confusion = np.zeros((len(classes), len(classes)), dtype=np.int64)
    if len(y):
        predicted = probabilities.argmax(axis=1)
        for truth, guess in zip(y, predicted):
            confusion[int(truth), int(guess)] += 1
    per_class: dict[str, dict[str, float | int]] = {}
    f1s: list[float] = []
    for index, name in enumerate(classes):
        tp = int(confusion[index, index])
        fp = int(confusion[:, index].sum() - tp)
        fn = int(confusion[index, :].sum() - tp)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        f1s.append(f1)
        per_class[name] = {"support": int(confusion[index, :].sum()), "precision": precision, "recall": recall, "f1": f1}
    if len(y):
        confidence = probabilities.max(axis=1)
        correct = probabilities.argmax(axis=1) == y
        ece = 0.0
        for low in np.linspace(0.0, 0.9, 10):
            mask = (confidence >= low) & (confidence < low + 0.1 + 1e-12)
            if mask.any():
                ece += float(mask.mean()) * abs(float(correct[mask].mean()) - float(confidence[mask].mean()))
        truth = np.eye(len(classes))[y]
        brier = float(np.mean(np.sum((probabilities - truth) ** 2, axis=1)))
        coverage_risk = []
        for threshold in (0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.995):
            accepted = confidence >= threshold
            coverage_risk.append({
                "threshold": threshold,
                "coverage": float(accepted.mean()),
                "risk": float(1.0 - correct[accepted].mean()) if accepted.any() else None,
            })
    else:
        ece, brier, coverage_risk = 0.0, 0.0, []
    return {
        "pages": int(len(y)), "per_class": per_class,
        "macro_f1": float(sum(f1s) / len(f1s)) if f1s else 0.0,
        "confusion": {"labels": list(classes), "matrix": confusion.tolist()},
        "ece": ece, "brier": brier, "coverage_risk": coverage_risk,
    }


def _policy(probabilities: np.ndarray, y: np.ndarray, classes: Sequence[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    predicted = probabilities.argmax(axis=1) if len(y) else np.asarray([], dtype=int)
    confidence = probabilities.max(axis=1) if len(y) else np.asarray([], dtype=float)
    ordered = np.sort(probabilities, axis=1) if len(y) else np.empty((0, len(classes)))
    margins = ordered[:, -1] - ordered[:, -2] if len(classes) > 1 and len(y) else np.asarray([], dtype=float)
    for index, name in enumerate(classes):
        support = int((y == index).sum())
        proposed = DEFAULT_THRESHOLD
        proposed_margin = DEFAULT_MARGIN
        sufficient = support >= 10
        if sufficient:
            candidates = sorted(set(float(value) for value in confidence[predicted == index]), reverse=True)
            for threshold in candidates:
                accepted = (predicted == index) & (confidence >= threshold)
                if accepted.sum() >= 5 and float((y[accepted] == index).mean()) >= 0.995:
                    proposed = threshold
            correct = (predicted == index) & (y == index)
            if correct.any():
                proposed_margin = max(DEFAULT_MARGIN, float(np.quantile(margins[correct], 0.1)))
        result[name] = {
            "probability": proposed, "margin": proposed_margin,
            "calibration_support": support, "conservative_default": not sufficient,
        }
    return result


def _binary_policy(
    probabilities: np.ndarray, y: np.ndarray, *, trained: bool,
) -> dict[str, Any]:
    positive = int((y == 1).sum())
    negative = int((y == 0).sum())
    sufficient = bool(trained and positive >= 10 and negative >= 10)
    threshold = 1.0
    selected = False
    if sufficient:
        for candidate in sorted(set(float(value) for value in probabilities), reverse=True):
            accepted = probabilities >= candidate
            true_positive = int(((y == 1) & accepted).sum())
            if (
                true_positive >= 5
                and accepted.any()
                and float((y[accepted] == 1).mean()) >= 0.995
            ):
                threshold = candidate
                selected = True
    return {
        "probability": threshold,
        "target_precision": 0.995,
        "calibration_positive": positive,
        "calibration_negative": negative,
        "calibration_sufficient": sufficient,
        "threshold_selected": selected,
        "auto_action_gate": False,
    }


def _model_feature_names(
    examples: Sequence[dict[str, Any]], field: str,
) -> list[str]:
    if field == "text":
        return [
            *(f"text.hash.{index}" for index in range(512)),
            "text.log1p_chars", "text.token_count_log1p",
        ]
    return sorted({name for item in examples for name in item[field]})


def _navigation_rows(
    items: Sequence[dict[str, Any]],
    field: str,
    feature_names: Sequence[str],
    kind: str,
) -> tuple[np.ndarray, np.ndarray]:
    labelled = [
        item for item in items
        if isinstance(item.get("targets"), Mapping) and kind in item["targets"]
    ]
    if not labelled:
        return (
            np.empty((0, len(feature_names)), dtype=np.float64),
            np.empty(0, dtype=np.int64),
        )
    return (
        _matrix(labelled, field, feature_names),
        np.asarray([int(item["targets"][kind]) for item in labelled], dtype=np.int64),
    )


def _save_navigation_npz(
    path: Path,
    *,
    kind: str,
    feature_names: Sequence[str],
    weights: np.ndarray,
    bias: np.ndarray,
    temperature: np.ndarray,
    trained: np.ndarray,
    min_feature_l1: float,
    max_feature_l1: float,
    metadata: Mapping[str, Any],
) -> str:
    """Atomically write a pickle-free, deterministic offline artifact."""
    expected_shape = (len(NAVIGATION_KINDS), len(feature_names))
    if (
        weights.shape != expected_shape
        or bias.shape != (len(NAVIGATION_KINDS),)
        or temperature.shape != (len(NAVIGATION_KINDS),)
        or trained.shape != (len(NAVIGATION_KINDS),)
        or not np.isfinite(weights).all()
        or not np.isfinite(bias).all()
        or not np.isfinite(temperature).all()
        or (temperature <= 0.0).any()
        or not math.isfinite(min_feature_l1)
        or not math.isfinite(max_feature_l1)
        or min_feature_l1 < 0.0
        or max_feature_l1 < min_feature_l1
    ):
        raise TrainingError("invalid navigation auxiliary artifact arrays")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    metadata_json = json.dumps(
        dict(metadata), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    try:
        with temporary.open("wb") as stream:
            np.savez(
                stream,
                schema=np.asarray(NAVIGATION_AUXILIARY_SCHEMA, dtype=np.str_),
                kind=np.asarray(kind, dtype=np.str_),
                navigation_kinds=np.asarray(NAVIGATION_KINDS, dtype=np.str_),
                feature_names=np.asarray(list(feature_names), dtype=np.str_),
                weights=np.asarray(weights, dtype=np.float64),
                bias=np.asarray(bias, dtype=np.float64),
                temperature=np.asarray(temperature, dtype=np.float64),
                trained=np.asarray(trained, dtype=np.bool_),
                min_feature_l1=np.asarray(min_feature_l1, dtype=np.float64),
                max_feature_l1=np.asarray(max_feature_l1, dtype=np.float64),
                metadata_json=np.asarray(metadata_json, dtype=np.str_),
            )
        payload = temporary.read_bytes()
        temporary.replace(target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(payload).hexdigest()


def _train_navigation_auxiliary(
    examples: Sequence[dict[str, Any]],
    source_metadata: Mapping[str, Any],
    *,
    output: Path,
    seed: int,
    epochs: int,
    learning_rate: float,
    l2: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Train three independent masked sigmoid heads for offline evaluation."""
    if not examples:
        for field in ("layout", "text"):
            try:
                (output / f"navigation-{field}.npz").unlink()
            except FileNotFoundError:
                pass
        return (
            {
                "schema": "pdf2md.navigation-aux-training-metrics.v1",
                "status": "no_explicit_labels",
                "tasks": list(NAVIGATION_KINDS),
                "pages": 0,
                "explicit_labels": 0,
                "artifact_sha256": {},
                "source": dict(source_metadata),
            },
            {
                "schema": "pdf2md.navigation-aux-policy.v1",
                "status": "no_explicit_labels",
                "experimental": True,
                "approved_for_auto_action": False,
                "artifact_sha256": {},
                "models": {},
            },
        )

    documents = sorted({str(item["document_id"]) for item in examples})
    split_by_document = {item: split_document(item, seed) for item in documents}
    split_examples = {
        split: [item for item in examples if split_by_document[item["document_id"]] == split]
        for split in ("train", "calibration", "test")
    }
    model_metrics: dict[str, Any] = {}
    model_policies: dict[str, Any] = {}
    fingerprints: dict[str, str] = {}
    any_trained = False
    for field_index, field in enumerate(("layout", "text")):
        feature_names = _model_feature_names(examples, field)
        if not feature_names:
            raise TrainingError(f"navigation {field} examples have no features")
        weights = np.zeros((len(NAVIGATION_KINDS), len(feature_names)), dtype=np.float64)
        bias = np.zeros(len(NAVIGATION_KINDS), dtype=np.float64)
        temperature = np.ones(len(NAVIGATION_KINDS), dtype=np.float64)
        trained = np.zeros(len(NAVIGATION_KINDS), dtype=np.bool_)
        field_metrics: dict[str, Any] = {}
        field_policy: dict[str, Any] = {}
        for kind_index, navigation_kind in enumerate(NAVIGATION_KINDS):
            x_train, y_train = _navigation_rows(
                split_examples["train"], field, feature_names, navigation_kind,
            )
            if len(y_train) and set(map(int, y_train.tolist())) == {0, 1}:
                weights[kind_index], bias[kind_index] = _fit_binary(
                    x_train, y_train,
                    seed=seed + 100 + field_index * 10 + kind_index,
                    epochs=epochs, learning_rate=learning_rate, l2=l2,
                )
                trained[kind_index] = True
                any_trained = True
            x_cal, y_cal = _navigation_rows(
                split_examples["calibration"], field, feature_names, navigation_kind,
            )
            if trained[kind_index] and len(y_cal):
                logits_cal = x_cal @ weights[kind_index] + bias[kind_index]
                temperature[kind_index] = _binary_temperature(logits_cal, y_cal)
            probabilities_cal = _predict_binary(
                x_cal, weights[kind_index], float(bias[kind_index]),
                float(temperature[kind_index]),
            )
            field_policy[navigation_kind] = _binary_policy(
                probabilities_cal, y_cal, trained=bool(trained[kind_index]),
            )
            field_policy[navigation_kind]["trained"] = bool(trained[kind_index])
            field_metrics[navigation_kind] = {}
            for split, items in split_examples.items():
                x_split, y_split = _navigation_rows(
                    items, field, feature_names, navigation_kind,
                )
                probabilities = _predict_binary(
                    x_split, weights[kind_index], float(bias[kind_index]),
                    float(temperature[kind_index]),
                )
                field_metrics[navigation_kind][split] = _binary_evaluation(
                    probabilities, y_split,
                )

        x_train_all = _matrix(split_examples["train"], field, feature_names)
        l1 = np.sum(np.abs(x_train_all), axis=1) if len(x_train_all) else np.empty(0)
        min_l1 = max(0.0, float(np.quantile(l1, 0.005)) * 0.25) if len(l1) else 0.0
        max_l1 = float(np.quantile(l1, 0.995) * 4.0 + 1e-9) if len(l1) else 0.0
        artifact_metadata = {
            **dict(source_metadata),
            "seed": seed,
            "epochs": epochs,
            "l2": l2,
            "split_by_document": split_by_document,
            "explicit_labels_only": True,
            "missing_label_semantics": "unknown",
            "experimental": True,
            "approved_for_auto_action": False,
        }
        artifact_path = output / f"navigation-{field}.npz"
        fingerprints[field] = _save_navigation_npz(
            artifact_path,
            kind=field,
            feature_names=feature_names,
            weights=weights,
            bias=bias,
            temperature=temperature,
            trained=trained,
            min_feature_l1=min_l1,
            max_feature_l1=max_l1,
            metadata=artifact_metadata,
        )
        model_metrics[field] = field_metrics
        model_policies[field] = field_policy

    split_summary = {
        split: {
            "documents": sorted({item["document_id"] for item in items}),
            "pages": len(items),
            "explicit_labels": sum(len(item.get("targets", {})) for item in items),
            "per_kind": {
                kind: {
                    "present": sum(item.get("targets", {}).get(kind) == 1 for item in items),
                    "absent": sum(item.get("targets", {}).get(kind) == 0 for item in items),
                }
                for kind in NAVIGATION_KINDS
            },
        }
        for split, items in split_examples.items()
    }
    status = "trained" if any_trained else "insufficient_training_support"
    metrics = {
        "schema": "pdf2md.navigation-aux-training-metrics.v1",
        "status": status,
        "tasks": list(NAVIGATION_KINDS),
        "pages": len(examples),
        "explicit_labels": sum(len(item.get("targets", {})) for item in examples),
        "splits": split_summary,
        "models": model_metrics,
        "artifact_sha256": fingerprints,
        "source": dict(source_metadata),
    }
    policy = {
        "schema": "pdf2md.navigation-aux-policy.v1",
        "status": status,
        "experimental": True,
        "approved_for_auto_action": False,
        "target_precision": 0.995,
        "artifact_sha256": fingerprints,
        "models": model_policies,
    }
    return metrics, policy


def train(
    examples: list[dict[str, Any]], source_metadata: dict[str, Any], *, output: Path,
    navigation_examples: Sequence[dict[str, Any]] = (),
    navigation_source_metadata: Mapping[str, Any] | None = None,
    seed: int = 7, epochs: int = 600, learning_rate: float = 0.2,
    l2: float = 1e-3, allow_small: bool = False, min_documents: int = 12,
    min_per_class: int = 4,
) -> dict[str, Any]:
    documents = sorted({item["document_id"] for item in examples})
    labels = Counter(item["kind"] for item in examples)
    if not allow_small:
        if len(documents) < min_documents:
            raise TrainingError(f"need at least {min_documents} documents (found {len(documents)}); use --allow-small for a pipeline experiment")
    classes = sorted(labels)
    if len(classes) < 2:
        raise TrainingError("at least two page-role classes are required")
    class_index = {name: index for index, name in enumerate(classes)}
    split_by_document = {item: split_document(item, seed) for item in documents}
    split_examples = {
        split: [item for item in examples if split_by_document[item["document_id"]] == split]
        for split in ("train", "calibration", "test")
    }
    if not split_examples["train"]:
        raise TrainingError("document hash split has no training documents; change --seed")
    if not split_examples["calibration"] or not split_examples["test"]:
        raise TrainingError("document hash split requires non-empty calibration and test documents; change --seed or add documents")
    train_labels = Counter(item["kind"] for item in split_examples["train"])
    unseen = sorted(set(classes) - set(train_labels))
    if unseen:
        raise TrainingError(f"classes absent from the training-document split: {unseen}; change --seed or add documents")
    if not allow_small:
        sparse = {name: train_labels[name] for name in classes if train_labels[name] < min_per_class}
        if sparse:
            raise TrainingError(f"training-split classes below --min-per-class={min_per_class}: {sparse}")
    output.mkdir(parents=True, exist_ok=True)
    model_metrics: dict[str, Any] = {}
    policies: dict[str, Any] = {}
    fingerprints: dict[str, str] = {}
    for model_offset, field in enumerate(("layout", "text")):
        feature_names = _model_feature_names(examples, field)
        train_items = split_examples["train"]
        x_train = _matrix(train_items, field, feature_names)
        y_train = np.asarray([class_index[item["kind"]] for item in train_items], dtype=np.int64)
        weights, bias, mean, scale = _fit_linear(
            x_train, y_train, len(classes), seed=seed + model_offset,
            epochs=epochs, learning_rate=learning_rate, l2=l2,
        )
        cal_items = split_examples["calibration"]
        x_cal = _matrix(cal_items, field, feature_names)
        y_cal = np.asarray([class_index[item["kind"]] for item in cal_items], dtype=np.int64)
        logits_cal = x_cal @ weights.T + bias if len(x_cal) else np.empty((0, len(classes)))
        temperature = _temperature(logits_cal, y_cal)
        l1 = np.sum(np.abs(x_train), axis=1)
        artifact_metadata = {
            **source_metadata, "seed": seed, "epochs": epochs, "l2": l2,
            "split_by_document": split_by_document,
            "standardization_folded_into_weights": True,
            "training_pages": len(train_items),
            "experimental": bool(allow_small or source_metadata.get("experiment_only")),
            "approved_for_auto_action": False,
        }
        artifact = {
            "schema": ARTIFACT_SCHEMA, "kind": field, "classes": classes,
            "feature_names": feature_names, "weights": weights.tolist(), "bias": bias.tolist(),
            "temperature": temperature,
            "ood": {
                "min_known_fraction": 0.0,
                "min_feature_l1": max(0.0, float(np.quantile(l1, 0.005)) * 0.25) if len(l1) else 0.0,
                "max_feature_l1": float(np.quantile(l1, 0.995) * 4.0 + 1e-9) if len(l1) else None,
            },
            "metadata": artifact_metadata,
        }
        fingerprints[field] = save_json_artifact(output / f"{field}.json", artifact)
        loaded = load_model_artifact(output / f"{field}.json", expected_kind=field)
        if loaded is None:
            raise TrainingError(f"saved {field} artifact failed validation")
        model_metrics[field] = {}
        for split, items in split_examples.items():
            x = _matrix(items, field, feature_names)
            y = np.asarray([class_index[item["kind"]] for item in items], dtype=np.int64)
            model_metrics[field][split] = _evaluation(_predict(x, weights, bias, temperature), y, classes)
        policies[field] = _policy(_predict(x_cal, weights, bias, temperature), y_cal, classes)
    split_summary = {
        split: {
            "documents": sorted({item["document_id"] for item in items}),
            "pages": len(items),
        }
        for split, items in split_examples.items()
    }
    navigation_metrics, navigation_policy = _train_navigation_auxiliary(
        navigation_examples,
        navigation_source_metadata or {
            "source_ids": [],
            "redistributable": True,
            "training_eligible": True,
            "inputs": {},
        },
        output=output,
        seed=seed,
        epochs=epochs,
        learning_rate=learning_rate,
        l2=l2,
    )
    metrics = {
        "schema": "pdf2md.region-training-metrics.v1", "seed": seed,
        "classes": classes, "splits": split_summary, "models": model_metrics,
        "artifact_sha256": fingerprints, "source": source_metadata,
        "navigation_auxiliary": navigation_metrics,
    }
    class_thresholds = {
        name: max(policies["layout"][name]["probability"], policies["text"][name]["probability"])
        for name in classes
    }
    class_margins = {
        name: max(policies["layout"][name]["margin"], policies["text"][name]["margin"])
        for name in classes
    }
    policy = {
        "schema": "pdf2md.region-cascade-policy.v1",
        "experimental": True,
        "approved_for_auto_action": False,
        "artifact_sha256": fingerprints,
        "target_precision": 0.995, "default_probability": DEFAULT_THRESHOLD,
        "default_margin": DEFAULT_MARGIN,
        "thresholds": {"layout": DEFAULT_THRESHOLD, "text": DEFAULT_THRESHOLD, **class_thresholds},
        "margins": {"layout": 0.20, "text": DEFAULT_MARGIN, **class_margins},
        "models": policies,
        "navigation_auxiliary": navigation_policy,
    }
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (output / "policy.json").write_text(json.dumps(policy, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return metrics


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train calibrated PDF2MD front-region linear heads")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument(
        "--navigation-annotations",
        type=Path,
        help=(
            "optional explicit present/absent navigation labels; the project "
            "corpus uses data/training/navigation-annotations.jsonl, while "
            "custom corpora only auto-discover a sibling file"
        ),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-regression-only", action="store_true")
    parser.add_argument("--allow-small", action="store_true")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--epochs", type=int, default=600)
    parser.add_argument("--learning-rate", type=float, default=0.2)
    parser.add_argument("--l2", type=float, default=1e-3)
    parser.add_argument("--min-documents", type=int, default=12)
    parser.add_argument("--min-per-class", type=int, default=4)
    return parser


def resolve_navigation_annotations(
    corpus: Path, explicit: Path | None,
) -> Path | None:
    """Resolve optional labels without leaking the project set into a custom corpus."""
    if explicit is not None:
        return explicit
    if corpus.resolve() == DEFAULT_CORPUS.resolve():
        return DEFAULT_NAVIGATION_ANNOTATIONS if DEFAULT_NAVIGATION_ANNOTATIONS.is_file() else None
    sibling = corpus.parent / "navigation-annotations.jsonl"
    return sibling if sibling.is_file() else None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sibling_annotations = args.corpus.parent / "annotations.jsonl"
    annotations = args.annotations or (sibling_annotations if sibling_annotations.is_file() else DEFAULT_ANNOTATIONS)
    navigation_annotations = resolve_navigation_annotations(
        args.corpus, args.navigation_annotations,
    )
    try:
        examples, source_metadata = load_examples(
            annotations, args.corpus, allow_regression_only=args.allow_regression_only,
        )
        if navigation_annotations is None:
            navigation_examples: list[dict[str, Any]] = []
            navigation_source_metadata: dict[str, Any] = {
                "source_ids": [],
                "redistributable": True,
                "training_eligible": True,
                "inputs": {},
            }
        else:
            navigation_examples, navigation_source_metadata = load_navigation_examples(
                navigation_annotations,
                args.corpus,
                primary_annotations_path=annotations,
                allow_regression_only=args.allow_regression_only,
            )
        metrics = train(
            examples, source_metadata, output=args.output, seed=args.seed,
            navigation_examples=navigation_examples,
            navigation_source_metadata=navigation_source_metadata,
            epochs=args.epochs, learning_rate=args.learning_rate, l2=args.l2,
            allow_small=args.allow_small, min_documents=args.min_documents,
            min_per_class=args.min_per_class,
        )
    except TrainingError as exc:
        print(f"training error: {exc}", file=sys.stderr)
        return 2
    print(
        f"trained {sum(item['pages'] for item in metrics['splits'].values())} pages / "
        f"{sum(len(item['documents']) for item in metrics['splits'].values())} documents -> {args.output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
