#!/usr/bin/env python3
"""Manage the optional PDF2MD regression corpus using only the stdlib."""
from __future__ import annotations
import argparse, hashlib, ipaddress, json, os, re, socket, sys, tempfile, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_MANIFEST = DATA_DIR / "corpus.json"
DEFAULT_STATE = DATA_DIR / "local-state.json"
MAX_FILE_BYTES = 512 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
RFC2544_BENCHMARK_NETS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001:2::/48"),
)
REQUIRED_FIELDS = {"id", "title", "language", "document_type", "field", "source_page", "url",
                   "license_class", "redistributable", "training_eligible", "suite",
                   "expected_front_regions", "local_path"}
OPTIONAL_FIELDS = {"expected_sha256", "expected_size"}
FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
CONTENT_RANGE_PATTERN = re.compile(
    r"bytes\s+(?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+)\Z",
    re.IGNORECASE,
)
SUITES = {"smoke", "core", "extended", "local-existing"}
LICENSE_CLASSES = {
    "cc0-1.0", "cc-by-3.0-de", "cc-by-4.0", "cc-by-nc-sa-4.0",
    "copyrighted-publicly-readable",
    "government-public-document", "open-access-unspecified", "public-domain-scan",
    "institutional-thesis", "us-government-public-domain", "user-provided-unknown",
    "vendor-documentation",
}
TEXT_FIELDS = {"title", "language", "document_type", "field", "license_class", "suite", "local_path"}
RESERVED_DATA_NAMES = {"corpus.json", "readme.md", "local-state.json"}
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
FRONT_REGION_KINDS = {
    "cover", "legal", "revision_history", "preface", "abstract",
    "acknowledgements", "contents", "list_of_figures", "list_of_tables",
    "abbreviations", "nomenclature", "body_start", "other_front",
}
STATE_FIELDS = {
    "resolved_url", "etag", "last_modified", "sha256", "size",
    "downloaded_at", "verified_at",
}
STATE_REQUIRED_FIELDS = {"sha256", "size"}

class CorpusError(RuntimeError):
    pass

def required_text(value: Any, field: str, document_id: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CorpusError(f"{document_id}: {field} must be a non-empty, trimmed string")
    return value

def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value

def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists() and default is not None:
        return default
    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object,
        )
    except (OSError, ValueError) as exc:
        raise CorpusError(f"cannot read JSON {path}: {exc}") from exc

def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()

