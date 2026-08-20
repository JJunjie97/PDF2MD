#!/usr/bin/env python3
"""Build a deterministic metadata-only review queue from v2 reports.

This command never opens PDFs. Report blocks and navigation payloads may contain
document text, so they are validated only as containers and never copied out.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from typing import Any, Iterable, Mapping


REPORT_SCHEMA = "pdf2md.front-regions.v2"
QUEUE_SCHEMA = "pdf2md.front-review-queue.v3"
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_REPORTS = 4096
MAX_PAGES = 4096
MAX_CANDIDATES = 10
DEFAULT_THRESHOLD = 0.20
DEFAULT_LIMIT = 500

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")
KINDS = {
    "cover", "legal", "revision_history", "preface", "abstract",
    "acknowledgements", "contents", "list_of_figures", "list_of_tables",
    "abbreviations", "nomenclature", "body_start", "other_front",
}
NAVIGATION_KINDS = {"contents", "list_of_figures", "list_of_tables"}
LEADER_CHARACTERS = frozenset(".．·•…⋯")
TERMINAL_PAGE_RE = re.compile(
    r"(?:^|\s)(?:[ivxlcdm]+|\d+)(?:\s*[-–—]\s*(?:[ivxlcdm]+|\d+))?\s*$",
    re.IGNORECASE,
)
LEADER_RUN_RE = re.compile(r"(?:\s*[.．·•…⋯]\s*){3,}")
NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
LONG_NAVIGATION_ENTRY_CHARS = 240
LEADER_DEBRIS_MIN_CHARS = 160
LEADER_DEBRIS_MIN_MARKS = 20
MIN_DUPLICATE_ENTRIES = 3
MIN_DUPLICATE_RUN_ENTRIES = 4
MIN_RESIDUAL_ENTRIES = 3
REPORT_FIELDS = {
    "schema", "classifier", "inputs", "processing", "stop_reason",
    "limited_by_max_pages", "stopped_at_body", "pages", "warnings",
}
CLASSIFIER_REQUIRED = {
    "version", "fingerprint", "model_fingerprints", "thresholds", "margins",
}
CLASSIFIER_OPTIONAL = {"rules_version", "features_version"}
INPUT_REQUIRED = {
    "content_list_sha256", "start_page", "max_pages", "input_page_count",
    "selected_page_count",
}
INPUT_OPTIONAL = {"source_sha256"}
PAGE_FIELDS = {
    "page", "kind", "accepted", "decision_source", "calibrated_probability",
    "rule_strength", "top_candidates", "evidence", "blocks",
}


class QueueError(ValueError):
    """A report or argument violates the queue contract."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise QueueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _exact_fields(value: Mapping[str, Any], required: set[str],
                  optional: set[str], label: str) -> None:
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing or unknown:
        raise QueueError(
            f"{label}: invalid fields; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )


def _probability(value: Any, *, optional: bool = False) -> float | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QueueError("probability must be a finite number in [0, 1]")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise QueueError("probability must be a finite number in [0, 1]")
    return result


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise QueueError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _input_path(value: str | Path) -> Path:
    path = Path(value)
    if path.suffix.lower() != ".json":
        raise QueueError(f"report must end in .json: {path}")
    if path.is_symlink():
        raise QueueError(f"symbolic-link reports are not allowed: {path}")
    try:
        path = path.resolve(strict=True)
        size = path.stat().st_size
    except OSError as error:
        raise QueueError(f"cannot resolve report: {path}") from error
    if not path.is_file():
        raise QueueError(f"report is not a regular file: {path}")
    if not 0 < size <= MAX_REPORT_BYTES:
        raise QueueError(
            f"report size must be 1..{MAX_REPORT_BYTES} bytes: {path}"
        )
    return path


def _read_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object
        )
    except QueueError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QueueError(f"invalid JSON report {path}: {error}") from error
    if not isinstance(value, dict):
        raise QueueError(f"report must be an object: {path}")
    return value


