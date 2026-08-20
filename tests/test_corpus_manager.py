from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("manage_corpus", ROOT / "scripts" / "manage-corpus.py")
assert SPEC and SPEC.loader
corpus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(corpus)

def item(**changes):
    value = {
        "id": "sample", "title": "Sample", "language": "en",
        "document_type": "manual", "field": "testing",
        "source_page": "https://example.test/sample",
        "url": "https://example.test/sample.pdf",
        "license_class": "copyrighted-publicly-readable", "redistributable": False,
        "training_eligible": False, "suite": "smoke",
        "expected_front_regions": ["contents"], "local_path": "sample.pdf",
    }
    value.update(changes)
    return value

class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str = "https://cdn.example.test/sample.pdf",
                 headers: dict[str, str] | None = None):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body)), "ETag": '"abc"', "Last-Modified": "today"}
        self.headers.update(headers or {})
        self.url = url
    def geturl(self):
        return self.url
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()

def public_resolver(host, port, **kwargs):
    return [(corpus.socket.AF_INET, corpus.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

def private_resolver(host, port, **kwargs):
    return [(corpus.socket.AF_INET, corpus.socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

def rfc2544_proxy_resolver(host, port, **kwargs):
    return [(corpus.socket.AF_INET, corpus.socket.SOCK_STREAM, 6, "", ("198.18.3.193", port))]

class CorpusManagerTests(unittest.TestCase):
    def write_manifest(self, directory: Path, documents, name: str = "corpus.json"):
        path = directory / name
        path.write_text(json.dumps({
            "schema_version": 1,
            "front_region_schema": "pdf2md.front-regions.v1",
            "documents": documents,
        }), encoding="utf-8")
        return path

    def test_repository_manifest_is_valid_and_diverse(self):
        manifest = corpus.load_manifest(ROOT / "data" / "corpus.json")
        documents = manifest["documents"]
        self.assertGreaterEqual(len(documents), 24)
        self.assertIn("zh-CN", {entry["language"] for entry in documents})
        self.assertIn("en", {entry["language"] for entry in documents})
        self.assertGreaterEqual(len({entry["field"] for entry in documents}), 8)
        watermarked = next(entry for entry in documents if entry["id"] == "local-labview-fpga-watermarked")
        self.assertFalse(watermarked["training_eligible"])
        titles = {entry["id"]: entry["title"] for entry in documents}
        self.assertEqual(
            titles["mee-contaminated-site-remediation-guide-2014-zh"],
            "\u6c61\u67d3\u573a\u5730\u4fee\u590d\u6280\u672f\u5e94\u7528\u6307\u5357\uff08\u5f81\u6c42\u610f\u89c1\u7a3f\uff09",
        )
        self.assertEqual(titles["commons-jihe-yuanben-volume-2-scan"], "\u5e7e\u4f55\u539f\u672c \u4e8c")
        self.assertEqual(
            titles["wuli-tidal-disruption-events-2018-zh"],
            "\u9ed1\u6d1e\u6f6e\u6c50\u6495\u88c2\u6052\u661f\u4e8b\u4ef6\u53ca\u5176\u56de\u54cd",
        )
        wuli = next(entry for entry in documents if entry["id"] == "wuli-tidal-disruption-events-2018-zh")
        self.assertIsNone(wuli["url"])
        smoke = [entry for entry in documents if entry["suite"] == "smoke" and entry["url"] is not None]
        self.assertEqual(len(smoke), 7)
        self.assertTrue(all(entry.get("expected_sha256") and entry.get("expected_size") for entry in smoke))

    def test_manifest_rejects_http_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(corpus.CorpusError):
                corpus.load_manifest(self.write_manifest(root, [item(url="http://example.test/a.pdf")]))
            with self.assertRaises(corpus.CorpusError):
                corpus.load_manifest(self.write_manifest(root, [item(local_path="../a.pdf")]))

    def test_manifest_rejects_windows_drive_ads_device_and_alias_paths(self):
        paths = (
            "C:sample.pdf",
            "corpus.json:stream.pdf",
            "CON.pdf",
            "nested/COM1.pdf",
            "folder./sample.pdf",
            "folder /sample.pdf",
        )
        for local_path in paths:
            with self.subTest(local_path=local_path), tempfile.TemporaryDirectory() as temporary:
                manifest = self.write_manifest(
                    Path(temporary), [item(local_path=local_path)],
                )
                with self.assertRaises(corpus.CorpusError):
                    corpus.load_manifest(manifest)

    def test_manifest_rejects_private_literal_userinfo_and_nonstandard_port(self):
        urls = (
            "https://localhost/a.pdf",
            "https://127.0.0.1/a.pdf",
            "https://10.0.0.1/a.pdf",
            "https://[::1]/a.pdf",
            "https://user:password@example.test/a.pdf",
            "https://example.test:444/a.pdf",
        )
        for url in urls:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as temporary:
                manifest = self.write_manifest(Path(temporary), [item(url=url)])
                with self.assertRaises(corpus.CorpusError):
                    corpus.load_manifest(manifest)

    def test_manifest_rejects_unsafe_ipv4_and_ipv6_literals(self):
        urls = (
            "https://224.0.0.1/a.pdf",             # IPv4 multicast
            "https://0.0.0.0/a.pdf",               # IPv4 unspecified
            "https://240.0.0.1/a.pdf",             # IPv4 reserved
            "https://[ff02::1]/a.pdf",              # IPv6 multicast
            "https://[fec0::1]/a.pdf",              # IPv6 site-local
            "https://[::]/a.pdf",                   # IPv6 unspecified
            "https://[100::]/a.pdf",                # IPv6 reserved
            "https://[::ffff:127.0.0.1]/a.pdf",     # IPv4-mapped loopback
            "https://[::ffff:224.0.0.1]/a.pdf",     # IPv4-mapped multicast
            "https://[::10.0.0.1]/a.pdf",           # IPv4-compatible private
        )
        for url in urls:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as temporary:
                manifest = self.write_manifest(Path(temporary), [item(url=url)])
                with self.assertRaisesRegex(corpus.CorpusError, "non-public IP"):
                    corpus.load_manifest(manifest)

    def test_manifest_rejects_invalid_id_suite_text_license_path_and_pin(self):
        cases = (
            {"id": "Bad ID"},
            {"suite": "manual-only"},
            {"title": " "},
            {"license_class": "made-up"},
            {"local_path": "sample.txt"},
            {"local_path": "corpus.json"},
            {"expected_sha256": "0" * 64},
            {"expected_size": 10},
            {"expected_sha256": "bad", "expected_size": 10},
            {"expected_sha256": "0" * 64, "expected_size": 0},
        )
        for changes in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temporary:
                manifest = self.write_manifest(Path(temporary), [item(**changes)])
                with self.assertRaises(corpus.CorpusError):
                    corpus.load_manifest(manifest)

    def test_manifest_rejects_unknown_or_duplicate_front_region_kind(self):
        for regions in (["title"], ["contents", "contents"]):
            with self.subTest(regions=regions), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manifest = self.write_manifest(
                    root,
                    [item(expected_front_regions=regions)],
                )
                with self.assertRaises(corpus.CorpusError):
                    corpus.load_manifest(manifest)

    def test_nonlocal_manual_candidate_may_omit_direct_pdf_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item(url=None, suite="extended")])
            loaded = corpus.load_manifest(manifest)
            self.assertIsNone(loaded["documents"][0]["url"])

    def test_mock_download_is_atomic_and_returns_metadata(self):
        body = b"%PDF-1.7\nmock\n%%EOF\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata = corpus.download_one(
                item(), root, 1024,
                opener=lambda *a, **k: Response(body), resolver=public_resolver,
            )
            self.assertEqual((root / "sample.pdf").read_bytes(), body)
            self.assertFalse((root / "sample.pdf.part").exists())
            self.assertEqual(metadata["sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(metadata["size"], len(body))
            self.assertEqual(metadata["resolved_url"], "https://cdn.example.test/sample.pdf")

    def test_mock_download_rejects_non_pdf_and_cleans_part(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(corpus.CorpusError, "not a PDF"):
                corpus.download_one(
                    item(), root, 1024,
                    opener=lambda *a, **k: Response(b"<html>no</html>"), resolver=public_resolver,
                )
            self.assertFalse((root / "sample.pdf").exists())
            self.assertFalse((root / "sample.pdf.part").exists())

    def test_mock_download_rejects_redirect_to_http(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(corpus.CorpusError, "HTTPS"):
                corpus.download_one(
                    item(), Path(temporary), 1024,
                    opener=lambda *a, **k: Response(b"%PDF-1.7\n", "http://example.test/a.pdf"),
                    resolver=public_resolver,
                )

    def test_mock_download_rechecks_final_response_dns(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(corpus.CorpusError, "non-public"):
                corpus.download_one(
                    item(), Path(temporary), 1024,
                    opener=lambda *a, **k: Response(
                        b"%PDF-1.7\nmock\n%%EOF\n",
                        "https://internal.example.test/a.pdf",
                    ),
                    resolver=lambda host, port, **kwargs: (
                        private_resolver(host, port, **kwargs)
                        if host == "internal.example.test"
                        else public_resolver(host, port, **kwargs)
                    ),
                )

    def test_mock_download_enforces_manifest_pin_before_landing(self):
        body = b"%PDF-1.7\npinned\n%%EOF\n"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata = corpus.download_one(
                item(expected_sha256=digest, expected_size=len(body)),
                root, 1024, opener=lambda *a, **k: Response(body), resolver=public_resolver,
            )
            self.assertEqual(metadata["sha256"], digest)
            self.assertTrue((root / "sample.pdf").exists())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(corpus.CorpusError, "manifest-pinned"):
                corpus.download_one(
                    item(expected_sha256="0" * 64, expected_size=len(body)),
                    root, 1024, opener=lambda *a, **k: Response(body), resolver=public_resolver,
                )
            self.assertFalse((root / "sample.pdf").exists())
            self.assertFalse((root / "sample.pdf.part").exists())

    def test_download_rejects_existing_part_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            part = root / "sample.pdf.part"
            part.write_bytes(b"keep")
            with self.assertRaisesRegex(corpus.CorpusError, "temporary download path"):
                corpus.download_one(item(), root, 1024, resolver=public_resolver)
            self.assertEqual(part.read_bytes(), b"keep")

    def test_download_does_not_delete_part_claimed_in_open_race(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            part = root / "sample.pdf.part"
            real_open = Path.open

            def racing_open(path, mode="r", *args, **kwargs):
                if path == part and mode == "xb":
                    with real_open(path, "wb") as peer:
                        peer.write(b"peer download")
                    raise FileExistsError(str(path))
                return real_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", racing_open):
                with self.assertRaisesRegex(corpus.CorpusError, "claimed concurrently"):
                    corpus.download_one(item(), root, 1024, resolver=public_resolver)
            self.assertEqual(part.read_bytes(), b"peer download")

    def test_download_does_not_overwrite_target_created_during_transfer(self):
        body = b"%PDF-1.7\nnew\n%%EOF\n"
        existing = b"%PDF-1.7\npeer\n%%EOF\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "sample.pdf"

            def opener(*args, **kwargs):
                target.write_bytes(existing)
                return Response(body)

            with self.assertRaisesRegex(corpus.CorpusError, "appeared during download"):
                corpus.download_one(
                    item(), root, 1024, opener=opener, resolver=public_resolver,
                )
            self.assertEqual(target.read_bytes(), existing)
            self.assertFalse((root / "sample.pdf.part").exists())

    def test_initial_dns_and_redirect_dns_are_checked_before_request(self):
        mocked_open = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(corpus.CorpusError, "non-public"):
                corpus.download_one(
                    item(), Path(temporary), 1024,
                    opener=mocked_open, resolver=private_resolver,
                )
            mocked_open.assert_not_called()
        handler = corpus.SafeRedirectHandler("sample", private_resolver)
        request = corpus.urllib.request.Request("https://example.test/sample.pdf")
        with self.assertRaisesRegex(corpus.CorpusError, "non-public"):
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://cdn.example.test/sample.pdf",
            )

    def test_dns_rejects_unsafe_ipv4_and_ipv6_results(self):
        addresses = (
            "224.0.0.1",          # IPv4 multicast is_global is True
            "0.0.0.0",            # IPv4 unspecified
            "240.0.0.1",          # IPv4 reserved
            "ff02::1",            # IPv6 multicast is_global is True
            "fec0::1",            # IPv6 site-local is_global is True
            "::",                 # IPv6 unspecified
            "100::",              # IPv6 reserved
            "::ffff:127.0.0.1",   # IPv4-mapped loopback
            "::ffff:224.0.0.1",   # IPv4-mapped multicast
            "::10.0.0.1",         # IPv4-compatible private
        )

        for address in addresses:
            family = corpus.socket.AF_INET6 if ":" in address else corpus.socket.AF_INET

            def resolver(host, port, **kwargs):
                endpoint = (
                    (address, port, 0, 0)
                    if family == corpus.socket.AF_INET6
                    else (address, port)
                )
                return [(family, corpus.socket.SOCK_STREAM, 6, "", endpoint)]

            with self.subTest(address=address):
                with self.assertRaisesRegex(corpus.CorpusError, "non-public IP"):
                    corpus.network_url(
                        "https://example.test/sample.pdf", "url", "sample", resolver,
                        allow_rfc2544_proxy_dns=True,
                    )

    def test_safe_redirect_allows_public_cross_domain_destination(self):
        handler = corpus.SafeRedirectHandler("sample", public_resolver)
        request = corpus.urllib.request.Request("https://example.test/sample.pdf")
        redirected = handler.redirect_request(
            request, None, 302, "Found", {},
            "https://official-cdn.example.test/sample.pdf",
        )
        self.assertIsNotNone(redirected)
        self.assertEqual(
            redirected.full_url, "https://official-cdn.example.test/sample.pdf",
        )

    def test_rfc2544_proxy_dns_requires_explicit_opt_in(self):
        body = b"%PDF-1.7\nproxy\n%%EOF\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(corpus.CorpusError, "non-public"):
                corpus.download_one(
                    item(), root, 1024,
                    opener=lambda *a, **k: Response(body), resolver=rfc2544_proxy_resolver,
                )
            metadata = corpus.download_one(
                item(), root, 1024,
                opener=lambda *a, **k: Response(body), resolver=rfc2544_proxy_resolver,
                allow_rfc2544_proxy_dns=True,
            )
            self.assertEqual(metadata["size"], len(body))

    def test_rfc2544_proxy_mode_still_rejects_private_dns_and_literal_ips(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(corpus.CorpusError, "non-public"):
                corpus.download_one(
                    item(), Path(temporary), 1024, resolver=private_resolver,
                    allow_rfc2544_proxy_dns=True,
                )
        with self.assertRaisesRegex(corpus.CorpusError, "non-public IP"):
            corpus.network_url(
                "https://198.18.3.193/sample.pdf", "url", "sample",
                rfc2544_proxy_resolver, True,
            )

    def test_download_rejects_partial_or_truncated_pdf_response(self):
        complete = b"%PDF-1.7\nbody\n%%EOF\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(corpus.CorpusError, "partial PDF"):
                corpus.download_one(
                    item(), root, 1024,
                    opener=lambda *a, **k: Response(
                        complete, headers={"Content-Range": "bytes 0-20/200"},
                    ),
                    resolver=public_resolver,
                )
            self.assertFalse((root / "sample.pdf").exists())

    def test_download_safely_resumes_an_announced_pdf_with_valid_ranges(self):
        body = b"%PDF-1.7\n" + (b"x" * 30) + b"\n%%EOF\n"
        prefix_size = 13
        requests = []

        def opener(request, timeout=60):
            requests.append(request)
            requested_range = request.get_header("Range")
            if requested_range is None:
                return Response(
                    body[:prefix_size],
                    headers={
                        "Content-Length": str(len(body)),
                        "Accept-Ranges": "bytes",
                    },
                )
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", requested_range)
            self.assertIsNotNone(match)
            start, end = map(int, match.groups())
            chunk = body[start : end + 1]
            return Response(
                chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{len(body)}",
                },
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata = corpus.download_one(
                item(), root, 1024, opener=opener, resolver=public_resolver,
            )
            self.assertEqual((root / "sample.pdf").read_bytes(), body)
            self.assertEqual(metadata["size"], len(body))
            self.assertEqual(metadata["sha256"], hashlib.sha256(body).hexdigest())
        self.assertGreaterEqual(len(requests), 2)
        self.assertEqual(requests[1].get_header("If-range"), '"abc"')

    def test_download_rejects_inconsistent_ranged_resume(self):
        body = b"%PDF-1.7\nbody\n%%EOF\n"

        def opener(request, timeout=60):
            requested_range = request.get_header("Range")
            if requested_range is None:
                return Response(
                    body[:8],
                    headers={
                        "Content-Length": str(len(body)),
                        "Accept-Ranges": "bytes",
                    },
                )
            return Response(
                body[8:],
                headers={
                    "Content-Range": f"bytes 9-{len(body) - 1}/{len(body)}",
                },
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(corpus.CorpusError, "invalid ranged"):
                corpus.download_one(
                    item(), root, 1024, opener=opener, resolver=public_resolver,
                )
            self.assertFalse((root / "sample.pdf").exists())
            self.assertFalse((root / "sample.pdf.part").exists())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(corpus.CorpusError, "not a PDF"):
                corpus.download_one(
                    item(), root, 1024,
                    opener=lambda *a, **k: Response(b"%PDF-1.7\ntruncated"),
                    resolver=public_resolver,
                )
            self.assertFalse((root / "sample.pdf").exists())

    def test_download_command_rebuilds_state_for_manifest_pinned_existing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body = b"%PDF-1.7\nexisting\n%%EOF\n"
            digest = hashlib.sha256(body).hexdigest()
            manifest = self.write_manifest(root, [item(
                expected_sha256=digest, expected_size=len(body),
            )])
            (root / "sample.pdf").write_bytes(body)
            state = root / "state.json"
            with patch.object(corpus, "download_one") as download:
                result = corpus.main(["--manifest", str(manifest), "--state", str(state), "download"])
            self.assertEqual(result, 0)
            download.assert_not_called()
            recorded = json.loads(state.read_text(encoding="utf-8"))["documents"]["sample"]
            self.assertEqual(recorded["size"], len(body))
            self.assertEqual(recorded["sha256"], digest)

    def test_download_command_requires_state_for_unpinned_existing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item()])
            (root / "sample.pdf").write_bytes(b"%PDF-1.7\nexisting\n%%EOF\n")
            with patch.object(corpus, "download_one") as download:
                result = corpus.main([
                    "--manifest", str(manifest), "--state", str(root / "state.json"), "download",
                    "--accept-unpinned",
                ])
            self.assertEqual(result, 2)
            download.assert_not_called()

    def test_pinned_existing_never_uses_matching_state_to_bypass_manifest(self):
        body = b"%PDF-1.7\nexisting\n%%EOF\n"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item(
                expected_sha256="0" * 64, expected_size=len(body),
            )])
            (root / "sample.pdf").write_bytes(body)
            state = root / "state.json"
            state.write_text(json.dumps({"schema_version": 1, "documents": {
                "sample": {"size": len(body), "sha256": digest},
            }}), encoding="utf-8")
            common = ["--manifest", str(manifest), "--state", str(state)]
            with patch.object(corpus, "download_one") as download:
                self.assertEqual(corpus.main(common + ["download"]), 2)
            download.assert_not_called()
            self.assertEqual(corpus.main(common + ["verify"]), 1)

    def test_download_command_requires_opt_in_for_unpinned_existing_with_matching_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body = b"%PDF-1.7\nexisting\n%%EOF\n"
            digest = hashlib.sha256(body).hexdigest()
            manifest = self.write_manifest(root, [item()])
            (root / "sample.pdf").write_bytes(body)
            state = root / "state.json"
            state.write_text(json.dumps({"schema_version": 1, "documents": {
                "sample": {"size": len(body), "sha256": digest},
            }}), encoding="utf-8")
            with patch.object(corpus, "download_one") as download:
                result = corpus.main([
                    "--manifest", str(manifest), "--state", str(state), "download",
                ])
            self.assertEqual(result, 2)
            download.assert_not_called()
            with patch.object(corpus, "download_one") as download:
                result = corpus.main([
                    "--manifest", str(manifest), "--state", str(state), "download",
                    "--accept-unpinned",
                ])
            self.assertEqual(result, 0)
            download.assert_not_called()

    def test_download_command_rejects_missing_unpinned_without_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item()])
            with patch.object(corpus, "download_one") as download:
                result = corpus.main([
                    "--manifest", str(manifest), "--state", str(root / "state.json"), "download",
                ])
            self.assertEqual(result, 2)
            download.assert_not_called()

    def test_download_id_does_not_implicitly_include_smoke_suite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(
                root,
                [
                    item(id="smoke", local_path="smoke.pdf"),
                    item(id="chosen", suite="extended", local_path="chosen.pdf"),
                ],
            )
            with patch.object(
                corpus,
                "download_one",
                return_value={"sha256": "0" * 64, "size": 9},
            ) as download:
                result = corpus.main([
                    "--manifest", str(manifest),
                    "--state", str(root / "state.json"),
                    "download", "--id", "chosen", "--accept-unpinned",
                ])
            self.assertEqual(result, 0)
            download.assert_called_once()
            self.assertEqual(download.call_args.args[0]["id"], "chosen")

    def test_verify_detects_digest_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item()])
            pdf = root / "sample.pdf"
            pdf.write_bytes(b"%PDF-1.7\nnew\n%%EOF\n")
            state = root / "state.json"
            state.write_text(json.dumps({"schema_version": 1, "documents": {
                "sample": {"size": 1, "sha256": "0" * 64}
            }}), encoding="utf-8")
            self.assertEqual(corpus.main([
                "--manifest", str(manifest), "--state", str(state), "verify",
                "--accept-unpinned",
            ]), 1)

    def test_verify_without_state_accepts_pin_but_rejects_unpinned(self):
        body = b"%PDF-1.7\nverified\n%%EOF\n"
        digest = hashlib.sha256(body).hexdigest()
        for pinned, expected in ((True, 0), (False, 1)):
            with self.subTest(pinned=pinned), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                changes = {
                    "expected_sha256": digest,
                    "expected_size": len(body),
                } if pinned else {}
                manifest = self.write_manifest(root, [item(**changes)])
                (root / "sample.pdf").write_bytes(body)
                result = corpus.main([
                    "--manifest", str(manifest), "--state", str(root / "state.json"), "verify",
                ])
                self.assertEqual(result, expected)

    def test_verify_requires_opt_in_even_when_unpinned_state_matches(self):
        body = b"%PDF-1.7\nverified\n%%EOF\n"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item()])
            (root / "sample.pdf").write_bytes(body)
            state = root / "state.json"
            state.write_text(json.dumps({"schema_version": 1, "documents": {
                "sample": {"size": len(body), "sha256": digest},
            }}), encoding="utf-8")
            base = ["--manifest", str(manifest), "--state", str(state), "verify"]
            self.assertEqual(corpus.main(base), 1)
            self.assertEqual(corpus.main(base + ["--accept-unpinned"]), 0)

    def test_pinned_existing_uses_manifest_as_authority_over_stale_state(self):
        body = b"%PDF-1.7\npinned\n%%EOF\n"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item(
                expected_sha256=digest, expected_size=len(body),
            )])
            (root / "sample.pdf").write_bytes(body)
            state = root / "state.json"
            state.write_text(json.dumps({"schema_version": 1, "documents": {
                "sample": {"size": len(body) + 1, "sha256": "0" * 64},
            }}), encoding="utf-8")
            self.assertEqual(corpus.main([
                "--manifest", str(manifest), "--state", str(state), "verify",
            ]), 0)

    def test_state_rejects_duplicate_keys_unknown_fields_and_invalid_scalar_types(self):
        invalid_states = (
            '{"schema_version":1,"documents":{},"documents":{}}',
            json.dumps({"schema_version": True, "documents": {}}),
            json.dumps({"schema_version": 1, "documents": {}, "extra": 1}),
            json.dumps({"schema_version": 1, "documents": {"Bad ID": {
                "size": 10, "sha256": "0" * 64,
            }}}),
            json.dumps({"schema_version": 1, "documents": {"sample": {
                "size": True, "sha256": "0" * 64,
            }}}),
            json.dumps({"schema_version": 1, "documents": {"sample": {
                "size": 10, "sha256": "bad",
            }}}),
            json.dumps({"schema_version": 1, "documents": {"sample": {
                "size": 10, "sha256": "0" * 64, "unexpected": "value",
            }}}),
        )
        for content in invalid_states:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.json"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(corpus.CorpusError):
                    corpus.load_state(path)

    def test_control_files_cannot_collide_with_manifest_state_or_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item(local_path="state.pdf")])
            self.assertEqual(corpus.main([
                "--manifest", str(manifest), "--state", str(root / "state.pdf"), "list",
            ]), 2)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(
                root, [item(local_path="manifest.pdf")], name="manifest.pdf",
            )
            self.assertEqual(corpus.main([
                "--manifest", str(manifest), "--state", str(root / "state.json"), "list",
            ]), 2)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item()])
            self.assertEqual(corpus.main([
                "--manifest", str(manifest), "--state", str(root / "README.md"), "list",
            ]), 2)

    def test_control_and_payload_paths_reject_part_and_ancestor_collisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item()])
            self.assertEqual(corpus.main([
                "--manifest", str(manifest),
                "--state", str(root / "sample.pdf.part"), "list",
            ]), 2)
        for second_path in ("sample.pdf/child.pdf", "sample.pdf.part/child.pdf"):
            with self.subTest(second_path=second_path), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manifest = self.write_manifest(root, [
                    item(), item(id="second", local_path=second_path),
                ])
                self.assertEqual(corpus.main([
                    "--manifest", str(manifest), "list",
                ]), 2)

    def test_state_path_rejects_ntfs_alternate_data_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item()])
            self.assertEqual(corpus.main([
                "--manifest", str(manifest),
                "--state", str(root / "unrelated.txt:state.json"), "list",
            ]), 2)

    def test_custom_manifest_defaults_state_to_its_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, [item()], name="custom.json")
            with patch.object(corpus, "list_command", return_value=0) as listing:
                self.assertEqual(corpus.main(["--manifest", str(manifest), "list"]), 0)
            self.assertEqual(listing.call_args.args[0].state, (root / "local-state.json").resolve())

if __name__ == "__main__":
    unittest.main()
