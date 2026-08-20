#!/usr/bin/env python3
"""Validate and prepare local front-page classification training metadata.

This tool is intentionally offline.  It never downloads a dataset, renders a
PDF, runs OCR, or stores document text/images in the repository.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import unicodedata
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
TRAINING_DIR = DATA_DIR / "training"
DEFAULT_CORPUS = DATA_DIR / "corpus.json"
DEFAULT_SOURCES = TRAINING_DIR / "sources.json"
DEFAULT_ANNOTATIONS = TRAINING_DIR / "annotations.jsonl"
DEFAULT_NAVIGATION_ANNOTATIONS = TRAINING_DIR / "navigation-annotations.jsonl"
DEFAULT_CANDIDATES = TRAINING_DIR / "local" / "bootstrap-candidates.jsonl"
DEFAULT_REVIEW = TRAINING_DIR / "local" / "review.json"

SOURCE_SCHEMA = "pdf2md.front-training-sources.v1"
ANNOTATION_SCHEMA = "pdf2md.front-page-label.v1"
NAVIGATION_ANNOTATION_SCHEMA = "pdf2md.front-navigation-label.v1"
REVIEW_SCHEMA = "pdf2md.front-review-queue.v1"

REGION_KINDS = {
    "cover", "legal", "revision_history", "preface", "abstract",
    "acknowledgements", "contents", "list_of_figures", "list_of_tables",
    "abbreviations", "nomenclature", "body_start", "other_front",
}
STATUSES = {"verified", "needs_review"}
ANNOTATION_FIELDS = {
    "schema", "document_id", "source_sha256", "page", "kind", "status", "reviewer",
}
NAVIGATION_ANNOTATION_FIELDS = ANNOTATION_FIELDS | {"presence"}
NAVIGATION_KINDS = {"contents", "list_of_figures", "list_of_tables"}
NAVIGATION_PRESENCES = {"present", "absent"}
SOURCE_FIELDS = {
    "id", "name", "official_url", "license", "license_url", "training_use",
    "use_for", "download_policy", "notes",
}
TRAINING_USES = {
    "permitted-with-attribution",
    "conditional-per-item-license",
    "conditional-image-license",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
AUTO_REVIEWER_PREFIX = "auto:"

_EXPLICIT_HEADINGS = {
    "table of contents": "contents",
    "contents": "contents",
    "\u76ee\u5f55": "contents",
    "\u76ee\u9304": "contents",
    "\u76ee\u6b21": "contents",
    "list of figures": "list_of_figures",
    "table of figures": "list_of_figures",
    "list of illustrations": "list_of_figures",
    "\u56fe\u76ee\u5f55": "list_of_figures",
    "\u5716\u76ee\u9304": "list_of_figures",
    "\u63d2\u56fe\u76ee\u5f55": "list_of_figures",
    "\u63d2\u5716\u76ee\u9304": "list_of_figures",
    "list of tables": "list_of_tables",
    "table of tables": "list_of_tables",
    "\u8868\u76ee\u5f55": "list_of_tables",
    "\u8868\u76ee\u9304": "list_of_tables",
    "\u8868\u683c\u76ee\u5f55": "list_of_tables",
    "\u8868\u683c\u76ee\u9304": "list_of_tables",
    "revision history": "revision_history",
    "document history": "revision_history",
    "record of revisions": "revision_history",
    "\u4fee\u8ba2\u8bb0\u5f55": "revision_history",
    "\u4fee\u8a02\u8a18\u9304": "revision_history",
    "\u7248\u672c\u5386\u53f2": "revision_history",
    "preface": "preface",
    "foreword": "preface",
    "\u524d\u8a00": "preface",
    "\u5e8f\u8a00": "preface",
    "abstract": "abstract",
    "\u6458\u8981": "abstract",
    "acknowledgements": "acknowledgements",
    "acknowledgments": "acknowledgements",
    "\u81f4\u8c22": "acknowledgements",
    "\u81f4\u8b1d": "acknowledgements",
    "list of abbreviations": "abbreviations",
    "abbreviations": "abbreviations",
    "list of acronyms": "abbreviations",
    "\u7f29\u7565\u8bed": "abbreviations",
    "\u7e2e\u7565\u8a9e": "abbreviations",
    "nomenclature": "nomenclature",
    "list of symbols": "nomenclature",
    "\u7b26\u53f7\u8bf4\u660e": "nomenclature",
    "\u7b26\u865f\u8aaa\u660e": "nomenclature",
    "legal disclaimer notice": "legal",
    "legal notice": "legal",
    "copyright": "legal",
    "\u7248\u6743\u58f0\u660e": "legal",
    "\u7248\u6b0a\u8072\u660e": "legal",
}
_BODY_HEADING = re.compile(
    r"^(?:introduction|chapter\s+1(?:\b|[. :\-])|"
    r"1(?:\.0+)?[. :\-]+(?:introduction|overview)|"
    r"\u7b2c(?:\u4e00|1)\u7ae0(?:\b|[. :\uff1a\-])|"
    r"\u4e00[\u3001. ]+(?:\u6982\u8ff0|\u7eea\u8bba|\u7dd2\u8ad6|\u5f15\u8a00))",
    re.IGNORECASE,
)


class TrainingDataError(ValueError):
    """Raised when versioned training metadata violates its contract."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise TrainingDataError(f"missing file: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise TrainingDataError(f"invalid JSON in {path}: {error}") from error


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise TrainingDataError(f"cannot read {path}: {error}") from error
    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise TrainingDataError(f"{path}:{number}: invalid JSON: {error}") from error
        if not isinstance(value, dict):
            raise TrainingDataError(f"{path}:{number}: annotation must be an object")
        records.append(value)
    return records


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    text = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    _atomic_write(path, text)