def https_url(value: Any, field: str, document_id: str) -> str:
    parsed = urllib.parse.urlparse(value if isinstance(value, str) else "")
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise CorpusError(f"{document_id}: {field} must be an HTTPS URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CorpusError(f"{document_id}: {field} has an invalid port") from exc
    if parsed.username is not None or parsed.password is not None:
        raise CorpusError(f"{document_id}: {field} must not contain user information")
    if port not in (None, 443):
        raise CorpusError(f"{document_id}: {field} must use the default HTTPS port")
    hostname = urllib.parse.unquote(parsed.hostname).rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise CorpusError(f"{document_id}: {field} must not use a local host")
    address_text = hostname.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        address = None
    if address is not None and _is_non_public_address(address):
        raise CorpusError(f"{document_id}: {field} must not use a non-public IP address")
    return value

def _embedded_ipv4_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    if not isinstance(address, ipaddress.IPv6Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    numeric = int(address)
    # RFC 4291's deprecated IPv4-compatible form is ::IPv4. Exclude the two
    # IPv6 special addresses that also fall within ::/96.
    if 1 < numeric <= 0xFFFFFFFF:
        return ipaddress.IPv4Address(numeric)
    return None

def _has_forbidden_address_property(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if (
        address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or getattr(address, "is_site_local", False)
    ):
        return True
    embedded = _embedded_ipv4_address(address)
    return embedded is not None and (
        not embedded.is_global
        or embedded.is_multicast
        or embedded.is_unspecified
        or embedded.is_reserved
    )

def _is_non_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return _has_forbidden_address_property(address) or not address.is_global

def _is_rfc2544_proxy_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address in network for network in RFC2544_BENCHMARK_NETS)

def network_url(value: Any, field: str, document_id: str, resolver: Any = None,
                allow_rfc2544_proxy_dns: bool = False) -> str:
    value = https_url(value, field, document_id)
    parsed = urllib.parse.urlparse(value)
    resolver = resolver or socket.getaddrinfo
    try:
        addresses = resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CorpusError(f"{document_id}: cannot resolve {field}: {exc}") from exc
    if not addresses:
        raise CorpusError(f"{document_id}: {field} did not resolve to an address")
    for result in addresses:
        address_text = str(result[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError as exc:
            raise CorpusError(f"{document_id}: {field} resolved to an invalid address") from exc
        proxy_exception = (
            allow_rfc2544_proxy_dns
            and _is_rfc2544_proxy_address(address)
            and not _has_forbidden_address_property(address)
        )
        if _is_non_public_address(address) and not proxy_exception:
            raise CorpusError(f"{document_id}: {field} resolved to a non-public IP address")
    return value

class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, document_id: str, resolver: Any = None,
                 allow_rfc2544_proxy_dns: bool = False):
        super().__init__()
        self.document_id = document_id
        self.resolver = resolver
        self.allow_rfc2544_proxy_dns = allow_rfc2544_proxy_dns

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
        headers: Any, newurl: str) -> Any:
        destination = urllib.parse.urljoin(req.full_url, newurl)
        network_url(
            destination, "redirect URL", self.document_id, self.resolver,
            self.allow_rfc2544_proxy_dns,
        )
        return super().redirect_request(req, fp, code, msg, headers, destination)

def local_path(value: Any, document_id: str, data_dir: Path) -> Path:
    value = required_text(value, "local_path", document_id)
    relative = Path(value)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise CorpusError(f"{document_id}: unsafe local_path")
    for component in relative.parts:
        basename = component.split(".", 1)[0].casefold()
        if (
            ":" in component
            or component.endswith((".", " "))
            or basename in WINDOWS_RESERVED_NAMES
        ):
            raise CorpusError(f"{document_id}: unsafe local_path component")
    if relative.name.lower() in RESERVED_DATA_NAMES:
        raise CorpusError(f"{document_id}: local_path uses a reserved data filename")
    if relative.suffix.lower() != ".pdf":
        raise CorpusError(f"{document_id}: local_path must end in .pdf")
    target = (data_dir / relative).resolve()
    try:
        target.relative_to(data_dir.resolve())
    except ValueError as exc:
        raise CorpusError(f"{document_id}: local_path escapes data/") from exc
    return target

def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = read_json(path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise CorpusError("manifest schema_version must be 1")
    if manifest.get("front_region_schema") != "pdf2md.front-regions.v1":
        raise CorpusError("manifest front_region_schema must be pdf2md.front-regions.v1")
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        raise CorpusError("manifest documents must be a list")
    ids, paths = set(), set()
    for index, item in enumerate(documents):
        if (
            not isinstance(item, dict)
            or REQUIRED_FIELDS - set(item)
            or set(item) - FIELDS
        ):
            raise CorpusError(f"documents[{index}] has missing or unknown fields")
        document_id = item["id"]
        if not isinstance(document_id, str) or not ID_PATTERN.fullmatch(document_id) or document_id in ids:
            raise CorpusError(f"invalid or duplicate document id: {document_id!r}")
        ids.add(document_id)
        for field in TEXT_FIELDS:
            required_text(item[field], field, document_id)
        if item["suite"] not in SUITES:
            raise CorpusError(f"{document_id}: unknown suite {item['suite']!r}")
        if item["license_class"] not in LICENSE_CLASSES:
            raise CorpusError(f"{document_id}: unknown license_class {item['license_class']!r}")
        if not isinstance(item["redistributable"], bool) or not isinstance(item["training_eligible"], bool):
            raise CorpusError(f"{document_id}: license flags must be boolean")
        if item["training_eligible"] and not item["redistributable"]:
            raise CorpusError(f"{document_id}: training_eligible requires redistributable=true")
        regions = item["expected_front_regions"]
        if not isinstance(regions, list):
            raise CorpusError(f"{document_id}: expected_front_regions must be a list")
        if (
            any(not isinstance(region, str) or region not in FRONT_REGION_KINDS for region in regions)
            or len(regions) != len(set(regions))
        ):
            raise CorpusError(
                f"{document_id}: expected_front_regions contains an unknown or duplicate kind"
            )
        expected_sha256, expected_size = item.get("expected_sha256"), item.get("expected_size")
        if (expected_sha256 is None) != (expected_size is None):
            raise CorpusError(f"{document_id}: expected_sha256 and expected_size must be supplied together")
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str)
            or not SHA256_PATTERN.fullmatch(expected_sha256)
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size <= 0
        ):
            raise CorpusError(f"{document_id}: invalid expected PDF digest or size")
        if item["url"] is None:
            if item["source_page"] is not None:
                https_url(item["source_page"], "source_page", document_id)
        else:
            https_url(item["url"], "url", document_id)
            https_url(item["source_page"], "source_page", document_id)
        normalized = os.path.normcase(str(local_path(item["local_path"], document_id, path.parent)))
        if normalized in paths:
            raise CorpusError(f"duplicate local_path: {item['local_path']}")
        paths.add(normalized)
    return manifest

def validate_control_paths(manifest_path: Path, state_path: Path,
                           documents: Sequence[dict[str, Any]]) -> None:
    manifest_path, state_path = manifest_path.resolve(), state_path.resolve()
    data_dir = manifest_path.parent
    for label, path in (("manifest", manifest_path), ("state", state_path)):
        for component in path.parts[1:]:
            basename = component.split(".", 1)[0].casefold()
            if (
                ":" in component
                or component.endswith((".", " "))
                or basename in WINDOWS_RESERVED_NAMES
            ):
                raise CorpusError(f"{label} path contains an unsafe Windows component")
    def key(path: Path) -> str:
        return os.path.normcase(str(path.resolve()))
    manifest_key, state_key = key(manifest_path), key(state_path)
    fixed_controls = {key(data_dir / "corpus.json"), key(data_dir / "README.md")}
    if state_key == manifest_key or state_key in fixed_controls:
        raise CorpusError("state path conflicts with the manifest or a reserved data file")
    controls = [
        ("manifest", manifest_path),
        ("state", state_path),
        ("default manifest", data_dir / "corpus.json"),
        ("corpus README", data_dir / "README.md"),
        ("default state", data_dir / "local-state.json"),
    ]
    protected = {key(path) for _, path in controls}
    payloads: list[tuple[str, Path]] = []
    for item in documents:
        target = local_path(item["local_path"], item["id"], data_dir)
        payloads.extend([
            (f"{item['id']} target", target),
            (f"{item['id']} temporary download", target.with_name(target.name + ".part")),
        ])

    def overlaps(first: Path, second: Path) -> bool:
        first_parts = Path(key(first)).parts
        second_parts = Path(key(second)).parts
        shorter = min(len(first_parts), len(second_parts))
        return first_parts[:shorter] == second_parts[:shorter]

    for payload_label, payload_path in payloads:
        if key(payload_path) in protected:
            raise CorpusError(f"{payload_label} conflicts with a control file")
        for control_label, control_path in controls:
            if overlaps(payload_path, control_path):
                raise CorpusError(
                    f"{payload_label} has a file/directory collision with {control_label}"
                )
    for index, (first_label, first_path) in enumerate(payloads):
        for second_label, second_path in payloads[index + 1:]:
            if overlaps(first_path, second_path):
                raise CorpusError(
                    f"{first_label} has a file/directory collision with {second_label}"
                )

def select(documents: Sequence[dict[str, Any]], suites: Sequence[str], ids: Sequence[str]) -> list[dict[str, Any]]:
    unknown = sorted(set(ids) - {item["id"] for item in documents})
    if unknown:
        raise CorpusError(f"unknown corpus id(s): {', '.join(unknown)}")
    if not suites and not ids:
        return list(documents)
    return [item for item in documents if item["suite"] in set(suites) or item["id"] in set(ids)]

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()

def validate_expected_pdf(item: dict[str, Any], size: int, digest: str) -> None:
    expected_sha256, expected_size = item.get("expected_sha256"), item.get("expected_size")
    if expected_sha256 is None:
        return
    if expected_size != size or expected_sha256 != digest:
        raise CorpusError(f"{item['id']}: PDF differs from the manifest-pinned size or SHA-256")

def is_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as source:
            if not source.read(1024).lstrip().startswith(b"%PDF-"):
                return False
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - 65_536), os.SEEK_SET)
            return b"%%EOF" in source.read()
    except OSError:
        return False