def _validate_report(report: dict[str, Any], path: Path) -> None:
    _exact_fields(report, REPORT_FIELDS, set(), str(path))
    if report.get("schema") != REPORT_SCHEMA:
        raise QueueError(f"{path}: schema must be {REPORT_SCHEMA}")
    classifier, inputs, pages = (
        report.get("classifier"), report.get("inputs"), report.get("pages")
    )
    if not isinstance(classifier, dict) or not isinstance(inputs, dict):
        raise QueueError(f"{path}: classifier and inputs must be objects")
    _exact_fields(
        classifier, CLASSIFIER_REQUIRED, CLASSIFIER_OPTIONAL,
        f"{path}:classifier",
    )
    _exact_fields(inputs, INPUT_REQUIRED, INPUT_OPTIONAL, f"{path}:inputs")
    _sha256(classifier.get("fingerprint"), f"{path}:classifier.fingerprint")
    _sha256(
        inputs.get("content_list_sha256"),
        f"{path}:inputs.content_list_sha256",
    )
    if "source_sha256" in inputs:
        _sha256(inputs["source_sha256"], f"{path}:inputs.source_sha256")
    if not isinstance(pages, list) or len(pages) > MAX_PAGES:
        raise QueueError(f"{path}: pages must have at most {MAX_PAGES} items")
    seen: set[int] = set()
    for index, page in enumerate(pages):
        label = f"{path}:pages[{index}]"
        if not isinstance(page, dict):
            raise QueueError(f"{label} must be an object")
        _exact_fields(page, PAGE_FIELDS, set(), label)
        number = page.get("page")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise QueueError(f"{label}.page must be a positive integer")
        if number in seen:
            raise QueueError(f"{path}: duplicate page {number}")
        seen.add(number)
        if not isinstance(page.get("accepted"), bool):
            raise QueueError(f"{label}.accepted must be boolean")
        if page.get("decision_source") not in {"rule", "layout", "text", "abstain"}:
            raise QueueError(f"{label}.decision_source is invalid")
        if page.get("kind") is not None and page["kind"] not in KINDS:
            raise QueueError(f"{label}.kind is invalid")
        _probability(page.get("calibrated_probability"), optional=True)
        _probability(page.get("rule_strength"), optional=True)
        candidates = page.get("top_candidates")
        if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
            raise QueueError(
                f"{label}.top_candidates must have at most "
                f"{MAX_CANDIDATES} items"
            )
        for offset, candidate in enumerate(candidates):
            candidate_label = f"{label}.top_candidates[{offset}]"
            if not isinstance(candidate, dict):
                raise QueueError(f"{candidate_label} must be an object")
            keys = set(candidate)
            if (
                not {"kind", "source"} <= keys
                or not keys <= {"kind", "source", "probability", "strength"}
                or (("probability" in keys) == ("strength" in keys))
            ):
                raise QueueError(f"{candidate_label} has invalid fields")
            if candidate["kind"] not in KINDS:
                raise QueueError(f"{candidate_label}.kind is invalid")
            if candidate["source"] not in {"rule", "layout", "text"}:
                raise QueueError(f"{candidate_label}.source is invalid")
            _probability(
                candidate.get("probability", candidate.get("strength"))
            )
        if (
            not isinstance(page.get("evidence"), dict)
            or not isinstance(page.get("blocks"), list)
        ):
            raise QueueError(
                f"{label}.evidence must be an object and blocks a list"
            )


def _document_id(path: Path) -> str:
    value = path.stem
    for part in reversed(path.parts[:-1]):
        if part.lower().endswith(".pdf2md"):
            value = part[:-7]
            break
    value = unicodedata.normalize("NFC", value.strip())
    if (
        not value or len(value) > 240
        or any(ord(character) < 32 for character in value)
    ):
        raise QueueError(f"cannot derive a safe document id from {path}")
    return value


def _portable_report_path(path: Path) -> str:
    """Return a stable locator without serializing a machine-local prefix."""
    for index, part in enumerate(path.parts[:-1]):
        if part.lower().endswith(".pdf2md"):
            return Path(*path.parts[index:]).as_posix()
    return path.name


def _score(candidate: Mapping[str, Any]) -> float:
    return float(candidate.get("probability", candidate.get("strength", 0.0)))


def _stage_candidates(page: Mapping[str, Any]) -> list[dict[str, Any]]:
    grouped = {
        source: [
            dict(item) for item in page["top_candidates"]
            if item["source"] == source
        ]
        for source in ("rule", "layout", "text")
    }
    source = page["decision_source"]
    if source in ("layout", "text") and grouped[source]:
        selected = grouped[source]
    elif grouped["text"]:
        selected = grouped["text"]
    elif grouped["layout"]:
        selected = grouped["layout"]
    else:
        selected = grouped["rule"]
    return sorted(
        selected,
        key=lambda item: (-_score(item), item["kind"], item["source"]),
    )


def _margin(candidates: list[dict[str, Any]]) -> float | None:
    if not candidates:
        return None
    runner_up = _score(candidates[1]) if len(candidates) > 1 else 0.0
    return round(max(0.0, _score(candidates[0]) - runner_up), 6)


