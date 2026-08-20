from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pdf2md_audit_navigation",
    ROOT / "scripts" / "audit-navigation.py",
)
assert SPEC is not None and SPEC.loader is not None
audit_navigation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_navigation)


class NavigationAuditContextTests(unittest.TestCase):
    def _artifact(self, root: Path, pages: str) -> tuple[Path, Path]:
        source = root / "paper.pdf"
        source.write_bytes(b"%PDF-test")
        output = root / "paper.pdf2md"
        output.mkdir()
        markdown = output / "paper.md"
        markdown.write_text("# Body\n", encoding="utf-8")
        manifest = output / "raw" / "manifest.json"
        manifest.parent.mkdir()
        manifest.write_text(
            json.dumps({"selections": [{"pages": pages}]}),
            encoding="utf-8",
        )
        return markdown, source

    def test_full_replay_prefers_exact_v8_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            markdown, source = self._artifact(Path(temporary), "all")
            cache_root = markdown.parent / "raw" / "cache"
            cache_root.mkdir()
            legacy = cache_root / "frontmatter-v7.json"
            exact = cache_root / "frontmatter-v8-all.json"
            legacy.write_text("{}", encoding="utf-8")
            exact.write_text("{}", encoding="utf-8")

            actual = audit_navigation._navigation_replay_context(markdown)

        self.assertEqual(actual, (source, exact, None, None))

    def test_partial_replay_uses_page_bound_v8_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            markdown, source = self._artifact(Path(temporary), "1-20")
            cache = markdown.parent / "raw" / "cache" / "frontmatter-v8-1-20.json"
            cache.parent.mkdir()
            cache.write_text("{}", encoding="utf-8")

            actual = audit_navigation._navigation_replay_context(markdown)

        self.assertEqual(actual, (source, cache, None, range(1, 21)))

    def test_missing_cache_never_enables_pdf_read_during_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            markdown, _source = self._artifact(Path(temporary), "all")

            actual = audit_navigation._navigation_replay_context(markdown)

        self.assertEqual(actual, (None, None, None, None))


if __name__ == "__main__":
    unittest.main()
