from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests
from pypdf import PdfReader

from pdf2md_markdown import convert_html_tables
from pdf2md_toc import enhance_document_navigation


CORE_VERSION = "2.7.0"
# Keep compatibility with selections created by the 2.0 core. Public-output
# filtering does not change OCR content, so those expensive results remain valid.
CACHE_VERSION = "2.0.0"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"}
PROFILE_SETTINGS = {
    "fast": {"backend": "pipeline", "effort": "medium", "image_analysis": False},
    "balanced": {"backend": "hybrid-engine", "effort": "medium", "image_analysis": False},
    "accurate": {"backend": "hybrid-engine", "effort": "high", "image_analysis": True},
}

EventCallback = Callable[[str, object], None]
ENGINE_PROGRESS_RE = re.compile(
    r"Processing pages.*?(\d{1,3})%.*?(\d+)\s*/\s*(\d+)",
    re.IGNORECASE,
)


class ConversionError(RuntimeError):
    pass


class ConversionCancelled(ConversionError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    runtime: Path
    environment: Path
    python: Path
    engine: Path
    config: Path
    cuda: Path
    cache: Path
    models: Path
    modelscope: Path
    huggingface: Path
    temp: Path
    condarc: Path


@dataclass(frozen=True, slots=True)
class OutputLayout:
    root: Path
    markdown: Path
    images: Path
    raw: Path
    cache: Path
    cached_images: Path
    selections: Path
    logs: Path
    manifest: Path


@dataclass(slots=True)
class ConversionOptions:
    source: Path
    output: Path | None = None
    pages: str | None = None
    profile: str = "balanced"
    method: str = "auto"
    language: str = "ch"
    force: bool = False
    timeout: int = 1800


@dataclass(slots=True)
class RunResult:
    markdown: Path
    images: Path
    output: Path
    pages: str
    profile: str
    cache: str
    elapsed_seconds: float


def _noop_emit(_kind: str, _value: object) -> None:
    return


def _emit_progress(emit: EventCallback, percent: float, message: str) -> None:
    emit(
        "progress",
        {
            "percent": max(0, min(100, round(float(percent), 1))),
            "message": message,
        },
    )


def parse_engine_progress(line: str) -> tuple[int, int, int] | None:
    """Return MinerU's page-stage percentage and page counts from a tqdm line."""
    match = ENGINE_PROGRESS_RE.search(line)
    if not match:
        return None
    percent, completed, total = (int(value) for value in match.groups())
    if total < 1 or completed < 0:
        return None
    return max(0, min(100, percent)), min(completed, total), total


def project_root() -> Path:
    if getattr(__import__("sys"), "frozen", False):
        return Path(__import__("sys").executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def runtime_paths() -> RuntimePaths:
    root = project_root()
    runtime = root / "runtime"
    environment = runtime / "env"
    models = root / "models"
    return RuntimePaths(
        root=root,
        runtime=runtime,
        environment=environment,
        python=environment / "python.exe",
        engine=root / "src" / "pdf2md_engine.py",
        config=runtime / "pdf2md.json",
        cuda=runtime / "cuda",
        cache=runtime / "cache",
        models=models,
        modelscope=models / "modelscope",
        huggingface=models / "huggingface",
        temp=runtime / "temp",
        condarc=root / "config" / "condarc",
    )


def validate_runtime() -> RuntimePaths:
    paths = runtime_paths()
    legacy_config = paths.runtime / "mineru.json"
    if not paths.config.exists() and legacy_config.is_file():
        legacy_config.replace(paths.config)
    required = (paths.python, paths.engine, paths.config, paths.cuda, paths.condarc)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        details = "\n".join(f"- {path}" for path in missing)
        raise ConversionError(f"PDF2MD 本地运行环境不完整：\n{details}\n\n请运行 scripts\\install.ps1。")
    for directory in (
        paths.cache,
        paths.cache / "pip",
        paths.cache / "uv",
        paths.cache / "torch",
        paths.cache / "matplotlib",
        paths.cache / "numba",
        paths.modelscope,
        paths.huggingface,
        paths.temp,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    _repair_engine_config(paths)
    return paths


def _repair_engine_config(paths: RuntimePaths) -> None:
    try:
        payload = json.loads(paths.config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    model_dirs = payload.get("models-dir")
    if not isinstance(model_dirs, dict):
        return
    changed = False
    for key, value in tuple(model_dirs.items()):
        if not isinstance(value, str) or not value or Path(value).exists():
            continue
        parts = Path(value).parts
        lower_parts = [part.lower() for part in parts]
        if "models" not in lower_parts:
            continue
        relative = parts[lower_parts.index("models") + 1 :]
        candidate = paths.models.joinpath(*relative)
        if candidate.exists():
            model_dirs[key] = str(candidate)
            changed = True
    if changed:
        temporary = paths.config.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(paths.config)


def build_runtime_env(paths: RuntimePaths, session_temp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "CONDARC": str(paths.condarc),
            "CONDA_PKGS_DIRS": str(paths.runtime / "conda-pkgs"),
            "PIP_CACHE_DIR": str(paths.cache / "pip"),
            "UV_CACHE_DIR": str(paths.cache / "uv"),
            "XDG_CACHE_HOME": str(paths.cache),
            "HF_HOME": str(paths.huggingface),
            "HUGGINGFACE_HUB_CACHE": str(paths.huggingface / "hub"),
            "MODELSCOPE_CACHE": str(paths.modelscope),
            "TORCH_HOME": str(paths.cache / "torch"),
            "CUDA_PATH": str(paths.cuda),
            "MPLCONFIGDIR": str(paths.cache / "matplotlib"),
            "NUMBA_CACHE_DIR": str(paths.cache / "numba"),
            "MINERU_TOOLS_CONFIG_JSON": str(paths.config),
            "MINERU_MODEL_SOURCE": "local",
            "MINERU_API_OUTPUT_ROOT": str(session_temp / "server-output"),
            "MINERU_API_ENABLE_FASTAPI_DOCS": "0",
            "MINERU_API_MAX_CONCURRENT_REQUESTS": "1",
            "MINERU_API_TASK_RETENTION_SECONDS": "120",
            "MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS": "30",
            "PDF2MD_ROOT": str(paths.root),
            "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
            "TOKENIZERS_PARALLELISM": "false",
            "TEMP": str(session_temp / "temp"),
            "TMP": str(session_temp / "temp"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    env["PATH"] = os.pathsep.join(
        (
            str(paths.environment),
            str(paths.environment / "Scripts"),
            str(paths.environment / "Library" / "bin"),
            str(paths.cuda / "bin"),
            env.get("PATH", ""),
        )
    )
    return env


def default_output_for(source: Path) -> Path:
    return source.parent / f"{source.stem}.pdf2md"


def output_layout(source: Path, output: Path | None = None) -> OutputLayout:
    root = (output or default_output_for(source)).expanduser().resolve()
    if output is None and not root.exists():
        legacy = source.parent / f"{source.stem}.mineru"
        if legacy.is_dir():
            legacy.replace(root)
    raw = root / "raw"
    cache = raw / "cache"
    return OutputLayout(
        root=root,
        markdown=root / f"{source.stem}.md",
        images=root / "images",
        raw=raw,
        cache=cache,
        cached_images=cache / "images",
        selections=cache / "selections",
        logs=raw / "logs",
        manifest=raw / "manifest.json",
    )


def ensure_layout(layout: OutputLayout) -> None:
    for directory in (
        layout.root,
        layout.raw,
        layout.cache,
        layout.cached_images,
        layout.selections,
        layout.logs,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def parse_page_ranges(expression: str | None) -> list[tuple[int, int | None]]:
    if expression is None or not expression.strip():
        return [(1, None)]
    cleaned = (
        expression.strip()
        .replace("，", ",")
        .replace("、", ",")
        .replace("；", ",")
        .replace(";", ",")
        .replace("–", "-")
        .replace("—", "-")
    )
    if cleaned.casefold() in {"all", "全文"}:
        return [(1, None)]
    ranges: list[tuple[int, int]] = []
    for part in cleaned.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not match:
            raise ConversionError("页码格式应为 3、3-8 或 1-3,8,12-15。")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise ConversionError("PDF 页码从 1 开始，结束页不能小于起始页。")
        ranges.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [(start, end) for start, end in merged]


def ranges_text(ranges: list[tuple[int, int | None]]) -> str:
    if ranges == [(1, None)]:
        return "all"
    return ",".join(str(start) if start == end else f"{start}-{end}" for start, end in ranges)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _source_identity(source: Path, layout: OutputLayout) -> dict[str, object]:
    stat = source.stat()
    previous = _read_json(layout.manifest) or {}
    previous_source = previous.get("source")
    if isinstance(previous_source, dict):
        if (
            previous_source.get("size") == stat.st_size
            and previous_source.get("mtime_ns") == stat.st_mtime_ns
            and isinstance(previous_source.get("sha256"), str)
        ):
            return dict(previous_source)
    return {
        "path": str(source),
        "name": source.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _file_sha256(source),
    }


def _load_manifest(source: Path, layout: OutputLayout, identity: dict[str, object]) -> dict[str, object]:
    manifest = _read_json(layout.manifest)
    if not manifest or not isinstance(manifest.get("source"), dict):
        return {"schema_version": 2, "source": identity, "selections": []}
    old_source = manifest["source"]
    assert isinstance(old_source, dict)
    if old_source.get("sha256") != identity.get("sha256"):
        if layout.cache.exists():
            shutil.rmtree(layout.cache)
        layout.cached_images.mkdir(parents=True, exist_ok=True)
        layout.selections.mkdir(parents=True, exist_ok=True)
        return {"schema_version": 2, "source": identity, "selections": []}
    manifest["source"] = identity
    if not isinstance(manifest.get("selections"), list):
        manifest["selections"] = []
    return manifest


def _task_key(identity: dict[str, object], page_range: str, options: ConversionOptions) -> str:
    material = "|".join(
        (
            str(identity["sha256"]),
            page_range,
            options.profile,
            options.method,
            options.language,
            CACHE_VERSION,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _safe_extract(zip_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    output_root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (output_root / member.filename).resolve()
            try:
                target.relative_to(output_root)
            except ValueError as exc:
                raise ConversionError(f"OCR 引擎返回了不安全的 ZIP 路径：{member.filename}") from exc
        archive.extractall(output_root)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    else:
        process.terminate()


class OCRService:
    def __init__(self, paths: RuntimePaths, emit: EventCallback, cancel_event: threading.Event) -> None:
        self.paths = paths
        self.emit = emit
        self.cancel_event = cancel_event
        self.process: subprocess.Popen[str] | None = None
        self.session = requests.Session()
        self.session.trust_env = False
        self.base_url = ""
        self.session_root = paths.temp / "api" / f"session-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.client_root = self.session_root / "client"
        self.log_lines: list[str] = []
        self.progress_start = 20.0
        self.progress_end = 88.0
        self.progress_value = 20.0

    def set_progress_window(self, start: float, end: float) -> None:
        self.progress_start = start
        self.progress_end = max(start, end)
        self.progress_value = start

    def _handle_log_line(self, raw_line: str) -> None:
        line = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", raw_line).strip()
        if not line:
            return
        self.log_lines.append(line)
        if len(self.log_lines) > 4000:
            del self.log_lines[:1000]
        parsed = parse_engine_progress(line)
        if parsed is not None:
            percent, completed, total = parsed
            mapped = self.progress_start + (self.progress_end - self.progress_start) * percent / 100
            if mapped > self.progress_value:
                self.progress_value = mapped
                _emit_progress(self.emit, mapped, f"解析页面 {completed}/{total}")
        self.emit("line", line)

    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _read_logs(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        buffer: list[str] = []
        while True:
            character = self.process.stdout.read(1)
            if not character:
                if buffer:
                    self._handle_log_line("".join(buffer))
                return
            if character in "\r\n":
                if buffer:
                    self._handle_log_line("".join(buffer))
                    buffer.clear()
                continue
            buffer.append(character)

    def start(self, timeout: float = 120.0) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.session_root.mkdir(parents=True, exist_ok=True)
        self.client_root.mkdir(parents=True, exist_ok=True)
        (self.session_root / "temp").mkdir(parents=True, exist_ok=True)
        port = self._free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        command = [
            str(self.paths.python),
            str(self.paths.engine),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--enable-vlm-preload",
            "false",
        ]
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        self.emit("message", "启动引擎")
        self.process = subprocess.Popen(
            command,
            cwd=str(self.paths.root),
            env=build_runtime_env(self.paths, self.session_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
        )
        log_reader = threading.Thread(target=self._read_logs, daemon=True)
        log_reader.start()
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            self._check_cancelled()
            if self.process.poll() is not None:
                # Drain the final traceback before constructing the
                # user-facing startup error.
                log_reader.join(timeout=1)
                tail = "\n".join(self.log_lines[-20:])
                details = f"\n{tail}" if tail else ""
                raise ConversionError(
                    f"PDF2MD OCR 引擎启动失败（退出码 {self.process.returncode}）。{details}"
                )
            try:
                response = self.session.get(f"{self.base_url}/health", timeout=3)
                if response.status_code == 200:
                    self.emit("message", "引擎就绪")
                    return
                last_error = f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                last_error = str(exc)
            time.sleep(0.5)
        raise ConversionError(f"等待 PDF2MD OCR 引擎启动超时。{last_error}")

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise ConversionCancelled("转换已取消。")

    def convert_range(
        self,
        source: Path,
        start: int,
        end: int | None,
        options: ConversionOptions,
        deadline: float,
    ) -> Path:
        self._check_cancelled()
        profile = PROFILE_SETTINGS[options.profile]
        page_label = "all" if end is None else (str(start) if start == end else f"{start}-{end}")
        self.emit("message", f"准备页面 {page_label}")
        form = [
            ("lang_list", options.language),
            ("backend", str(profile["backend"])),
            ("effort", str(profile["effort"])),
            ("parse_method", options.method),
            ("formula_enable", "true"),
            ("table_enable", "true"),
            ("image_analysis", str(profile["image_analysis"]).lower()),
            ("return_md", "true"),
            ("return_middle_json", "false"),
            ("return_model_output", "false"),
            ("return_content_list", "false"),
            ("return_images", "true"),
            ("response_format_zip", "true"),
            ("return_original_file", "false"),
            ("client_side_output_generation", "false"),
            ("start_page_id", str(start - 1)),
            ("end_page_id", str(99999 if end is None else end - 1)),
        ]
        try:
            with source.open("rb") as handle:
                response = self.session.post(
                    f"{self.base_url}/tasks",
                    data=form,
                    files={"files": (source.name, handle, "application/pdf")},
                    timeout=(30, min(300, max(30, deadline - time.monotonic()))),
                )
        except requests.RequestException as exc:
            raise ConversionError(f"提交 OCR 任务失败：{exc}") from exc
        if response.status_code != 202:
            raise ConversionError(f"OCR 引擎拒绝任务：HTTP {response.status_code} {response.text[-1000:]}")
        try:
            payload = response.json()
            task_id = str(payload["task_id"])
            status_url = str(payload["status_url"])
            result_url = str(payload["result_url"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ConversionError("OCR 引擎返回了无效的任务信息。") from exc

        last_status = ""
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                status_response = self.session.get(status_url, timeout=20)
                status_response.raise_for_status()
                status_payload = status_response.json()
            except (requests.RequestException, ValueError) as exc:
                self.emit("line", f"状态查询暂时失败，将重试：{exc}")
                time.sleep(1)
                continue
            status = str(status_payload.get("status", ""))
            if status != last_status:
                last_status = status
                if status == "pending":
                    self.emit("message", "等待解析")
                elif status == "processing":
                    self.emit("message", f"解析页面 {page_label}")
            if status == "completed":
                break
            if status not in {"pending", "processing"}:
                raise ConversionError(
                    "OCR 任务失败：" + json.dumps(status_payload, ensure_ascii=False)[-1500:]
                )
            time.sleep(1)
        else:
            raise ConversionError(f"PDF2MD 转换超时（{options.timeout} 秒）。")

        zip_path = self.client_root / f"{task_id}.zip"
        extract_dir = self.client_root / task_id
        try:
            with self.session.get(result_url, stream=True, timeout=(30, 300)) as result:
                result.raise_for_status()
                with zip_path.open("wb") as stream:
                    for chunk in result.iter_content(1024 * 1024):
                        self._check_cancelled()
                        if chunk:
                            stream.write(chunk)
        except requests.RequestException as exc:
            raise ConversionError(f"下载 OCR 结果失败：{exc}") from exc
        _safe_extract(zip_path, extract_dir)
        zip_path.unlink(missing_ok=True)
        _emit_progress(self.emit, self.progress_end, "读取结果")
        return extract_dir

    def stop(self) -> None:
        self.session.close()
        if self.process is not None:
            _terminate_process_tree(self.process)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        if self.session_root.exists():
            shutil.rmtree(self.session_root, ignore_errors=True)


def _rewrite_image_links(content: str, mappings: dict[str, str]) -> str:
    if not mappings:
        return content

    def published_path(value: str) -> str | None:
        normalized = value.strip().strip("<>").replace("\\", "/")
        return mappings.get(normalized.lower()) or mappings.get(Path(normalized).name.lower())

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
        return match.group(0) if not replacement else f"{match.group(1)}{replacement}{suffix}{match.group(3)}"

    def replace_html(match: re.Match[str]) -> str:
        replacement = published_path(match.group(2))
        return match.group(0) if not replacement else f"{match.group(1)}{replacement}{match.group(3)}"

    content = re.sub(r"(!\[[^\]]*\]\()([^)]+)(\))", replace_markdown, content)
    return re.sub(r"(<img\b[^>]*?\bsrc=[\"'])([^\"']+)([\"'])", replace_html, content, flags=re.I)


def _referenced_image_names(content: str) -> list[str]:
    references: set[str] = set()
    patterns = (
        r"!\[[^\]]*\]\(\s*<?images/([^\s\)>]+)",
        r"<img\b[^>]*?\bsrc=[\"']images/([^\"']+)[\"']",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, content, flags=re.I):
            name = Path(match.group(1).replace("\\", "/")).name
            if name:
                references.add(name)
    return sorted(references)


def _referenced_image_names_in_order(content: str) -> list[str]:
    matches: list[tuple[int, str]] = []
    patterns = (
        r"!\[[^\]]*\]\(\s*<?images/([^\s\)>]+)",
        r"<img\b[^>]*?\bsrc=[\"']images/([^\"']+)[\"']",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, content, flags=re.I):
            name = Path(match.group(1).replace("\\", "/")).name
            if name:
                matches.append((match.start(), name))
    ordered: list[str] = []
    seen: set[str] = set()
    for _position, name in sorted(matches, key=lambda item: item[0]):
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(name)
    return ordered


def _read_extracted_markdown(extracted: Path, source: Path, page_range: str) -> tuple[Path, str]:
    candidates = sorted(extracted.rglob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        raise ConversionError(f"OCR 引擎没有返回页 {page_range} 的 Markdown。")
    markdown = next((item for item in candidates if item.stem == source.stem), candidates[0])
    try:
        content = markdown.read_text(encoding="utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ConversionError(
            f"OCR 引擎返回的页 {page_range} Markdown 不是有效 UTF-8（字节 {exc.start}）。"
        ) from exc
    return markdown, content


def _replacement_character_count(extracted: Path, source: Path, page_range: str) -> int:
    _markdown, content = _read_extracted_markdown(extracted, source, page_range)
    return content.count("\ufffd")


def _cache_selection(
    extracted: Path,
    layout: OutputLayout,
    task_key: str,
    source: Path,
    page_range: str,
    options: ConversionOptions,
    actual_method: str | None = None,
) -> dict[str, object]:
    markdown, content = _read_extracted_markdown(extracted, source, page_range)
    mappings: dict[str, str] = {}
    cached_image_names: list[str] = []
    for image in extracted.rglob("*"):
        if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        digest = _file_sha256(image)[:20]
        name = f"{digest}{image.suffix.lower()}"
        target = layout.cached_images / name
        if not target.exists():
            shutil.copy2(image, target)
        cached_image_names.append(name)
        published = f"images/{name}"
        candidates = {image.name, image.relative_to(extracted).as_posix()}
        try:
            candidates.add(image.relative_to(markdown.parent).as_posix())
        except ValueError:
            pass
        for candidate in candidates:
            mappings[candidate.replace("\\", "/").lower()] = published
    content = _rewrite_image_links(content, mappings)
    referenced_images = _referenced_image_names(content)
    selection_path = layout.selections / f"{task_key}.md"
    selection_path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return {
        "task_key": task_key,
        "pages": page_range,
        "profile": options.profile,
        "method": actual_method or options.method,
        "requested_method": options.method,
        "language": options.language,
        "selection": selection_path.relative_to(layout.root).as_posix(),
        "images": referenced_images,
        "cached_images": sorted(set(cached_image_names)),
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _cached_selection(
    manifest: dict[str, object], layout: OutputLayout, task_key: str
) -> tuple[dict[str, object] | None, int]:
    selections = manifest.get("selections")
    if not isinstance(selections, list):
        return None, 0
    for item in selections:
        if not isinstance(item, dict) or item.get("task_key") != task_key:
            continue
        selection = layout.root / str(item.get("selection", ""))
        if not selection.is_file():
            return None, 0
        try:
            content = selection.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            return None, 1
        replacement_count = content.count("\ufffd")
        if replacement_count:
            return None, replacement_count
        images = _referenced_image_names(content)
        if any(not (layout.cached_images / name).is_file() for name in images):
            return None, 0
        cached_item = dict(item)
        cached_item["images"] = images
        return cached_item, 0
    return None, 0


def _update_manifest_selection(manifest: dict[str, object], item: dict[str, object]) -> None:
    selections = manifest.setdefault("selections", [])
    assert isinstance(selections, list)
    selections[:] = [
        existing
        for existing in selections
        if not isinstance(existing, dict) or existing.get("task_key") != item["task_key"]
    ]
    selections.append(item)


def _publish_document(
    layout: OutputLayout,
    selected: list[dict[str, object]],
    source: Path | None = None,
    refresh_frontmatter: bool = False,
) -> None:
    if layout.images.exists():
        shutil.rmtree(layout.images)
    layout.images.mkdir(parents=True, exist_ok=True)
    pieces: list[str] = []
    multiple = len(selected) > 1
    for item in selected:
        selection = layout.root / str(item["selection"])
        content = selection.read_text(encoding="utf-8", errors="replace").strip()
        if multiple:
            pieces.append(f"## PDF pages {item['pages']}\n\n{content}")
        else:
            pieces.append(content)
    content = "\n\n".join(pieces).rstrip() + "\n"
    content = convert_html_tables(content)
    image_mappings: dict[str, str] = {}
    for index, name in enumerate(_referenced_image_names_in_order(content), start=1):
        suffix = Path(name).suffix.lower()
        public_name = f"{index}{suffix}"
        public_path = f"images/{public_name}"
        image_mappings[name.casefold()] = public_path
        image_mappings[f"images/{name}".casefold()] = public_path
        source_image = layout.cached_images / name
        target_image = layout.images / public_name
        try:
            os.link(source_image, target_image)
        except OSError:
            shutil.copy2(source_image, target_image)
    content = _rewrite_image_links(content, image_mappings)
    full_document = (
        len(selected) == 1
        and str(selected[0].get("pages", "")).casefold() == "all"
    )
    content = enhance_document_navigation(
        content,
        source=source if full_document else None,
        frontmatter_cache=(layout.cache / "frontmatter-v6.json") if full_document else None,
        force_frontmatter=refresh_frontmatter and full_document,
    )
    temporary = layout.markdown.with_suffix(".md.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(layout.markdown)


def run_conversion(
    options: ConversionOptions,
    emit: EventCallback = _noop_emit,
    cancel_event: threading.Event | None = None,
) -> RunResult:
    started = time.monotonic()
    _emit_progress(emit, 2, "检查文件")
    cancel_event = cancel_event or threading.Event()
    source = options.source.expanduser().resolve()
    if not source.is_file():
        raise ConversionError(f"找不到输入 PDF：{source}")
    if source.suffix.lower() != ".pdf":
        raise ConversionError("当前精简版仅支持 PDF → Markdown。")
    if options.profile not in PROFILE_SETTINGS:
        raise ConversionError(f"未知转换模式：{options.profile}")
    if options.method not in {"auto", "txt", "ocr"}:
        raise ConversionError(f"未知解析方法：{options.method}")
    if options.timeout < 1:
        raise ConversionError("超时时间必须大于 0 秒。")

    paths = validate_runtime()
    ranges = parse_page_ranges(options.pages)
    if ranges != [(1, None)]:
        try:
            page_count = len(PdfReader(str(source)).pages)
        except Exception as exc:
            raise ConversionError(f"无法读取 PDF 页数：{exc}") from exc
        last_page = max(int(end or start) for start, end in ranges)
        if last_page > page_count:
            raise ConversionError(f"页码 {last_page} 超出 PDF 总页数 {page_count}。")
        if ranges == [(1, page_count)]:
            ranges = [(1, None)]
    selected_pages = ranges_text(ranges)
    layout = output_layout(source, options.output)
    if layout.root == source or source in layout.root.parents:
        raise ConversionError("输出目录不能覆盖输入 PDF。")
    ensure_layout(layout)
    identity = _source_identity(source, layout)
    manifest = _load_manifest(source, layout, identity)
    _emit_progress(emit, 7, "检查缓存")
    deadline = started + options.timeout
    service: OCRService | None = None
    completed: list[dict[str, object]] = []
    cache_hits = 0

    try:
        for index, (start, end) in enumerate(ranges, start=1):
            if cancel_event.is_set():
                raise ConversionCancelled("转换已取消。")
            page_range = "all" if end is None else (str(start) if start == end else f"{start}-{end}")
            key = _task_key(identity, page_range, options)
            cached, cached_replacements = (
                (None, 0) if options.force else _cached_selection(manifest, layout, key)
            )
            task_start = 20 + (index - 1) * 70 / len(ranges)
            task_end = 20 + index * 70 / len(ranges)
            if cached is not None:
                cache_hits += 1
                emit("message", f"读取缓存 {page_range}")
                completed.append(cached)
                _emit_progress(emit, task_end, "读取缓存")
                continue
            if service is None:
                service = OCRService(paths, emit, cancel_event)
                _emit_progress(emit, 10, "启动引擎")
                service.start(timeout=min(120, max(10, deadline - time.monotonic())))
                _emit_progress(emit, 18, "引擎就绪")
            service.set_progress_window(task_start, task_end - 2)
            _emit_progress(emit, task_start, f"准备页面 {page_range}")
            if cached_replacements:
                emit(
                    "message",
                    f"缓存页 {page_range} 含 {cached_replacements} 个损坏字符，正在用局部修复引擎重建…",
                )
            extracted = service.convert_range(source, start, end, options, deadline)
            replacement_count = _replacement_character_count(extracted, source, page_range)
            if replacement_count:
                raise ConversionError(
                    f"页 {page_range} 在 span 级局部修复后仍有 {replacement_count} 个无法识别的字符；"
                    "已停止发布。可针对该小页段显式使用 --ocr。"
                )
            item = _cache_selection(
                extracted,
                layout,
                key,
                source,
                page_range,
                options,
                actual_method=options.method,
            )
            _update_manifest_selection(manifest, item)
            _write_json(layout.manifest, manifest)
            completed.append(item)
    finally:
        if service is not None:
            logs = "\n".join(service.log_lines).rstrip()
            service.stop()
            if logs:
                (layout.logs / "last-run.log").write_text(logs + "\n", encoding="utf-8")

    if not completed:
        raise ConversionError("没有可发布的 Markdown 结果。")
    _write_json(layout.manifest, manifest)
    emit("message", "整理输出")
    _emit_progress(emit, 94, "整理输出")
    _publish_document(
        layout,
        completed,
        source=source,
        refresh_frontmatter=options.force,
    )
    elapsed = time.monotonic() - started
    emit("message", "完成")
    _emit_progress(emit, 100, "完成")
    return RunResult(
        markdown=layout.markdown,
        images=layout.images,
        output=layout.root,
        pages=selected_pages,
        profile=options.profile,
        cache="hit" if cache_hits == len(ranges) else "updated",
        elapsed_seconds=elapsed,
    )
