from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pypdf import PdfReader


SCHEMA_VERSION = 1
TOOL_VERSION = "1.0.0"
DEFAULT_TOKEN_BUDGET = 12_000
DEFAULT_MAX_PAGES = 12


class AgentError(Exception):
    def __init__(self, code: str, message: str, exit_code: int, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.retryable = retryable


@dataclass(frozen=True)
class Runtime:
    root: Path
    executable: Path
    python: Path


def runtime() -> Runtime:
    script = Path(__file__).resolve()
    configured_root = os.getenv("MINERU_LOCAL_ROOT")
    candidates = ([Path(configured_root).expanduser().resolve()] if configured_root else []) + list(script.parents)
    root = next(
        (
            candidate
            for candidate in candidates
            if (candidate / ".conda-env" / "python.exe").is_file()
            and (candidate / "MinerU-Local.exe").is_file()
        ),
        script.parents[3],
    )
    return Runtime(
        root=root,
        executable=root / "MinerU-Local.exe",
        python=root / ".conda-env" / "python.exe",
    )


def emit(payload: dict[str, Any]) -> None:
    payload.setdefault("schema_version", SCHEMA_VERSION)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def resolve_pdf(value: str) -> Path:
    path = Path(value.strip().strip('"')).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise AgentError("INPUT_NOT_FOUND", f"PDF not found: {path}", 3)
    if path.suffix.lower() != ".pdf":
        raise AgentError("UNSUPPORTED_INPUT", f"Expected a PDF file: {path}", 3)
    return path


def output_dir_for(source: Path) -> Path:
    return source.parent / f"{source.stem}.mineru"


@dataclass(frozen=True)
class OutputLayout:
    root: Path
    document: Path
    images: Path
    raw: Path
    jobs: Path
    selections: Path
    logs: Path
    index: Path
    manifest: Path
    inspection: Path


def layout_for(source: Path) -> OutputLayout:
    root = output_dir_for(source)
    raw = root / "raw"
    return OutputLayout(
        root=root,
        document=root / f"{source.stem}.md",
        images=root / "images",
        raw=raw,
        jobs=raw / "jobs",
        selections=raw / "selections",
        logs=raw / "logs",
        index=raw / "index",
        manifest=raw / "manifest.json",
        inspection=raw / "inspect.json",
    )


def merge_directory(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if not destination.exists():
        source.replace(destination)
        return
    destination.mkdir(parents=True, exist_ok=True)
    for child in list(source.iterdir()):
        target = destination / child.name
        if child.is_dir() and target.is_dir():
            merge_directory(child, target)
        elif not target.exists():
            child.replace(target)
    try:
        source.rmdir()
    except OSError:
        pass


def ensure_layout(source: Path) -> OutputLayout:
    layout = layout_for(source)
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.raw.mkdir(parents=True, exist_ok=True)

    for name, destination in (
        ("inspect.json", layout.inspection),
        ("manifest.json", layout.manifest),
    ):
        legacy = layout.root / name
        if legacy.exists() and not destination.exists():
            legacy.replace(destination)
    for name, destination in (
        ("index", layout.index),
        ("logs", layout.logs),
        ("selections", layout.selections),
    ):
        legacy = layout.root / name
        if legacy.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            merge_directory(legacy, destination)

    layout.jobs.mkdir(parents=True, exist_ok=True)
    for child in list(layout.raw.iterdir()):
        if child.is_dir() and re.fullmatch(r"[0-9a-f]{16}", child.name):
            target = layout.jobs / child.name
            if target.exists():
                merge_directory(child, target)
            else:
                child.replace(target)

    for directory in (layout.images, layout.selections, layout.logs, layout.index):
        directory.mkdir(parents=True, exist_ok=True)

    if layout.manifest.exists():
        try:
            manifest = json.loads(layout.manifest.read_text(encoding="utf-8"))
            changed = False
            for item in manifest.get("selections", []):
                task_key = str(item.get("task_key", ""))
                markdown = Path(str(item.get("markdown", "")))
                raw_output = Path(str(item.get("raw_output", "")))
                log = Path(str(item.get("log", "")))
                replacements = {
                    "markdown": layout.selections / markdown.name,
                    "raw_output": layout.jobs / task_key,
                    "log": layout.logs / log.name,
                }
                for key, candidate in replacements.items():
                    if candidate.exists() and str(item.get(key, "")) != str(candidate):
                        item[key] = str(candidate)
                        changed = True
            if changed:
                temporary = layout.manifest.with_suffix(".tmp")
                temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(layout.manifest)
        except Exception:
            pass
    return layout


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(source: Path, include_hash: bool = True) -> dict[str, Any]:
    stat = source.stat()
    result: dict[str, Any] = {
        "path": str(source),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if include_hash:
        result["sha256"] = file_sha256(source)
    return result


def open_reader(source: Path) -> PdfReader:
    try:
        reader = PdfReader(str(source))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                raise AgentError("PDF_ENCRYPTED", "PDF requires a password.", 4)
        return reader
    except AgentError:
        raise
    except Exception as exc:
        raise AgentError("PDF_UNREADABLE", f"Cannot read PDF: {exc}", 4) from exc


def estimate_tokens(text: str) -> int:
    cjk = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
    latin = max(0, len(text) - cjk)
    return max(1, round((latin / 3.5 + cjk * 1.2) * 1.15))


def sample_page_indices(page_count: int, limit: int = 14) -> list[int]:
    if page_count <= limit:
        return list(range(page_count))
    values = set(range(min(10, page_count)))
    values.update({page_count - 1, page_count - 2})
    remaining = max(0, limit - len(values))
    for step in range(1, remaining + 1):
        values.add(round(step * (page_count - 1) / (remaining + 1)))
    return sorted(values)


def flatten_outline(reader: PdfReader, limit: int = 80) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def walk(items: Iterable[Any], depth: int = 0) -> None:
        for item in items:
            if len(result) >= limit:
                return
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            title = getattr(item, "title", None)
            if not title:
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:
                page = None
            result.append({"title": str(title), "pdf_page": page, "depth": depth})

    try:
        walk(reader.outline)
    except Exception:
        pass
    return result


def inspect_pdf(source: Path, save: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    reader = open_reader(source)
    count = len(reader.pages)
    samples: list[dict[str, Any]] = []
    toc_candidates: list[int] = []
    extracted_chars = 0
    extracted_tokens = 0
    sampled_pages = sample_page_indices(count)
    toc_pattern = re.compile(r"(?:^|\n)\s*(?:table\s+of\s+contents|contents|目\s*录)\s*(?:\n|$)", re.I)

    for index in sampled_pages:
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:
            text = ""
        chars = len(text.strip())
        extracted_chars += chars
        extracted_tokens += estimate_tokens(text)
        samples.append({"pdf_page": index + 1, "characters": chars})
        if index < min(count, 20) and toc_pattern.search(text):
            toc_candidates.append(index + 1)

    average = extracted_chars / max(1, len(sampled_pages))
    nonempty = sum(1 for item in samples if item["characters"] >= 40)
    if average < 20 and nonempty == 0:
        pdf_kind = "scanned"
    elif nonempty >= max(1, round(len(samples) * 0.7)):
        pdf_kind = "text"
    else:
        pdf_kind = "mixed"

    outline = flatten_outline(reader)
    metadata: dict[str, str] = {}
    try:
        for key, value in (reader.metadata or {}).items():
            if value is not None:
                metadata[str(key).lstrip("/")] = str(value)
    except Exception:
        pass

    estimated_full_characters = round(average * count)
    estimated_full_tokens = max(1, round(extracted_tokens / max(1, len(sampled_pages)) * count))
    source_info = fingerprint(source)
    payload: dict[str, Any] = {
        "ok": True,
        "command": "inspect",
        "tool_version": TOOL_VERSION,
        "source": source_info,
        "output_dir": str(output_dir_for(source)),
        "page_count": count,
        "pdf_kind": pdf_kind,
        "metadata": metadata,
        "outline": outline,
        "toc_candidate_pages": sorted(set(toc_candidates)),
        "sampled_pages": samples,
        "estimated_full_characters": estimated_full_characters,
        "estimated_full_tokens": estimated_full_tokens,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    if save:
        layout = ensure_layout(source)
        target = layout.inspection
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    return payload


def parse_pages(expression: str, page_count: int) -> list[int]:
    pages: set[int] = set()
    for part in expression.replace(" ", "").split(","):
        if not part:
            raise AgentError("INVALID_PAGES", f"Invalid page expression: {expression}", 2)
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise AgentError("INVALID_PAGES", f"Invalid page expression: {expression}", 2)
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > page_count:
            raise AgentError("INVALID_PAGES", f"Pages must be within 1-{page_count}: {part}", 2)
        pages.update(range(start, end + 1))
    return sorted(pages)


def merge_ranges(pages: Iterable[int]) -> list[tuple[int, int]]:
    values = sorted(set(pages))
    if not values:
        return []
    result: list[tuple[int, int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        result.append((start, previous))
        start = previous = value
    result.append((start, previous))
    return result


def ranges_expression(ranges: Iterable[tuple[int, int]]) -> str:
    return ",".join(str(start) if start == end else f"{start}-{end}" for start, end in ranges)


def index_paths(source: Path) -> tuple[Path, Path]:
    directory = ensure_layout(source).index
    return directory / "native-text.jsonl", directory / "meta.json"


def load_index(source: Path) -> list[dict[str, Any]] | None:
    index_path, meta_path = index_paths(source)
    if not index_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        stat = source.stat()
        if meta.get("size") != stat.st_size or meta.get("mtime_ns") != stat.st_mtime_ns:
            return None
        records: list[dict[str, Any]] = []
        with index_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    records.append(json.loads(line))
        return records
    except Exception:
        return None


def build_index(source: Path) -> tuple[list[dict[str, Any]], bool]:
    cached = load_index(source)
    if cached is not None:
        return cached, True

    reader = open_reader(source)
    records: list[dict[str, Any]] = []
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        cleaned = re.sub(r"[ \t]+", " ", text).strip()
        records.append(
            {
                "pdf_page": index + 1,
                "characters": len(cleaned),
                "estimated_tokens": estimate_tokens(cleaned),
                "text": cleaned,
            }
        )

    index_path, meta_path = index_paths(source)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_tmp = index_path.with_suffix(".tmp")
    meta_tmp = meta_path.with_suffix(".tmp")
    with index_tmp.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    stat = source.stat()
    meta_tmp.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "source": str(source),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": file_sha256(source),
                "page_count": len(records),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    index_tmp.replace(index_path)
    meta_tmp.replace(meta_path)
    return records, False


def query_terms(query: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", query.strip().lower())
    terms = [normalized] if normalized else []
    terms.extend(token for token in re.findall(r"[a-z0-9_.+/-]{2,}", normalized) if token not in terms)
    terms.extend(token for token in re.findall(r"[\u3400-\u9fff]{2,}", normalized) if token not in terms)
    return terms


def make_snippet(text: str, terms: list[str], limit: int = 260) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    lower = compact.lower()
    positions = [lower.find(term) for term in terms if lower.find(term) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - limit // 3)
    end = min(len(compact), start + limit)
    snippet = compact[start:end]
    if start:
        snippet = "…" + snippet
    if end < len(compact):
        snippet += "…"
    return snippet


def search_pdf(source: Path, query: str, top_k: int = 8) -> dict[str, Any]:
    started = time.monotonic()
    terms = query_terms(query)
    if not terms:
        raise AgentError("EMPTY_QUERY", "Search query is empty.", 2)
    records, cache_hit = build_index(source)
    matches: list[dict[str, Any]] = []
    full_query = terms[0]
    for record in records:
        text = str(record.get("text", ""))
        lower = text.lower()
        if not lower:
            continue
        score = lower.count(full_query) * 8
        matched_terms: list[str] = []
        for term in terms[1:] if len(terms) > 1 else terms:
            count = lower.count(term)
            if count:
                score += min(count, 5)
                matched_terms.append(term)
        if full_query in lower and full_query not in matched_terms:
            matched_terms.insert(0, full_query)
        if score <= 0:
            continue
        matches.append(
            {
                "pdf_page": record["pdf_page"],
                "score": score,
                "matched_terms": matched_terms[:8],
                "snippet": make_snippet(text, terms),
                "page_estimated_tokens": record.get("estimated_tokens", 0),
            }
        )
    matches.sort(key=lambda item: (-int(item["score"]), int(item["pdf_page"])))
    return {
        "ok": True,
        "command": "search",
        "tool_version": TOOL_VERSION,
        "source": str(source),
        "output_dir": str(output_dir_for(source)),
        "query": query,
        "index_cache": "hit" if cache_hit else "created",
        "page_count": len(records),
        "matches": matches[: max(1, top_k)],
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def read_manifest(source: Path) -> dict[str, Any]:
    path = ensure_layout(source).manifest
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "source": fingerprint(source), "selections": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "source": fingerprint(source), "selections": []}
    current = fingerprint(source)
    recorded = value.get("source", {})
    if recorded.get("sha256") != current.get("sha256"):
        return {"schema_version": SCHEMA_VERSION, "source": current, "selections": []}
    value.setdefault("selections", [])
    return value


def write_manifest(source: Path, manifest: dict[str, Any]) -> Path:
    target = ensure_layout(source).manifest
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def profile_args(profile: str) -> list[str]:
    if profile == "accurate":
        return ["-b", "hybrid-engine", "--effort", "high"]
    if profile == "pipeline":
        return ["-b", "pipeline", "--effort", "medium", "--no-image-analysis"]
    return ["-b", "hybrid-engine", "--effort", "medium", "--no-image-analysis"]


def terminate_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()


def add_provenance_header(markdown: Path, source: Path, page_range: str, profile: str) -> None:
    content = markdown.read_text(encoding="utf-8", errors="replace")
    if content.startswith("<!-- mineru-agent:"):
        return
    header = (
        f'<!-- mineru-agent: source="{source.name}"; pdf_pages="{page_range}"; '
        f'profile="{profile}" -->\n\n'
    )
    markdown.write_text(header + content, encoding="utf-8")


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"}


def collect_images(raw_output: Path, images_dir: Path) -> dict[str, str]:
    mappings: dict[str, str] = {}
    if not raw_output.exists():
        return mappings
    images_dir.mkdir(parents=True, exist_ok=True)
    for image in raw_output.rglob("*"):
        if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        digest = file_sha256(image)[:16]
        destination = images_dir / f"{digest}{image.suffix.lower()}"
        if not destination.exists():
            shutil.copy2(image, destination)
        published = f"images/{destination.name}"
        relative = image.relative_to(raw_output).as_posix()
        candidates = {relative, image.name}
        parts = relative.split("/")
        lower_parts = [part.lower() for part in parts]
        if "images" in lower_parts:
            candidates.add("/".join(parts[lower_parts.index("images") :]))
        for candidate in candidates:
            mappings[candidate.replace("\\", "/").lower()] = published
    return mappings


def rewrite_image_links(content: str, mappings: dict[str, str]) -> str:
    if not mappings:
        return content

    def published_path(value: str) -> str | None:
        normalized = value.strip().strip("<>").replace("\\", "/")
        direct = mappings.get(normalized.lower())
        if direct:
            return direct
        return mappings.get(Path(normalized).name.lower())

    def replace_markdown(match: re.Match[str]) -> str:
        value = match.group(2).strip()
        if value.startswith("<") and ">" in value:
            end = value.index(">") + 1
            path_part, suffix = value[:end], value[end:]
        else:
            parts = re.split(r"(\s+[\"'].*)$", value, maxsplit=1)
            path_part = parts[0]
            suffix = parts[1] if len(parts) > 1 else ""
        replacement = published_path(path_part)
        if not replacement:
            return match.group(0)
        return f"{match.group(1)}{replacement}{suffix}{match.group(3)}"

    def replace_html(match: re.Match[str]) -> str:
        replacement = published_path(match.group(2))
        if not replacement:
            return match.group(0)
        return f"{match.group(1)}{replacement}{match.group(3)}"

    content = re.sub(r"(!\[[^\]]*\]\()([^)]+)(\))", replace_markdown, content)
    return re.sub(r"(<img\b[^>]*?\bsrc=[\"'])([^\"']+)([\"'])", replace_html, content, flags=re.I)


def publish_document(source: Path, selections: list[dict[str, Any]]) -> Path:
    layout = ensure_layout(source)
    pieces = [
        f'<!-- mineru-agent-document: source="{source.name}"; '
        f'selected_pdf_pages="{",".join(str(item.get("pdf_pages", "")) for item in selections)}" -->'
    ]
    multiple = len(selections) > 1
    for item in selections:
        markdown = Path(str(item["markdown"]))
        raw_output = Path(str(item["raw_output"]))
        if not markdown.exists():
            raise AgentError("MARKDOWN_NOT_FOUND", f"Cached Markdown is missing: {markdown}", 6, True)
        content = markdown.read_text(encoding="utf-8", errors="replace").strip()
        mappings = collect_images(raw_output, layout.images)
        content = rewrite_image_links(content, mappings)
        if multiple:
            pieces.append(f"## Source PDF pages {item.get('pdf_pages', '')}\n\n{content}")
        else:
            pieces.append(content)
    temporary = layout.document.with_suffix(".tmp")
    temporary.write_text("\n\n".join(pieces).rstrip() + "\n", encoding="utf-8")
    temporary.replace(layout.document)
    return layout.document


def convert_pdf(
    source: Path,
    pages_expression: str | None,
    profile: str = "balanced",
    force: bool = False,
    timeout: int = 1800,
) -> dict[str, Any]:
    started = time.monotonic()
    rt = runtime()
    if not rt.executable.exists() or not rt.python.exists():
        raise AgentError("RUNTIME_MISSING", f"MinerU runtime is incomplete under {rt.root}", 5)

    reader = open_reader(source)
    page_count = len(reader.pages)
    if pages_expression:
        pages = parse_pages(pages_expression, page_count)
        ranges = merge_ranges(pages)
        full_document = len(pages) == page_count
    else:
        pages = list(range(1, page_count + 1))
        ranges = [(1, page_count)]
        full_document = True

    layout = ensure_layout(source)
    output = layout.root
    manifest = read_manifest(source)
    source_hash = str(manifest["source"]["sha256"])
    completed: list[dict[str, Any]] = []

    invocation_ranges: list[tuple[int, int] | None]
    if full_document:
        invocation_ranges = [None]
    else:
        invocation_ranges = list(ranges)

    for selected_range in invocation_ranges:
        if selected_range is None:
            range_text = f"1-{page_count}"
            range_slug = "full"
        else:
            start, end = selected_range
            range_text = str(start) if start == end else f"{start}-{end}"
            range_slug = f"page-{start:04d}" if start == end else f"pages-{start:04d}-{end:04d}"
        key_material = f"{source_hash}|{range_text}|{profile}|{TOOL_VERSION}"
        task_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:16]
        selection_md = layout.selections / f"{range_slug}-{profile}.md"
        cached = next(
            (
                item
                for item in manifest.get("selections", [])
                if item.get("task_key") == task_key and Path(str(item.get("markdown", ""))).exists()
            ),
            None,
        )
        if cached and not force:
            cached_result = dict(cached)
            cached_result["cache"] = "hit"
            completed.append(cached_result)
            continue

        raw_dir = layout.jobs / task_key
        raw_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(rt.executable),
            str(source),
            "-o",
            str(raw_dir),
            *profile_args(profile),
            "--md-output",
            str(selection_md),
        ]
        if selected_range is not None:
            start, end = selected_range
            if start == end:
                command.extend(["--page", str(start)])
            else:
                command.extend(["--pages", f"{start}-{end}"])

        print(f"[mineru-read-pdf] converting PDF pages {range_text} ({profile})", file=sys.stderr)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            cwd=str(rt.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        try:
            process_output, _ = process.communicate(timeout=max(1, timeout))
        except subprocess.TimeoutExpired as exc:
            terminate_tree(process)
            process_output, _ = process.communicate()
            log_path = layout.logs / f"{task_key}.log"
            log_path.write_text(process_output or "", encoding="utf-8")
            raise AgentError("CONVERSION_TIMEOUT", f"MinerU timed out after {timeout} seconds.", 7, True) from exc

        log_path = layout.logs / f"{task_key}.log"
        log_path.write_text(process_output or "", encoding="utf-8")
        if process.returncode != 0:
            tail = "\n".join((process_output or "").splitlines()[-12:])
            raise AgentError("CONVERSION_FAILED", f"MinerU failed for pages {range_text}.\n{tail}", 6, True)

        if not selection_md.exists():
            candidates = sorted(raw_dir.rglob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
            if not candidates:
                raise AgentError("MARKDOWN_NOT_FOUND", f"MinerU produced no Markdown for pages {range_text}.", 6, True)
            shutil.copy2(candidates[0], selection_md)
        add_provenance_header(selection_md, source, range_text, profile)
        item = {
            "task_key": task_key,
            "pdf_pages": range_text,
            "profile": profile,
            "markdown": str(selection_md),
            "raw_output": str(raw_dir),
            "log": str(log_path),
            "cache": "created",
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        manifest["selections"] = [
            existing for existing in manifest.get("selections", []) if existing.get("task_key") != task_key
        ] + [item]
        write_manifest(source, manifest)
        completed.append(item)

    write_manifest(source, manifest)
    published = publish_document(source, completed)
    return {
        "ok": True,
        "command": "convert",
        "tool_version": TOOL_VERSION,
        "source": str(source),
        "page_count": page_count,
        "selected_ranges": ranges_expression(ranges),
        "profile": profile,
        "output_dir": str(output),
        "markdown": str(published),
        "images_dir": str(layout.images),
        "cache": "hit" if completed and all(item.get("cache") == "hit" for item in completed) else "updated",
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def choose_pages(
    inspection: dict[str, Any],
    matches: list[dict[str, Any]],
    token_budget: int,
    context_pages: int,
    max_pages: int,
) -> tuple[list[int], str]:
    page_count = int(inspection["page_count"])
    estimated_total = max(1, int(inspection.get("estimated_full_tokens", 1)))
    per_page_tokens = max(250, estimated_total // max(1, page_count))
    allowed = max(1, min(max_pages, token_budget // per_page_tokens))

    if page_count <= 25 and estimated_total <= 30_000:
        return list(range(1, page_count + 1)), "short-document-full"

    selected: set[int] = set()
    cluster_count = 0
    for match in matches:
        page = int(match["pdf_page"])
        candidate = set(range(max(1, page - context_pages), min(page_count, page + context_pages) + 1))
        touches = any(abs(existing - page) <= context_pages + 1 for existing in selected)
        if not touches and cluster_count >= 3:
            continue
        if len(selected | candidate) > allowed:
            continue
        if not touches:
            cluster_count += 1
        selected.update(candidate)
        if len(selected) >= allowed:
            break
    if selected:
        return sorted(selected), "native-text-search"

    toc_pages = [int(page) for page in inspection.get("toc_candidate_pages", [])]
    if toc_pages:
        for page in toc_pages:
            selected.update(range(page, min(page_count, page + min(3, allowed - 1)) + 1))
            if len(selected) >= allowed:
                break
        return sorted(selected)[:allowed], "contents-first"

    fallback_count = min(allowed, 8 if inspection.get("pdf_kind") == "scanned" else 5)
    return list(range(1, fallback_count + 1)), "front-matter-fallback"


def prepare_pdf(
    source: Path,
    query: str,
    profile: str,
    token_budget: int,
    context_pages: int,
    max_pages: int,
    top_k: int,
    force: bool,
    timeout: int,
) -> dict[str, Any]:
    started = time.monotonic()
    inspection = inspect_pdf(source)
    search_result = search_pdf(source, query, top_k=top_k)
    selected, strategy = choose_pages(
        inspection,
        list(search_result.get("matches", [])),
        token_budget=max(1, token_budget),
        context_pages=max(0, context_pages),
        max_pages=max(1, max_pages),
    )
    all_pages = len(selected) == int(inspection["page_count"])
    expression = None if all_pages else ranges_expression(merge_ranges(selected))
    conversion = convert_pdf(source, expression, profile=profile, force=force, timeout=timeout)
    return {
        "ok": True,
        "command": "prepare",
        "tool_version": TOOL_VERSION,
        "source": str(source),
        "query": query,
        "pdf_kind": inspection["pdf_kind"],
        "page_count": inspection["page_count"],
        "strategy": strategy,
        "selected_ranges": conversion["selected_ranges"],
        "token_budget": token_budget,
        "output_dir": conversion["output_dir"],
        "markdown": conversion["markdown"],
        "images_dir": conversion["images_dir"],
        "cache": conversion["cache"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def status_pdf(source: Path) -> dict[str, Any]:
    output = output_dir_for(source)
    layout = ensure_layout(source) if output.exists() else layout_for(source)
    manifest_path = layout.manifest
    index_path = layout.index / "native-text.jsonl"
    manifest = read_manifest(source) if manifest_path.exists() else None
    return {
        "ok": True,
        "command": "status",
        "tool_version": TOOL_VERSION,
        "source": str(source),
        "output_dir": str(output),
        "output_exists": output.exists(),
        "markdown": str(layout.document) if layout.document.exists() else None,
        "images_dir": str(layout.images) if layout.images.exists() else None,
        "index_exists": index_path.exists(),
        "cached_selection_count": len(manifest.get("selections", [])) if manifest else 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mineru-pdf",
        description="Token-efficient local PDF preparation for AI agents.",
    )
    parser.add_argument("--version", action="version", version=f"mineru-pdf {TOOL_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser("inspect", help="Inspect structure and text density without MinerU.")
    inspect_parser.add_argument("pdf")

    search_parser = commands.add_parser("search", help="Search native page text and return ranked snippets.")
    search_parser.add_argument("pdf")
    search_parser.add_argument("--query", "-q", required=True)
    search_parser.add_argument("--top-k", type=int, default=8)

    convert_parser = commands.add_parser("convert", help="Convert all or selected PDF pages to Markdown.")
    convert_parser.add_argument("pdf")
    convert_parser.add_argument("--pages", help="1-based pages, e.g. 3, 3-8, or 1-3,8,12-15")
    convert_parser.add_argument("--profile", choices=("balanced", "accurate", "pipeline"), default="balanced")
    convert_parser.add_argument("--force", action="store_true")
    convert_parser.add_argument("--timeout", type=int, default=1800)

    prepare_parser = commands.add_parser("prepare", help="Locate and convert a minimal page set for a question.")
    prepare_parser.add_argument("pdf")
    prepare_parser.add_argument("--query", "-q", required=True)
    prepare_parser.add_argument("--profile", choices=("balanced", "accurate", "pipeline"), default="balanced")
    prepare_parser.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET)
    prepare_parser.add_argument("--context-pages", type=int, default=1)
    prepare_parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    prepare_parser.add_argument("--top-k", type=int, default=8)
    prepare_parser.add_argument("--force", action="store_true")
    prepare_parser.add_argument("--timeout", type=int, default=1800)

    status_parser = commands.add_parser("status", help="Report cached index and converted selections.")
    status_parser.add_argument("pdf")
    return parser


def configure_streams() -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    configure_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        source = resolve_pdf(args.pdf)
        if args.command == "inspect":
            payload = inspect_pdf(source)
        elif args.command == "search":
            payload = search_pdf(source, args.query, top_k=max(1, args.top_k))
        elif args.command == "convert":
            payload = convert_pdf(
                source,
                args.pages,
                profile=args.profile,
                force=args.force,
                timeout=max(1, args.timeout),
            )
        elif args.command == "prepare":
            payload = prepare_pdf(
                source,
                query=args.query,
                profile=args.profile,
                token_budget=max(1, args.token_budget),
                context_pages=max(0, args.context_pages),
                max_pages=max(1, args.max_pages),
                top_k=max(1, args.top_k),
                force=args.force,
                timeout=max(1, args.timeout),
            )
        elif args.command == "status":
            payload = status_pdf(source)
        else:
            raise AgentError("INVALID_COMMAND", f"Unknown command: {args.command}", 2)
        emit(payload)
        return 0
    except AgentError as exc:
        emit(
            {
                "ok": False,
                "error_code": exc.code,
                "message": str(exc),
                "retryable": exc.retryable,
            }
        )
        return exc.exit_code
    except KeyboardInterrupt:
        emit({"ok": False, "error_code": "CANCELLED", "message": "Operation cancelled.", "retryable": True})
        return 7
    except Exception as exc:
        emit({"ok": False, "error_code": "INTERNAL_ERROR", "message": str(exc), "retryable": False})
        return 8


if __name__ == "__main__":
    raise SystemExit(main())