def _entropy(candidates: list[dict[str, Any]]) -> float:
    values = [_score(item) for item in candidates if _score(item) > 0.0]
    total = sum(values)
    if len(values) < 2 or total <= 0.0:
        return 0.0
    values = [value / total for value in values]
    result = -sum(value * math.log(value) for value in values)
    return round(result / math.log(len(values)), 6)


def _reason(value: Any) -> str | None:
    return value if isinstance(value, str) and TOKEN_RE.fullmatch(value) else None


def _nested_lines(value: Any) -> list[list[str]]:
    """Return bounded-shape string blocks without retaining them in output."""
    if not isinstance(value, list):
        return []
    blocks: list[list[str]] = []
    for block in value:
        if not isinstance(block, list):
            continue
        lines = [line for line in block if isinstance(line, str) and line]
        if lines:
            blocks.append(lines)
    return blocks


def _navigation_blocks(page: Mapping[str, Any]) -> list[list[str]]:
    kind = page.get("kind")
    evidence = page.get("evidence")
    if kind not in NAVIGATION_KINDS or not isinstance(evidence, Mapping):
        return []
    mapping = evidence.get("navigation_blocks")
    if not isinstance(mapping, Mapping):
        return []
    return _nested_lines(mapping.get(kind))


def _line_key(value: str) -> str:
    # Keys are used only in memory for structural comparisons and never leave
    # the process.  Truncation bounds work on adversarial but valid reports.
    value = unicodedata.normalize("NFKC", value[:8192]).casefold().strip()
    value = LEADER_RUN_RE.sub(" ", value)
    value = TERMINAL_PAGE_RE.sub("", value)
    return NON_WORD_RE.sub("", value)


def _page_bearing(value: str) -> bool:
    return bool(TERMINAL_PAGE_RE.search(
        unicodedata.normalize("NFKC", value[:8192]).strip()
    ))


def _covered(key: str, exported: set[str]) -> bool:
    if not key:
        return True
    if key in exported:
        return True
    # Compact previews can truncate a full navigation line.  Treat a
    # substantial prefix as covered so truncation cannot create false residue.
    return any(
        min(len(key), len(other)) >= 12
        and (key.startswith(other) or other.startswith(key))
        for other in exported
    )


def _empty_structure() -> dict[str, Any]:
    return {
        "anomalies": [],
        "metrics": {
            "navigation_block_count": 0,
            "navigation_entry_count": 0,
            "max_navigation_entry_chars": 0,
            "long_navigation_entry_count": 0,
            "leader_debris_entry_count": 0,
            "duplicate_navigation_entry_count": 0,
            "duplicate_navigation_run_count": 0,
            "residual_navigation_entry_count": 0,
            "separated_navigation_run_count": 0,
        },
    }