def _is_https(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("https://") and len(value) > 8


def load_sources(path: Path = DEFAULT_SOURCES) -> dict[str, Any]:
    manifest = _read_json(path)
    if not isinstance(manifest, dict) or manifest.get("schema") != SOURCE_SCHEMA:
        raise TrainingDataError(f"{path}: schema must be {SOURCE_SCHEMA}")
    if manifest.get("download_policy") != "manual-only":
        raise TrainingDataError(f"{path}: top-level download_policy must be manual-only")
    datasets = manifest.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise TrainingDataError(f"{path}: datasets must be a non-empty list")
    seen: set[str] = set()
    for index, item in enumerate(datasets):
        label = f"{path}:datasets[{index}]"
        if not isinstance(item, dict) or set(item) != SOURCE_FIELDS:
            raise TrainingDataError(f"{label}: fields must be {sorted(SOURCE_FIELDS)}")
        dataset_id = item["id"]
        if not isinstance(dataset_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", dataset_id):
            raise TrainingDataError(f"{label}: invalid id")
        if dataset_id in seen:
            raise TrainingDataError(f"{label}: duplicate id {dataset_id}")
        seen.add(dataset_id)
        for field in ("name", "license", "notes"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise TrainingDataError(f"{label}: {field} must be non-empty")
        if not _is_https(item["official_url"]) or not _is_https(item["license_url"]):
            raise TrainingDataError(f"{label}: official_url and license_url must use HTTPS")
        if item["training_use"] not in TRAINING_USES:
            raise TrainingDataError(f"{label}: unknown training_use")
        if item["download_policy"] != "manual-only":
            raise TrainingDataError(f"{label}: automatic dataset downloads are forbidden")
        if (
            not isinstance(item["use_for"], list)
            or not item["use_for"]
            or any(not isinstance(value, str) or not value for value in item["use_for"])
        ):
            raise TrainingDataError(f"{label}: use_for must be a non-empty string list")
    return manifest


def load_corpus(path: Path = DEFAULT_CORPUS) -> dict[str, Any]:
    manifest = _read_json(path)
    if (
        not isinstance(manifest, dict)
        or manifest.get("front_region_schema") != "pdf2md.front-regions.v1"
        or not isinstance(manifest.get("documents"), list)
    ):
        raise TrainingDataError(f"{path}: incompatible corpus manifest")
    seen: set[str] = set()
    for index, item in enumerate(manifest["documents"]):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise TrainingDataError(f"{path}:documents[{index}]: missing id")
        if item["id"] in seen:
            raise TrainingDataError(f"{path}: duplicate document id {item['id']}")
        seen.add(item["id"])
    return manifest


def _corpus_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in manifest["documents"]}


def _local_pdf_path(document: dict[str, Any], data_dir: Path) -> Path:
    value = document.get("local_path")
    if not isinstance(value, str) or not value:
        raise TrainingDataError(f"{document.get('id', '?')}: local_path is required")
    normalised = value.replace("\\", "/")
    pure = PurePosixPath(normalised)
    if (
        pure.is_absolute()
        or re.match(r"^[A-Za-z]:", normalised)
        or ".." in pure.parts
        or any(part in {"", "."} for part in pure.parts)
    ):
        raise TrainingDataError(f"{document['id']}: unsafe local_path")
    return data_dir.joinpath(*pure.parts)


def inspect_path(document: dict[str, Any], data_dir: Path) -> Path:
    pdf_path = _local_pdf_path(document, data_dir)
    return pdf_path.with_name(pdf_path.name + "2md") / "raw" / "inspect.json"


def _inspect_page_count(document: dict[str, Any], data_dir: Path) -> int | None:
    path = inspect_path(document, data_dir)
    if not path.is_file():
        return None
    value = _read_json(path)
    observed_sha = (
        value.get("source", {}).get("sha256")
        if isinstance(value, dict) and isinstance(value.get("source"), dict)
        else None
    )
    if observed_sha != document.get("expected_sha256"):
        raise TrainingDataError(f"{document['id']}: inspect source hash does not match corpus")
    count = value.get("page_count") if isinstance(value, dict) else None
    return count if isinstance(count, int) and count > 0 else None


def _synthetic_pdf_page_count(
    document: dict[str, Any], data_dir: Path,
) -> int | None:
    """Verify a generated PDF and read only its page tree when inspect is absent."""
    if document.get("document_type") != "synthetic-front-matter":
        return None
    expected_sha = document.get("expected_sha256")
    expected_size = document.get("expected_size")
    if (
        not isinstance(expected_sha, str)
        or SHA256_RE.fullmatch(expected_sha) is None
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size <= 0
    ):
        raise TrainingDataError(
            f"{document.get('id', '?')}: synthetic PDF requires pinned SHA-256 and size"
        )
    pdf_path = _local_pdf_path(document, data_dir)
    root = data_dir.resolve()
    try:
        resolved = pdf_path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise TrainingDataError(
            f"{document['id']}: synthetic PDF path is missing or escapes corpus directory"
        ) from error
    if pdf_path.is_symlink() or not resolved.is_file():
        raise TrainingDataError(f"{document['id']}: synthetic PDF must be a regular file")
    if resolved.stat().st_size != expected_size:
        raise TrainingDataError(f"{document['id']}: synthetic PDF size does not match corpus")
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise TrainingDataError(f"{document['id']}: cannot hash synthetic PDF") from error
    if digest.hexdigest() != expected_sha:
        raise TrainingDataError(f"{document['id']}: synthetic PDF SHA-256 does not match corpus")
    try:
        from pypdf import PdfReader

        count = len(PdfReader(str(resolved), strict=True).pages)
    except Exception as error:
        raise TrainingDataError(f"{document['id']}: cannot read synthetic PDF page count") from error
    if count < 1:
        raise TrainingDataError(f"{document['id']}: synthetic PDF has no pages")
    return count


def validate_annotations(
    records: list[dict[str, Any]],
    corpus: dict[str, Any],
    *,
    data_dir: Path,
) -> list[dict[str, Any]]:
    documents = _corpus_index(corpus)
    seen_pages: set[tuple[str, str, int]] = set()
    page_counts: dict[str, int | None] = {}
    for index, record in enumerate(records):
        label = f"annotation[{index}]"
        if set(record) != ANNOTATION_FIELDS:
            raise TrainingDataError(f"{label}: fields must be {sorted(ANNOTATION_FIELDS)}")
        if record["schema"] != ANNOTATION_SCHEMA:
            raise TrainingDataError(f"{label}: schema must be {ANNOTATION_SCHEMA}")
        document_id = record["document_id"]
        if document_id not in documents:
            raise TrainingDataError(f"{label}: unknown document_id {document_id!r}")
        source_sha = record["source_sha256"]
        if not isinstance(source_sha, str) or SHA256_RE.fullmatch(source_sha) is None:
            raise TrainingDataError(f"{label}: source_sha256 must be 64 lowercase hex characters")
        expected_sha = documents[document_id].get("expected_sha256")
        if expected_sha is None:
            raise TrainingDataError(f"{label}: corpus source is not hash-pinned")
        if source_sha != expected_sha:
            raise TrainingDataError(f"{label}: source_sha256 does not match corpus.json")
        page = record["page"]
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise TrainingDataError(f"{label}: page must be a positive 1-based physical page")
        if document_id not in page_counts:
            page_counts[document_id] = _inspect_page_count(documents[document_id], data_dir)
        count = page_counts[document_id]
        if count is not None and page > count:
            raise TrainingDataError(f"{label}: page {page} exceeds inspect page_count {count}")
        if record["kind"] not in REGION_KINDS:
            raise TrainingDataError(f"{label}: unknown kind {record['kind']!r}")
        if record["status"] not in STATUSES:
            raise TrainingDataError(f"{label}: unknown status {record['status']!r}")
        reviewer = record["reviewer"]
        if (
            not isinstance(reviewer, str)
            or not reviewer.strip()
            or any(ord(character) < 32 for character in reviewer)
        ):
            raise TrainingDataError(f"{label}: reviewer must be a non-empty printable string")
        if record["status"] == "verified" and reviewer.startswith(AUTO_REVIEWER_PREFIX):
            raise TrainingDataError(f"{label}: automatic candidates cannot be verified")
        page_key = (document_id, source_sha, page)
        if page_key in seen_pages:
            raise TrainingDataError(f"{label}: duplicate page label for {document_id} page {page}")
        seen_pages.add(page_key)
    return records


def load_annotations(
    path: Path = DEFAULT_ANNOTATIONS,
    *,
    corpus_path: Path = DEFAULT_CORPUS,
) -> list[dict[str, Any]]:
    corpus = load_corpus(corpus_path)
    return validate_annotations(_read_jsonl(path), corpus, data_dir=corpus_path.parent)


def validate_navigation_annotations(
    records: list[dict[str, Any]],
    corpus: dict[str, Any],
    *,
    data_dir: Path,
) -> list[dict[str, Any]]:
    """Validate positive, page-local navigation labels.

    This is deliberately separate from the mutually exclusive primary page
    labels consumed by the current evaluator and trainer. One physical page
    may therefore have a primary ``abstract`` label and an independent
    navigation ``contents`` label. Presence and absence are always explicit;
    an omitted page/kind pair remains unknown. Geometry, text, OCR output, and
    images are intentionally forbidden.
    """
    documents = _corpus_index(corpus)
    seen_labels: set[tuple[str, str, int, str]] = set()
    page_counts: dict[str, int] = {}
    for index, record in enumerate(records):
        label = f"navigation_annotation[{index}]"
        if set(record) != NAVIGATION_ANNOTATION_FIELDS:
            raise TrainingDataError(
                f"{label}: fields must be {sorted(NAVIGATION_ANNOTATION_FIELDS)}"
            )
        if record["schema"] != NAVIGATION_ANNOTATION_SCHEMA:
            raise TrainingDataError(
                f"{label}: schema must be {NAVIGATION_ANNOTATION_SCHEMA}"
            )
        document_id = record["document_id"]
        if document_id not in documents:
            raise TrainingDataError(f"{label}: unknown document_id {document_id!r}")
        source_sha = record["source_sha256"]
        if not isinstance(source_sha, str) or SHA256_RE.fullmatch(source_sha) is None:
            raise TrainingDataError(
                f"{label}: source_sha256 must be 64 lowercase hex characters"
            )
        expected_sha = documents[document_id].get("expected_sha256")
        if expected_sha is None:
            raise TrainingDataError(f"{label}: corpus source is not hash-pinned")
        if source_sha != expected_sha:
            raise TrainingDataError(f"{label}: source_sha256 does not match corpus.json")
        page = record["page"]
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise TrainingDataError(
                f"{label}: page must be a positive 1-based physical page"
            )
        if document_id not in page_counts:
            count = _inspect_page_count(documents[document_id], data_dir)
            if count is None:
                count = _synthetic_pdf_page_count(documents[document_id], data_dir)
            if count is None:
                raise TrainingDataError(
                    f"{label}: pinned inspect metadata is required to validate page bounds"
                )
            page_counts[document_id] = count
        if page > page_counts[document_id]:
            raise TrainingDataError(
                f"{label}: page {page} exceeds inspect page_count {page_counts[document_id]}"
            )
        kind = record["kind"]
        if kind not in NAVIGATION_KINDS:
            raise TrainingDataError(f"{label}: unknown navigation kind {kind!r}")
        if record["presence"] not in NAVIGATION_PRESENCES:
            raise TrainingDataError(
                f"{label}: unknown navigation presence {record['presence']!r}"
            )
        if record["status"] not in STATUSES:
            raise TrainingDataError(f"{label}: unknown status {record['status']!r}")
        reviewer = record["reviewer"]
        if (
            not isinstance(reviewer, str)
            or not reviewer.strip()
            or any(ord(character) < 32 for character in reviewer)
        ):
            raise TrainingDataError(
                f"{label}: reviewer must be a non-empty printable string"
            )
        if record["status"] == "verified" and reviewer.startswith(AUTO_REVIEWER_PREFIX):
            raise TrainingDataError(f"{label}: automatic candidates cannot be verified")
        navigation_key = (document_id, source_sha, page, kind)
        if navigation_key in seen_labels:
            raise TrainingDataError(
                f"{label}: duplicate {kind} label for {document_id} page {page}"
            )
        seen_labels.add(navigation_key)
    return records


def load_navigation_annotations(
    path: Path = DEFAULT_NAVIGATION_ANNOTATIONS,
    *,
    corpus_path: Path = DEFAULT_CORPUS,
) -> list[dict[str, Any]]:
    corpus = load_corpus(corpus_path)
    return validate_navigation_annotations(
        _read_jsonl(path), corpus, data_dir=corpus_path.parent
    )


def _normalise_heading(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .:\uff1a-\u2013\u2014")


def _outline_kind(title: Any, depth: Any) -> str | None:
    heading = _normalise_heading(title)
    explicit = _EXPLICIT_HEADINGS.get(heading)
    if explicit:
        return explicit
    if depth == 0 and _BODY_HEADING.match(heading):
        return "body_start"
    return None


def _load_inspect_for_bootstrap(
    document: dict[str, Any],
    data_dir: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    path = inspect_path(document, data_dir)
    if not path.is_file():
        return None, f"{document['id']}: no local inspect.json"
    try:
        value = _read_json(path)
    except TrainingDataError as error:
        return None, str(error)
    if not isinstance(value, dict):
        return None, f"{document['id']}: inspect.json is not an object"
    expected_sha = document.get("expected_sha256")
    observed_sha = value.get("source", {}).get("sha256") if isinstance(value.get("source"), dict) else None
    if expected_sha is None:
        return None, f"{document['id']}: source is not hash-pinned"
    if observed_sha != expected_sha:
        return None, f"{document['id']}: inspect source hash does not match corpus"
    if not isinstance(value.get("page_count"), int) or value["page_count"] < 1:
        return None, f"{document['id']}: inspect page_count is invalid"
    return value, None


def build_bootstrap_candidates(
    corpus: dict[str, Any],
    existing: list[dict[str, Any]],
    *,
    data_dir: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    occupied = {
        (item["document_id"], item["source_sha256"], item["page"])
        for item in existing
    }
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    for document in corpus["documents"]:
        inspect, warning = _load_inspect_for_bootstrap(document, data_dir)
        if warning:
            warnings.append(warning)
            continue
        assert inspect is not None
        source_sha = document["expected_sha256"]
        page_count = inspect["page_count"]
        expected_kinds = set(document.get("expected_front_regions") or REGION_KINDS)
        proposals: dict[int, tuple[int, str, str]] = {}

        def propose(page: Any, kind: str, reviewer: str, priority: int) -> None:
            if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= page_count:
                return
            if kind not in expected_kinds:
                return
            current = proposals.get(page)
            if current is None or priority > current[0]:
                proposals[page] = (priority, kind, reviewer)

        for page in inspect.get("toc_candidate_pages") or []:
            propose(page, "contents", "auto:inspect-toc-v1", 90)

        body_seen = False
        outline = inspect.get("outline")
        if isinstance(outline, list):
            for entry in outline:
                if not isinstance(entry, dict):
                    continue
                kind = _outline_kind(entry.get("title"), entry.get("depth"))
                if kind == "body_start":
                    if body_seen:
                        continue
                    body_seen = True
                if kind is not None:
                    propose(
                        entry.get("pdf_page"),
                        kind,
                        "auto:inspect-outline-v1",
                        100 if kind != "body_start" else 80,
                    )

        for page, (_, kind, reviewer) in sorted(proposals.items()):
            key = (document["id"], source_sha, page)
            if key in occupied:
                continue
            occupied.add(key)
            candidates.append({
                "schema": ANNOTATION_SCHEMA,
                "document_id": document["id"],
                "source_sha256": source_sha,
                "page": page,
                "kind": kind,
                "status": "needs_review",
                "reviewer": reviewer,
            })
    candidates.sort(key=lambda item: (item["document_id"], item["page"], item["kind"]))
    validate_annotations(candidates, corpus, data_dir=data_dir)
    return candidates, warnings


def export_review_queue(
    records: list[dict[str, Any]],
    corpus: dict[str, Any],
    *,
    data_dir: Path,
) -> dict[str, Any]:
    documents = _corpus_index(corpus)
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for record in sorted(records, key=lambda item: (item["document_id"], item["page"])):
        if record["status"] != "needs_review":
            continue
        key = (record["document_id"], record["source_sha256"], record["page"])
        if key in seen:
            continue
        seen.add(key)
        document = documents[record["document_id"]]
        local_path = document["local_path"].replace("\\", "/")
        item = dict(record)
        item.update({
            "title": document.get("title", ""),
            "language": document.get("language", ""),
            "document_type": document.get("document_type", ""),
            "local_pdf": local_path,
            "inspect_json": local_path + "2md/raw/inspect.json",
        })
        items.append(item)
    return {"schema": REVIEW_SCHEMA, "items": items}


def validate_command(args: argparse.Namespace) -> int:
    load_sources(args.sources)
    annotations = load_annotations(args.annotations, corpus_path=args.corpus)
    navigation = load_navigation_annotations(
        args.navigation_annotations, corpus_path=args.corpus
    )
    print(
        f"valid: {len(annotations)} primary annotations, "
        f"{len(navigation)} navigation annotations, "
        f"{len(load_sources(args.sources)['datasets'])} sources"
    )
    return 0


def list_command(args: argparse.Namespace) -> int:
    sources = load_sources(args.sources)["datasets"]
    annotations = load_annotations(args.annotations, corpus_path=args.corpus)
    navigation = load_navigation_annotations(
        args.navigation_annotations, corpus_path=args.corpus
    )
    counts = Counter(item["status"] for item in annotations)
    navigation_counts = Counter(item["status"] for item in navigation)
    print("Public sources (manual download only):")
    for item in sources:
        print(f"  {item['id']}: {item['training_use']} | {item['license']}")
    print(f"Annotations: {len(annotations)} total, "
          f"{counts['verified']} verified, {counts['needs_review']} needs_review")
    print(f"Navigation annotations: {len(navigation)} total, "
          f"{navigation_counts['verified']} verified, "
          f"{navigation_counts['needs_review']} needs_review")
    return 0


def bootstrap_command(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    existing = load_annotations(args.annotations, corpus_path=args.corpus)
    candidates, warnings = build_bootstrap_candidates(
        corpus, existing, data_dir=args.corpus.parent
    )
    _write_jsonl(args.output, candidates)
    print(f"wrote {len(candidates)} needs_review candidates to {args.output}")
    if warnings:
        print(f"skipped {len(warnings)} sources without usable pinned inspect metadata")
    return 0


def export_review_command(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    committed = load_annotations(args.annotations, corpus_path=args.corpus)
    if args.candidates.is_file():
        candidates = validate_annotations(
            _read_jsonl(args.candidates), corpus, data_dir=args.corpus.parent
        )
    else:
        candidates, _ = build_bootstrap_candidates(
            corpus, committed, data_dir=args.corpus.parent
        )
    queue = export_review_queue(
        committed + candidates, corpus, data_dir=args.corpus.parent
    )
    _atomic_write(args.output, json.dumps(queue, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {len(queue['items'])} review items to {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage offline PDF2MD front-page training metadata"
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument(
        "--navigation-annotations",
        type=Path,
        default=DEFAULT_NAVIGATION_ANNOTATIONS,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate sources and page labels")
    commands.add_parser("list", help="list source policies and label counts")
    bootstrap = commands.add_parser(
        "bootstrap", help="create local needs_review candidates from inspect/outline hints"
    )
    bootstrap.add_argument("--output", type=Path, default=DEFAULT_CANDIDATES)
    review = commands.add_parser(
        "export-review", help="export a local metadata-only review queue"
    )
    review.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    review.add_argument("--output", type=Path, default=DEFAULT_REVIEW)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            return validate_command(args)
        if args.command == "list":
            return list_command(args)
        if args.command == "bootstrap":
            return bootstrap_command(args)
        if args.command == "export-review":
            return export_review_command(args)
    except TrainingDataError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