def validate_state(state: Any) -> dict[str, Any]:
    if (
        not isinstance(state, dict)
        or set(state) != {"schema_version", "documents"}
        or type(state.get("schema_version")) is not int
        or state.get("schema_version") != 1
        or not isinstance(state.get("documents"), dict)
    ):
        raise CorpusError("invalid local state")
    for document_id, record in state["documents"].items():
        if not isinstance(document_id, str) or not ID_PATTERN.fullmatch(document_id):
            raise CorpusError("invalid document id in local state")
        if (
            not isinstance(record, dict)
            or STATE_REQUIRED_FIELDS - set(record)
            or set(record) - STATE_FIELDS
        ):
            raise CorpusError(f"{document_id}: invalid local-state record fields")
        digest, size = record.get("sha256"), record.get("size")
        if (
            not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
            or type(size) is not int
            or size <= 0
        ):
            raise CorpusError(f"{document_id}: invalid local-state PDF digest or size")
        resolved_url = record.get("resolved_url")
        if resolved_url is not None:
            https_url(resolved_url, "local-state resolved_url", document_id)
        for field in ("etag", "last_modified", "downloaded_at", "verified_at"):
            value = record.get(field)
            if value is not None and (
                not isinstance(value, str)
                or not value
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise CorpusError(f"{document_id}: invalid local-state {field}")
    return state

def load_state(path: Path) -> dict[str, Any]:
    state = read_json(path, {"schema_version": 1, "documents": {}})
    return validate_state(state)

def validate_recorded_pdf(item: dict[str, Any], target: Path,
                          state: dict[str, Any]) -> tuple[int, str, bool]:
    document_id = item["id"]
    if not target.is_file() or not is_pdf(target):
        raise CorpusError(f"{document_id}: existing target is not a regular PDF")
    size, digest = target.stat().st_size, sha256(target)
    validate_expected_pdf(item, size, digest)
    recorded = state["documents"].get(document_id)
    if item.get("expected_sha256") is not None:
        matches_state = bool(
            isinstance(recorded, dict)
            and recorded.get("size") == size
            and recorded.get("sha256") == digest
        )
        return size, digest, matches_state
    if recorded is None:
        raise CorpusError(f"{document_id}: unpinned existing target has no local-state record")
    if not isinstance(recorded, dict):
        raise CorpusError(f"{document_id}: invalid local-state record")
    if recorded.get("size") != size or recorded.get("sha256") != digest:
        raise CorpusError(f"{document_id}: existing target differs from local-state.json")
    return size, digest, True

def existing_state_metadata(item: dict[str, Any], size: int, digest: str) -> dict[str, Any]:
    return {
        "resolved_url": item.get("url"),
        "etag": None,
        "last_modified": None,
        "sha256": digest,
        "size": size,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }

def download_one(item: dict[str, Any], data_dir: Path, remaining: int,
                 opener: Any = None, resolver: Any = None,
                 allow_rfc2544_proxy_dns: bool = False) -> dict[str, Any]:
    document_id = item["id"]
    url = network_url(
        item["url"], "url", document_id, resolver, allow_rfc2544_proxy_dns,
    )
    target = local_path(item["local_path"], document_id, data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    if target.exists():
        raise CorpusError(f"{document_id}: target already exists")
    if part.exists():
        raise CorpusError(f"{document_id}: temporary download path already exists")
    base_headers = {
        "User-Agent": "PDF2MD-corpus/1",
        "Accept-Encoding": "identity",
    }
    request = urllib.request.Request(url, headers=base_headers)
    if opener is None:
        opener = urllib.request.build_opener(SafeRedirectHandler(
            document_id, resolver, allow_rfc2544_proxy_dns,
        )).open
    digest, size = hashlib.sha256(), 0
    part_owned = False
    try:
        try:
            output = part.open("xb")
            part_owned = True
        except FileExistsError as exc:
            raise CorpusError(
                f"{document_id}: temporary download path was claimed concurrently"
            ) from exc
        with output, opener(request, timeout=60) as response:
            resolved = network_url(
                response.geturl(), "resolved_url", document_id, resolver,
                allow_rfc2544_proxy_dns,
            )
            if response.headers.get("Content-Range"):
                raise CorpusError(f"{document_id}: server returned a partial PDF response")
            encoding = response.headers.get("Content-Encoding")
            if encoding and encoding.casefold() != "identity":
                raise CorpusError(f"{document_id}: compressed PDF responses are not accepted")
            length = response.headers.get("Content-Length")
            announced = int(length) if length else None
            if announced is not None and announced > min(MAX_FILE_BYTES, remaining):
                raise CorpusError(f"{document_id}: announced download exceeds size limit")
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES or size > remaining:
                    raise CorpusError(f"{document_id}: download exceeds size limit")
                output.write(chunk)
                digest.update(chunk)
            metadata = {
                "resolved_url": resolved,
                "etag": response.headers.get("ETag") or None,
                "last_modified": response.headers.get("Last-Modified") or None,
            }
            accepts_ranges = response.headers.get("Accept-Ranges", "").casefold() == "bytes"

        if announced is not None and size != announced:
            if size >= announced or not accepts_ranges:
                raise CorpusError(f"{document_id}: response ended before its announced size")
            validator = metadata["etag"] or metadata["last_modified"]
            if not validator:
                raise CorpusError(
                    f"{document_id}: incomplete response cannot be resumed without a validator"
                )
            resolved = network_url(
                resolved, "resolved_url", document_id, resolver, allow_rfc2544_proxy_dns,
            )
            with part.open("ab") as output:
                while size < announced:
                    end = min(size + CHUNK_SIZE - 1, announced - 1)
                    headers = dict(base_headers)
                    headers.update({"Range": f"bytes={size}-{end}", "If-Range": validator})
                    range_request = urllib.request.Request(resolved, headers=headers)
                    with opener(range_request, timeout=60) as response:
                        range_resolved = network_url(
                            response.geturl(), "range resolved_url", document_id, resolver,
                            allow_rfc2544_proxy_dns,
                        )
                        if range_resolved != resolved:
                            raise CorpusError(
                                f"{document_id}: ranged response changed its resolved URL"
                            )
                        content_range = response.headers.get("Content-Range", "")
                        match = CONTENT_RANGE_PATTERN.fullmatch(content_range.strip())
                        if (
                            match is None
                            or int(match.group("start")) != size
                            or int(match.group("end")) != end
                            or int(match.group("total")) != announced
                        ):
                            raise CorpusError(
                                f"{document_id}: invalid ranged PDF response"
                            )
                        expected = end - size + 1
                        range_length = response.headers.get("Content-Length")
                        if range_length is not None and int(range_length) != expected:
                            raise CorpusError(
                                f"{document_id}: ranged response length is inconsistent"
                            )
                        range_encoding = response.headers.get("Content-Encoding")
                        if range_encoding and range_encoding.casefold() != "identity":
                            raise CorpusError(
                                f"{document_id}: compressed ranged response is not accepted"
                            )
                        for header, expected_value in (
                            ("ETag", metadata["etag"]),
                            ("Last-Modified", metadata["last_modified"]),
                        ):
                            actual = response.headers.get(header)
                            if expected_value and actual != expected_value:
                                raise CorpusError(
                                    f"{document_id}: PDF changed during ranged download"
                                )
                        received = 0
                        while True:
                            chunk = response.read(min(CHUNK_SIZE, expected - received + 1))
                            if not chunk:
                                break
                            received += len(chunk)
                            if received > expected:
                                raise CorpusError(
                                    f"{document_id}: ranged response exceeded its declared interval"
                                )
                            output.write(chunk)
                            digest.update(chunk)
                        if received != expected:
                            raise CorpusError(
                                f"{document_id}: ranged response ended before its interval"
                            )
                        size += received
        if not size or not is_pdf(part):
            raise CorpusError(f"{document_id}: response is not a PDF")
        actual_digest = digest.hexdigest()
        validate_expected_pdf(item, size, actual_digest)
        if target.exists():
            raise CorpusError(f"{document_id}: target appeared during download")
        if os.name == "nt":
            os.rename(part, target)
        else:
            os.link(part, target, follow_symlinks=False)
            part.unlink()
        part_owned = False
    except CorpusError:
        raise
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise CorpusError(f"{document_id}: download failed: {exc}") from exc
    finally:
        if part_owned and part.exists():
            part.unlink()
    metadata.update({"sha256": actual_digest, "size": size,
                     "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")})
    return metadata

def list_command(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    validate_control_paths(args.manifest, args.state, manifest["documents"])
    documents = select(manifest["documents"], args.suite, args.id)
    for item in documents:
        target = local_path(item["local_path"], item["id"], args.manifest.parent)
        if target.exists():
            status = "present"
        elif item["url"] is None:
            status = "local-only" if item["suite"] == "local-existing" else "manual-only"
        else:
            status = "missing"
        print(f"{item['id']}\t{item['suite']}\t{item['language']}\t{item['document_type']}\t{status}\t{item['title']}")
    print(f"{len(documents)} document(s)")
    return 0

def download_command(args: argparse.Namespace) -> int:
    suites = args.suite
    if not suites and not args.id:
        suites = ["smoke"]
    manifest = load_manifest(args.manifest)
    validate_control_paths(args.manifest, args.state, manifest["documents"])
    documents = select(manifest["documents"], suites, args.id)
    unpinned_downloads = [
        item["id"] for item in documents
        if item["url"] is not None
        and item.get("expected_sha256") is None
    ]
    if unpinned_downloads and not args.accept_unpinned:
        raise CorpusError(
            "automatic download requires a manifest pin; use --accept-unpinned for: "
            + ", ".join(unpinned_downloads)
        )
    state = load_state(args.state)
    limit = int(args.max_total_mb * 1024 * 1024)
    if limit <= 0:
        raise CorpusError("--max-total-mb must be positive")
    used = count = 0
    for item in documents:
        target = local_path(item["local_path"], item["id"], args.manifest.parent)
        if target.exists():
            size, digest, recorded = validate_recorded_pdf(item, target, state)
            if not recorded:
                state["documents"][item["id"]] = existing_state_metadata(item, size, digest)
                validate_state(state)
                write_json(args.state, state)
                print(f"recorded {item['id']}: manifest-pinned existing PDF")
                continue
            print(f"skip {item['id']}: verified existing PDF ({size} bytes sha256={digest})")
            continue
        if item["url"] is None:
            print(f"skip {item['id']}: no direct download URL")
            continue
        metadata = download_one(
            item, args.manifest.parent, limit - used,
            allow_rfc2544_proxy_dns=args.allow_rfc2544_proxy_dns,
        )
        used += metadata["size"]
        state["documents"][item["id"]] = metadata
        validate_state(state)
        write_json(args.state, state)
        count += 1
        print(f"downloaded {item['id']}: {metadata['size']} bytes")
    print(f"downloaded {count} document(s), {used} bytes")
    return 0

def verify_command(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    validate_control_paths(args.manifest, args.state, manifest["documents"])
    documents = select(manifest["documents"], args.suite, args.id)
    state, failures, checked = load_state(args.state), 0, 0
    for item in documents:
        target = local_path(item["local_path"], item["id"], args.manifest.parent)
        if not target.exists():
            print(f"missing {item['id']}: {item['local_path']}")
            failures += 1
            continue
        checked += 1
        if item.get("expected_sha256") is None and not args.accept_unpinned:
            print(
                f"invalid {item['id']}: manifest has no pinned size/SHA-256 "
                "(use --accept-unpinned to verify against local-state.json)"
            )
            failures += 1
            continue
        try:
            size, digest, _ = validate_recorded_pdf(item, target, state)
        except CorpusError as exc:
            print(f"invalid {item['id']}: {exc}")
            failures += 1
            continue
        print(f"ok {item['id']}: {size} bytes sha256={digest}")
    print(f"checked {checked} document(s); {failures} failure(s)")
    return bool(failures)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the optional PDF2MD regression corpus")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--state", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    def filters(command: argparse.ArgumentParser) -> None:
        command.add_argument("--suite", action="append", default=[])
        command.add_argument("--id", action="append", default=[])
    listing = commands.add_parser("list"); filters(listing); listing.set_defaults(func=list_command)
    download = commands.add_parser("download"); filters(download)
    download.add_argument("--max-total-mb", type=float, default=1024.0)
    download.add_argument("--accept-unpinned", action="store_true")
    download.add_argument(
        "--allow-rfc2544-proxy-dns", action="store_true",
        help=(
            "allow DNS hostnames to resolve through the RFC 2544 benchmark range "
            "used by transparent proxies; literal/private/local IP URLs remain blocked"
        ),
    )
    download.set_defaults(func=download_command)
    verify = commands.add_parser("verify"); filters(verify)
    verify.add_argument("--accept-unpinned", action="store_true")
    verify.set_defaults(func=verify_command)
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.manifest = args.manifest.resolve()
    args.state = (
        args.state.resolve()
        if args.state is not None
        else (args.manifest.parent / "local-state.json").resolve()
    )
    try:
        return args.func(args)
    except CorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