def _structure_analysis(report: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    """Compute metadata-only navigation anomalies for every report page."""
    pages = report.get("pages")
    if not isinstance(pages, list):
        return {}
    result: dict[int, dict[str, Any]] = {}
    runs: list[tuple[int, str, frozenset[str]]] = []
    kind_pages: dict[str, list[tuple[int, int]]] = defaultdict(list)

    for page in pages:
        if not isinstance(page, Mapping):
            continue
        number = page.get("page")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        structure = _empty_structure()
        result[number] = structure
        if page.get("accepted") is not True or page.get("kind") not in NAVIGATION_KINDS:
            continue

        blocks = _navigation_blocks(page)
        lines = [line for block in blocks for line in block]
        keys = [_line_key(line) for line in lines]
        useful_keys = [key for key in keys if len(key) >= 3]
        counts = Counter(useful_keys)
        duplicate_entries = sum(count - 1 for count in counts.values() if count > 1)
        long_entries = sum(len(line) > LONG_NAVIGATION_ENTRY_CHARS for line in lines)
        leader_debris = sum(
            len(line) >= LEADER_DEBRIS_MIN_CHARS
            and sum(character in LEADER_CHARACTERS for character in line)
                >= LEADER_DEBRIS_MIN_MARKS
            and not _page_bearing(line)
            for line in lines
        )
        evidence = page.get("evidence")
        candidates = (
            _nested_lines(evidence.get("navigation_candidates"))
            if isinstance(evidence, Mapping) else []
        )
        exported = set(useful_keys)
        residual_keys = {
            key
            for block in candidates
            for line in block
            if _page_bearing(line)
            for key in [_line_key(line)]
            if len(key) >= 3 and not _covered(key, exported)
        }
        metrics = structure["metrics"]
        metrics.update({
            "navigation_block_count": len(blocks),
            "navigation_entry_count": len(lines),
            "max_navigation_entry_chars": max(map(len, lines), default=0),
            "long_navigation_entry_count": long_entries,
            "leader_debris_entry_count": leader_debris,
            "duplicate_navigation_entry_count": duplicate_entries,
            "residual_navigation_entry_count": len(residual_keys),
        })
        anomalies = structure["anomalies"]
        rule = evidence.get("rule", []) if isinstance(evidence, Mapping) else []
        if "unusable_navigation_debris" in rule or leader_debris:
            anomalies.append("navigation_leader_debris")
        if long_entries:
            anomalies.append("navigation_long_entry")
        if duplicate_entries >= MIN_DUPLICATE_ENTRIES:
            anomalies.append("duplicate_navigation_entries")
        if len(residual_keys) >= MIN_RESIDUAL_ENTRIES:
            anomalies.append("navigation_candidate_residue")
        stats = evidence.get("stats") if isinstance(evidence, Mapping) else None
        index_items = stats.get("index_items") if isinstance(stats, Mapping) else None
        if not lines or (
            isinstance(index_items, int) and not isinstance(index_items, bool)
            and index_items >= 6 and len(lines) > max(12, index_items * 3)
        ):
            anomalies.append("navigation_export_mismatch")
        for block in blocks:
            run_keys = frozenset(
                key for key in map(_line_key, block) if len(key) >= 3
            )
            if len(run_keys) >= MIN_DUPLICATE_RUN_ENTRIES:
                runs.append((number, str(page["kind"]), run_keys))
        kind_pages[str(page["kind"])].append((number, len(useful_keys)))

    # Repeated extracted runs, including repeats spread across adjacent pages.
    for offset, (left_page, left_kind, left) in enumerate(runs):
        for right_page, right_kind, right in runs[offset + 1:]:
            if left_kind != right_kind:
                continue
            overlap = len(left & right)
            union = len(left | right)
            if overlap < MIN_DUPLICATE_RUN_ENTRIES or overlap * 5 < union * 4:
                continue
            for number in {left_page, right_page}:
                structure = result[number]
                structure["metrics"]["duplicate_navigation_run_count"] += 1
                structure["anomalies"].append("duplicate_navigation_run")

    # Two sizeable runs of the same directory kind separated by another page
    # are structurally ambiguous (often bilingual or duplicated extraction) and
    # deserve review even if both were accepted at 0.98.
    for entries in kind_pages.values():
        groups: list[list[tuple[int, int]]] = []
        for entry in sorted(entries):
            if groups and entry[0] == groups[-1][-1][0] + 1:
                groups[-1].append(entry)
            else:
                groups.append([entry])
        substantial = [
            group for group in groups if sum(count for _, count in group) >= 5
        ]
        if len(substantial) < 2:
            continue
        for group in substantial:
            for number, _count in group:
                structure = result[number]
                structure["metrics"]["separated_navigation_run_count"] = len(substantial)
                structure["anomalies"].append("separated_navigation_runs")

    for structure in result.values():
        structure["anomalies"] = sorted(set(structure["anomalies"]))
    return result


def _evidence(page: Mapping[str, Any], entropy: float,
              structure: Mapping[str, Any]) -> dict[str, Any]:
    source = page["evidence"]
    reasons: set[str] = set()
    ood: list[str] = []
    reason = _reason(source.get("abstain_reason"))
    if reason:
        reasons.add(reason)
    for stage in ("layout", "text"):
        detail = source.get(stage)
        if not isinstance(detail, Mapping):
            continue
        if detail.get("ood") is True:
            ood.append(stage)
        reason = _reason(detail.get("reason"))
        if reason:
            reasons.add(reason)
    conflict = next(
        (value for value in sorted(reasons) if "conflict" in value), None
    )
    return {
        "accepted_false": page["accepted"] is False,
        "abstain": page["decision_source"] == "abstain",
        "ood": ood,
        "conflict": conflict,
        "entropy": entropy,
        "reasons": sorted(reasons),
        "structure_anomalies": list(structure.get("anomalies", [])),
        "structure_metrics": dict(structure.get("metrics", {})),
    }


def _item(report: Mapping[str, Any], page: Mapping[str, Any],
          path: Path, structure: Mapping[str, Any]) -> dict[str, Any]:
    candidates = _stage_candidates(page)
    entropy = _entropy(candidates)
    probability = page["calibrated_probability"]
    if probability is None and candidates:
        probability = _score(candidates[0])
    kind = page["kind"] or (
        candidates[0]["kind"] if candidates else None
    )
    source_sha = report["inputs"].get("source_sha256")
    content_list_sha = report["inputs"]["content_list_sha256"]
    compact = [
        {
            "kind": candidate["kind"],
            "probability": round(_score(candidate), 6),
            "source": candidate["source"],
        }
        for candidate in candidates[:5]
    ]
    return {
        "document_id": _document_id(path),
        "content_list_sha256": content_list_sha,
        "source_sha256": source_sha,
        "page": page["page"],
        "kind": kind,
        "top_candidates": compact,
        "probability": (
            round(float(probability), 6) if probability is not None else None
        ),
        "margin": _margin(candidates),
        "evidence": _evidence(page, entropy, structure),
        "report_path": _portable_report_path(path),
        "fingerprint": report["classifier"]["fingerprint"],
    }


def _eligible(item: Mapping[str, Any], threshold: float) -> bool:
    evidence, margin = item["evidence"], item["margin"]
    return bool(
        evidence["accepted_false"] or evidence["abstain"]
        or evidence["ood"] or evidence["conflict"]
        or evidence["structure_anomalies"]
        or evidence["entropy"] >= 0.65
        or margin is None or margin <= threshold
    )


def _rank(item: Mapping[str, Any]) -> tuple[Any, ...]:
    evidence, margin = item["evidence"], item["margin"]
    return (
        0 if evidence["structure_anomalies"] else 1,
        0 if evidence["accepted_false"] else 1,
        0 if evidence["abstain"] else 1,
        margin if margin is not None else -1.0,
        -float(evidence["entropy"]),
        0 if evidence["ood"] else 1,
        0 if evidence["conflict"] else 1,
        item["source_sha256"] or "", item["content_list_sha256"],
        item["page"], item["fingerprint"],
        item["report_path"],
    )


def build_queue(report_paths: Iterable[str | Path], *,
                threshold: float = DEFAULT_THRESHOLD,
                limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or not 0.0 <= float(threshold) <= 1.0
    ):
        raise QueueError("threshold must be a number in [0, 1]")
    threshold = float(threshold)
    if (
        isinstance(limit, bool) or not isinstance(limit, int)
        or not 1 <= limit <= 100000
    ):
        raise QueueError("limit must be an integer in [1, 100000]")
    supplied = list(report_paths)
    if not supplied or len(supplied) > MAX_REPORTS:
        raise QueueError(f"supply 1..{MAX_REPORTS} reports")
    paths = sorted(
        {_input_path(path) for path in supplied},
        key=lambda path: path.as_posix(),
    )
    selected: dict[tuple[str, int], dict[str, Any]] = {}
    for path in paths:
        report = _read_report(path)
        _validate_report(report, path)
        structures = _structure_analysis(report)
        for page in report["pages"]:
            item = _item(
                report, page, path,
                structures.get(page["page"], _empty_structure()),
            )
            if not _eligible(item, threshold):
                continue
            key = (item["content_list_sha256"], item["page"])
            if key not in selected or _rank(item) < _rank(selected[key]):
                selected[key] = item
    items = sorted(selected.values(), key=_rank)[:limit]
    return {
        "schema": QUEUE_SCHEMA,
        "threshold": round(threshold, 6),
        "limit": limit,
        "count": len(items),
        "items": items,
    }


def _output_path(value: str | Path, inputs: Iterable[str | Path]) -> Path:
    path = Path(value)
    if path.suffix.lower() != ".json":
        raise QueueError("output path must end in .json")
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise QueueError("output must be a regular file or not exist")
    resolved = path.resolve(strict=False)
    if any(resolved == _input_path(item) for item in inputs):
        raise QueueError("output must not overwrite an input report")
    return resolved


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            prefix=path.name + ".", suffix=".tmp", dir=path.parent,
        ) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a metadata-only low-confidence front-region queue"
    )
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("front-review-queue.json")
    )
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--check", action="store_true",
        help="validate and build in memory without writing output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        queue = build_queue(
            args.reports, threshold=args.threshold, limit=args.limit
        )
        if args.check:
            print(f"valid: {queue['count']} review items")
            return 0
        output = _output_path(args.output, args.reports)
        _atomic_write(output, queue)
        print(f"wrote {queue['count']} review items to {output}")
        return 0
    except QueueError as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    sys.exit(main())
