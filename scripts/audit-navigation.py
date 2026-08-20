from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pdf2md_toc as navigation  # noqa: E402


ANCHOR_RE = re.compile(r'<a\s+id="(?P<id>[^"]+)"[^>]*></a>', re.I)
GENERATED_TARGET_RE = re.compile(
    r'<a\s+id="(?P<id>\d+)"\s+data-pdf2md-nav="target"'
    r'(?:\s+data-pdf2md-heading="generated")?></a>',
    re.I,
)
GENERATED_SECTION_RE = re.compile(
    r'<a\s+id="(?P<id>[^"]+)"\s+data-pdf2md-nav="section"></a>', re.I
)
LOCAL_LINK_RE = re.compile(r"\]\(#(?P<id>[^\s)]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+\S.*$")


def _markdown_files(inputs: list[Path]) -> list[Path]:
    found: set[Path] = set()
    for item in inputs:
        item = item.expanduser().resolve()
        if item.is_file() and item.suffix.casefold() == ".md":
            found.add(item)
            continue
        if not item.is_dir():
            continue
        for markdown in item.rglob("*.md"):
            if markdown.parent.name.casefold().endswith(".pdf2md"):
                found.add(markdown.resolve())
    return sorted(found, key=lambda path: str(path).casefold())


def _source_for(markdown: Path) -> Path | None:
    output = markdown.parent
    if not output.name.casefold().endswith(".pdf2md"):
        return None
    source = output.with_name(output.name[: -len(".pdf2md")] + ".pdf")
    return source if source.is_file() else None


def _frontmatter_cache_for(output: Path, pages: str) -> Path | None:
    """Return an existing cache without causing the audit to read the PDF."""
    cache_root = output / "raw" / "cache"
    candidates = [cache_root / f"frontmatter-v8-{pages}.json"]
    if pages == "all":
        candidates.extend(
            (
                cache_root / "frontmatter-v8.json",
                cache_root / "frontmatter-v7.json",
            )
        )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _front_region_report(output: Path) -> dict[str, object] | None:
    report_path = output / "raw" / "cache" / "front-regions-v1.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return report if isinstance(report, dict) else None


def _navigation_replay_context(
    markdown: Path,
) -> tuple[Path | None, Path | None, dict[str, object] | None, range | None]:
    """Recover the context used to publish a single-selection artifact."""
    output = markdown.parent
    manifest_path = output / "raw" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        manifest = None
    selections = manifest.get("selections") if isinstance(manifest, dict) else None
    if not isinstance(selections, list) or len(selections) != 1:
        source = _source_for(markdown)
        cache = _frontmatter_cache_for(output, "all")
        return (source if cache is not None else None), cache, None, None
    selection = selections[0]
    if not isinstance(selection, dict):
        return None, None, None, None
    pages = str(selection.get("pages", "")).strip().casefold()
    if pages == "all":
        source = _source_for(markdown)
        cache = _frontmatter_cache_for(output, pages)
        return (
            source if cache is not None else None,
            cache,
            _front_region_report(output),
            None,
        )
    match = re.fullmatch(r"(?P<start>[0-9]+)(?:-(?P<end>[0-9]+))?", pages)
    if match is None:
        return None, None, None, None
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if start < 1 or end < start:
        return None, None, None, None
    cache = _frontmatter_cache_for(output, pages)
    source = _source_for(markdown) if cache is not None else None
    return source, cache, _front_region_report(output), range(start, end + 1)


