from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pypdf import PdfReader


SCHEMA_VERSION = 1
TOOL_VERSION = "2.5.0"
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
    cli: Path
    python: Path


def runtime() -> Runtime:
    script = Path(__file__).resolve()
    configured_root = os.getenv("PDF2MD_ROOT")
    candidates = ([Path(configured_root).expanduser().resolve()] if configured_root else []) + list(script.parents)
    root = next(
        (
            candidate
            for candidate in candidates
            if (candidate / "runtime" / "env" / "python.exe").is_file()
            and (candidate / "src" / "pdf2md_cli.py").is_file()
        ),
        script.parents[3],
    )
    return Runtime(
        root=root,
        cli=root / "src" / "pdf2md_cli.py",
        python=root / "runtime" / "env" / "python.exe",
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
    output = source.parent / f"{source.stem}.pdf2md"
    if not output.exists():
        legacy = source.parent / f"{source.stem}.mineru"
        if legacy.is_dir():
            legacy.replace(output)
    return output


@dataclass(frozen=True)
class OutputLayout:
    root: Path
    document: Path
    images: Path
    raw: Path
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
        index=raw / "index",
        manifest=raw / "manifest.json",
        inspection=raw / "inspect.json",
    )


def ensure_layout(source: Path) -> OutputLayout:
    layout = layout_for(source)
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.raw.mkdir(parents=True, exist_ok=True)
    layout.index.mkdir(parents=True, exist_ok=True)
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
    path = layout_for(source).manifest
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


def convert_pdf(
    source: Path,
    pages_expression: str | None,
    profile: str = "balanced",
    force: bool = False,
    timeout: int = 1800,
) -> dict[str, Any]:
    started = time.monotonic()
    rt = runtime()
    if not rt.cli.exists() or not rt.python.exists():
        raise AgentError("RUNTIME_MISSING", f"PDF2MD runtime is incomplete under {rt.root}", 5)

    reader = open_reader(source)
    page_count = len(reader.pages)
    if pages_expression:
        pages = parse_pages(pages_expression, page_count)
        ranges = merge_ranges(pages)
    else:
        pages = list(range(1, page_count + 1))
        ranges = [(1, page_count)]
    layout = ensure_layout(source)
    normalized_pages = ranges_expression(ranges)
    command = [
        str(rt.python),
        str(rt.cli),
        str(source),
        "--output",
        str(layout.root),
        "--profile",
        profile,
        "--timeout",
        str(max(1, timeout)),
        "--json",
    ]
    command.extend(("--pages", normalized_pages))
    if force:
        command.append("--force")

    print(f"[pdf2md-read-pdf] converting PDF pages {normalized_pages} ({profile})", file=sys.stderr)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    process = subprocess.Popen(
        command,
        cwd=str(rt.root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    try:
        stdout, stderr = process.communicate(timeout=max(31, timeout + 30))
    except subprocess.TimeoutExpired as exc:
        terminate_tree(process)
        process.communicate()
        raise AgentError("CONVERSION_TIMEOUT", f"PDF2MD timed out after {timeout} seconds.", 7, True) from exc

    if stderr:
        status_lines = [line for line in stderr.splitlines() if line.startswith("[状态]")]
        if status_lines:
            print("\n".join(status_lines[-8:]), file=sys.stderr)
    try:
        payload = json.loads(stdout.strip())
    except ValueError as exc:
        tail = "\n".join((stderr or stdout).splitlines()[-12:])
        raise AgentError("CONVERSION_FAILED", f"PDF2MD CLI returned invalid JSON.\n{tail}", 6, True) from exc
    if process.returncode != 0 or not payload.get("ok"):
        message = str(payload.get("message") or "\n".join(stderr.splitlines()[-12:]))
        raise AgentError("CONVERSION_FAILED", message, 6, True)

    markdown = Path(str(payload.get("markdown", "")))
    images_dir = Path(str(payload.get("images_dir", "")))
    if not markdown.is_file() or not images_dir.is_dir():
        raise AgentError("MARKDOWN_NOT_FOUND", "PDF2MD CLI did not publish Markdown + images.", 6, True)
    return {
        "ok": True,
        "command": "convert",
        "tool_version": TOOL_VERSION,
        "source": str(source),
        "page_count": page_count,
        "selected_ranges": normalized_pages,
        "profile": profile,
        "output_dir": str(layout.root),
        "markdown": str(markdown),
        "images_dir": str(images_dir),
        "cache": payload.get("cache", "updated"),
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
    layout = layout_for(source)
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
        prog="pdf2md-pdf",
        description="Token-efficient local PDF preparation for AI agents.",
    )
    parser.add_argument("--version", action="version", version=f"pdf2md-pdf {TOOL_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser("inspect", help="Inspect structure and text density without starting OCR.")
    inspect_parser.add_argument("pdf")

    search_parser = commands.add_parser("search", help="Search native page text and return ranked snippets.")
    search_parser.add_argument("pdf")
    search_parser.add_argument("--query", "-q", required=True)
    search_parser.add_argument("--top-k", type=int, default=8)

    convert_parser = commands.add_parser("convert", help="Convert all or selected PDF pages to Markdown.")
    convert_parser.add_argument("pdf")
    convert_parser.add_argument("--pages", help="1-based pages, e.g. 3, 3-8, or 1-3,8,12-15")
    convert_parser.add_argument("--profile", choices=("fast", "balanced", "accurate"), default="balanced")
    convert_parser.add_argument("--force", action="store_true")
    convert_parser.add_argument("--timeout", type=int, default=1800)

    prepare_parser = commands.add_parser("prepare", help="Locate and convert a minimal page set for a question.")
    prepare_parser.add_argument("pdf")
    prepare_parser.add_argument("--query", "-q", required=True)
    prepare_parser.add_argument("--profile", choices=("fast", "balanced", "accurate"), default="balanced")
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
