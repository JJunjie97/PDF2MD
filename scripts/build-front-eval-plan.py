#!/usr/bin/env python3
"""Build a deterministic, metadata-only plan for front-matter evaluation.

The tool hashes PDFs and reads their existing ``raw/inspect.json`` sidecars. It
does not extract, read, or emit PDF text, and never starts the conversion runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "data" / "corpus.json"
DEFAULT_OUTPUT = ROOT / "data" / "front-eval-plan.json"
CHUNK_SIZE = 1024 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class PlanError(RuntimeError):
    """Raised when corpus or inspection metadata cannot produce a safe plan."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlanError(f"missing JSON file: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read JSON {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PlanError(f"cannot hash PDF {path}: {exc}") from exc
    return digest.hexdigest()


def _safe_pdf_path(data_dir: Path, item: dict[str, Any]) -> Path:
    document_id = item.get("id", "<unknown>")
    value = item.get("local_path")
    if not isinstance(value, str) or not value or value != value.strip():
        raise PlanError(f"{document_id}: invalid local_path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".pdf":
        raise PlanError(f"{document_id}: unsafe local_path")
    data_root = data_dir.resolve()
    target = (data_root / relative).resolve()
    try:
        target.relative_to(data_root)
    except ValueError as exc:
        raise PlanError(f"{document_id}: local_path escapes the manifest directory") from exc
    return target


def _inspect_path(pdf_path: Path) -> Path:
    return pdf_path.with_name(pdf_path.name + "2md") / "raw" / "inspect.json"


def _positive_int(value: Any, field: str, document_id: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PlanError(f"{document_id}: inspect {field} must be a positive integer")
    return value


def _inspect_metadata(path: Path, document_id: str) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict) or value.get("ok") is not True or value.get("command") != "inspect":
        raise PlanError(f"{document_id}: invalid inspect metadata: {path}")
    source = value.get("source")
    if not isinstance(source, dict):
        raise PlanError(f"{document_id}: inspect metadata has no source object")
    digest = source.get("sha256")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        raise PlanError(f"{document_id}: inspect source SHA-256 is invalid")
    page_count = _positive_int(value.get("page_count"), "page_count", document_id)
    pdf_kind = value.get("pdf_kind")
    if pdf_kind not in {"text", "scanned", "mixed"}:
        raise PlanError(f"{document_id}: inspect pdf_kind is invalid")
    candidates = value.get("toc_candidate_pages", [])
    if not isinstance(candidates, list) or any(
        not isinstance(page, int)
        or isinstance(page, bool)
        or page < 1
        or page > page_count
        for page in candidates
    ):
        raise PlanError(f"{document_id}: inspect toc_candidate_pages is invalid")
    if candidates != sorted(set(candidates)):
        raise PlanError(f"{document_id}: inspect toc_candidate_pages must be sorted and unique")
    return {
        "sha256": digest,
        "page_count": page_count,
        "pdf_kind": pdf_kind,
        "toc_candidate_pages": candidates,
    }


def _expected_navigation_regions(item: dict[str, Any]) -> list[str]:
    """Return manifest-declared navigation regions relevant to TOC truth.

    ``inspect`` is deliberately only a candidate detector. A missed candidate
    must therefore not override a manifest annotation and silently become a
    negative example.
    """
    document_id = item["id"]
    regions = item.get("expected_front_regions", [])
    if not isinstance(regions, list) or any(not isinstance(region, str) for region in regions):
        raise PlanError(f"{document_id}: expected_front_regions must be a list of strings")
    return sorted({
        region
        for region in regions
        if region == "contents" or region.startswith("list_")
    })


def _selection(
    metadata: dict[str, Any],
    expected_navigation_regions: Sequence[str],
) -> tuple[int, str, str, list[str]]:
    page_count = metadata["page_count"]
    candidates = metadata["toc_candidate_pages"]
    if candidates:
        end = min(max(candidates) + 8, 40, page_count)
        return (
            end,
            "toc-positive",
            "high",
            [
                "inspect metadata contains table-of-contents candidate pages",
                "selection includes the last candidate plus eight following pages, capped at page 40",
            ],
        )
    if expected_navigation_regions:
        end = min(24 if metadata["pdf_kind"] == "scanned" else 12, page_count)
        return (
            end,
            "toc-expected-undetected",
            "high",
            [
                "manifest expects navigation region(s): "
                + ", ".join(expected_navigation_regions),
                "inspect metadata contains no table-of-contents candidate page",
                "sample remains a hard positive and must not be counted as a negative",
            ],
        )
    if metadata["pdf_kind"] == "scanned":
        return (
            min(24, page_count),
            "scanned-front",
            "high",
            [
                "inspect metadata classifies the PDF as scanned",
                "selection covers up to 24 front pages for scanned-layout evaluation",
            ],
        )
    return (
        min(12, page_count),
        "toc-negative",
        "normal",
        [
            "inspect metadata contains no table-of-contents candidate page",
            "selection provides a bounded text or mixed-PDF negative example",
        ],
    )


def _load_documents(manifest_path: Path) -> list[dict[str, Any]]:
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise PlanError("manifest schema_version must be 1")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or any(not isinstance(item, dict) for item in documents):
        raise PlanError("manifest documents must be a list of objects")
    ids = [item.get("id") for item in documents]
    if any(not isinstance(document_id, str) or not document_id for document_id in ids):
        raise PlanError("every manifest document must have a non-empty id")
    if len(ids) != len(set(ids)):
        raise PlanError("manifest document ids must be unique")
    return documents


def _select_documents(
    documents: Sequence[dict[str, Any]], suites: Sequence[str], ids: Sequence[str]
) -> list[dict[str, Any]]:
    known_ids = {item["id"] for item in documents}
    known_suites = {item.get("suite") for item in documents if isinstance(item.get("suite"), str)}
    unknown_ids = sorted(set(ids) - known_ids)
    unknown_suites = sorted(set(suites) - known_suites)
    if unknown_ids:
        raise PlanError(f"unknown corpus id(s): {', '.join(unknown_ids)}")
    if unknown_suites:
        raise PlanError(f"unknown corpus suite(s): {', '.join(unknown_suites)}")
    if not suites and not ids:
        selected = list(documents)
    else:
        suite_set, id_set = set(suites), set(ids)
        selected = [
            item for item in documents
            if item.get("suite") in suite_set or item["id"] in id_set
        ]
    return sorted(selected, key=lambda item: item["id"])


def build_plan(
    manifest_path: Path = DEFAULT_MANIFEST,
    suites: Sequence[str] = (),
    ids: Sequence[str] = (),
    *,
    require_all: bool = False,
) -> dict[str, Any]:
    """Build a deterministic plan without writing it or reading PDF content."""
    manifest_path = manifest_path.resolve()
    documents = _select_documents(_load_documents(manifest_path), suites, ids)
    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for item in documents:
        document_id = item["id"]
        pdf_path = _safe_pdf_path(manifest_path.parent, item)
        if not pdf_path.is_file():
            if require_all:
                raise PlanError(f"{document_id}: missing local PDF: {pdf_path}")
            skipped.append({"id": document_id, "reason": "missing_local_pdf"})
            continue
        inspect_path = _inspect_path(pdf_path)
        if not inspect_path.is_file():
            if require_all:
                raise PlanError(f"{document_id}: missing inspect metadata: {inspect_path}")
            skipped.append({"id": document_id, "reason": "missing_inspect_metadata"})
            continue
        metadata = _inspect_metadata(inspect_path, document_id)
        actual_digest = _sha256(pdf_path)
        if actual_digest != metadata["sha256"]:
            raise PlanError(f"{document_id}: inspect source SHA-256 does not match the local PDF")
        expected_digest = item.get("expected_sha256")
        if expected_digest is not None:
            if not isinstance(expected_digest, str) or not SHA256_PATTERN.fullmatch(expected_digest):
                raise PlanError(f"{document_id}: manifest expected_sha256 is invalid")
            if actual_digest != expected_digest:
                raise PlanError(f"{document_id}: local PDF does not match the manifest-pinned SHA-256")
        expected_navigation_regions = _expected_navigation_regions(item)
        end, role, priority, reasons = _selection(metadata, expected_navigation_regions)
        planned.append({
            "id": document_id,
            "sha256": actual_digest,
            "page_count": metadata["page_count"],
            "pdf_kind": metadata["pdf_kind"],
            "toc_candidate_pages": metadata["toc_candidate_pages"],
            "selection": [{"start": 1, "end": end}],
            "role": role,
            "priority": priority,
            "reasons": reasons,
        })
    return {
        "schema": "pdf2md.front-eval-plan.v1",
        "page_numbering": "physical-pdf-1-based",
        "document_count": len(planned),
        "skipped_count": len(skipped),
        "documents": planned,
        "skipped": skipped,
    }


def _safe_output_path(output: Path, manifest_path: Path) -> Path:
    target = output.resolve()
    if target.suffix.lower() != ".json":
        raise PlanError("output path must end in .json")
    protected = {manifest_path.resolve()}
    data_dir = manifest_path.resolve().parent
    for item in _load_documents(manifest_path.resolve()):
        pdf = _safe_pdf_path(data_dir, item)
        protected.add(pdf)
        protected.add(_inspect_path(pdf).resolve())
    if target in protected:
        raise PlanError("output path conflicts with a corpus control or source file")
    if target.exists() and target.is_dir():
        raise PlanError("output path is a directory")
    return target


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="append", default=[], help="include a corpus suite (repeatable)")
    parser.add_argument("--id", action="append", default=[], dest="ids", help="include a corpus id (repeatable)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="output JSON path")
    parser.add_argument("--check", action="store_true", help="validate and build in memory without writing")
    parser.add_argument(
        "--require-all",
        action="store_true",
        help="fail when a selected corpus entry has no local PDF or inspect metadata",
    )
    return parser


def run(argv: Sequence[str] | None = None, *, manifest_path: Path = DEFAULT_MANIFEST) -> int:
    args = _parser().parse_args(argv)
    plan = build_plan(manifest_path, args.suite, args.ids, require_all=args.require_all)
    output = _safe_output_path(args.output, manifest_path)
    if args.check:
        print(
            f"valid: {plan['document_count']} document(s), "
            f"{plan['skipped_count']} skipped; no file written"
        )
    else:
        _write_json_atomic(output, plan)
        print(
            f"wrote {plan['document_count']} document(s), "
            f"{plan['skipped_count']} skipped to {output}"
        )
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except PlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