def audit(markdown: Path, check_idempotence: bool = False) -> dict[str, object]:
    content = markdown.read_text(encoding="utf-8", errors="strict")
    lines = content.splitlines()
    source, cache, front_regions, selected_pages = _navigation_replay_context(
        markdown
    )
    anchor_pairs = [
        (match.group("id"), index)
        for index, line in enumerate(lines)
        if (match := ANCHOR_RE.fullmatch(line)) is not None
    ]
    anchor_counts = Counter(identifier for identifier, _index in anchor_pairs)
    anchor_lines = {identifier: index for identifier, index in anchor_pairs}
    link_ids = [match.group("id") for match in LOCAL_LINK_RE.finditer(content)]
    errors: list[str] = []

    duplicates = sorted(identifier for identifier, count in anchor_counts.items() if count > 1)
    missing = sorted(set(link_ids) - set(anchor_counts))
    if duplicates:
        errors.append(f"duplicate anchors: {', '.join(duplicates)}")
    if missing:
        errors.append(f"missing link targets: {', '.join(missing)}")

    for index, line in enumerate(lines):
        target = GENERATED_TARGET_RE.fullmatch(line)
        if target and (index + 1 >= len(lines) or HEADING_RE.fullmatch(lines[index + 1]) is None):
            errors.append(f"line {index + 1}: target #{target.group('id')} is not followed by a heading")

    section_metrics: list[dict[str, object]] = []
    structured_navigation = navigation._structured_navigation_entries(
        front_regions,
        selected_pages,
    )
    sections = navigation._section_ranges(
        lines,
        front_regions=front_regions,
        selected_physical_pages=selected_pages,
        structured_navigation=structured_navigation,
    )
    for section in sections:
        section_anchor = ""
        if section.start > 0:
            marker = GENERATED_SECTION_RE.fullmatch(lines[section.start - 1])
            if marker:
                section_anchor = marker.group("id")
        entries = navigation._entries_from_markdown(lines, section)
        forward_links: list[tuple[str, int]] = []
        max_line = 0
        for index in range(section.start + 1, section.end):
            max_line = max(max_line, len(lines[index]))
            if navigation.BULLET_RE.fullmatch(lines[index]) is None:
                continue
            forward_links.extend(
                (match.group("id"), index)
                for match in LOCAL_LINK_RE.finditer(lines[index])
            )
        if max_line > 500:
            errors.append(f"{section.kind}: navigation line exceeds 500 characters")
        if len(forward_links) > len(entries):
            errors.append(
                f"{section.kind}: more forward links than parsed navigation entries"
            )
        for target_id, source_line in forward_links:
            target_line = anchor_lines.get(target_id)
            if target_line is None or not section_anchor:
                continue
            window = lines[target_line + 2 : min(len(lines), target_line + 9)]
            expected = f"](#{section_anchor})"
            if not any(expected in candidate for candidate in window):
                errors.append(
                    f"line {source_line + 1}: #{target_id} has no backlink to #{section_anchor}"
                )
        section_metrics.append(
            {
                "kind": section.kind,
                "entries": len(entries),
                "links": len(forward_links),
                "unlinked": max(0, len(entries) - len(forward_links)),
                "max_line": max_line,
            }
        )

    if check_idempotence:
        first = navigation.enhance_document_navigation(
            content,
            source=source,
            frontmatter_cache=cache,
            front_regions=front_regions,
            selected_physical_pages=selected_pages,
        )
        second = navigation.enhance_document_navigation(
            first,
            source=source,
            frontmatter_cache=cache,
            front_regions=front_regions,
            selected_physical_pages=selected_pages,
        )
        if second != first:
            errors.append("navigation publishing is not idempotent")

    return {
        "markdown": str(markdown),
        "ok": not errors,
        "errors": errors,
        "anchors": len(anchor_pairs),
        "links": len(link_ids),
        "sections": section_metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit PDF2MD navigation links and backlinks.")
    parser.add_argument("paths", nargs="+", type=Path, help="Markdown file or directory")
    parser.add_argument("--idempotent", action="store_true", help="also rerun navigation in memory")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()
    files = _markdown_files(args.paths)
    results = [audit(path, check_idempotence=args.idempotent) for path in files]
    if args.json:
        print(json.dumps({"files": results}, ensure_ascii=False, indent=2))
    else:
        for result in results:
            state = "OK" if result["ok"] else "FAIL"
            sections = ", ".join(
                f"{item['kind']} {item['links']}/{item['entries']}"
                for item in result["sections"]
            )
            print(f"{state}\t{result['markdown']}\t{sections}")
            for error in result["errors"]:
                print(f"  - {error}")
    return 0 if files and all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
