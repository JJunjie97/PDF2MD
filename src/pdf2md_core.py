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
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests
from pypdf import PdfReader

from pdf2md_markdown import convert_html_tables
from pdf2md_region_cascade import SCHEMA as FRONT_REGIONS_V2_SCHEMA
from pdf2md_region_cascade import classifier_fingerprint
from pdf2md_region_cascade import classify_front_regions_v2
from pdf2md_region_cascade import project_front_regions_v1
from pdf2md_toc import enhance_document_navigation


CORE_VERSION = "2.8.0"
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
    content_lists: Path
    front_regions: Path
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
        content_lists=cache / "content-lists",
        front_regions=cache / "front-regions-v1.json",
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
        layout.content_lists,
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
        layout.content_lists.mkdir(parents=True, exist_ok=True)
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

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

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

    def preload(
        self,
        profile: str,
        method: str,
        language: str,
        timeout: float = 600.0,
    ) -> dict[str, object]:
        """Load the selected backend once and retain it in the API process."""
        self._check_cancelled()
        if not self.running:
            raise ConversionError("PDF2MD OCR 引擎尚未启动，无法预热模型。")
        if profile not in PROFILE_SETTINGS:
            raise ConversionError(f"未知转换模式：{profile}")
        backend = str(PROFILE_SETTINGS[profile]["backend"])
        self.emit("message", f"加载模型 {profile}")
        try:
            response = self.session.post(
                f"{self.base_url}/pdf2md/preload",
                json={
                    "backend": backend,
                    "method": method,
                    "language": language,
                },
                timeout=(30, max(30, timeout)),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ConversionError(f"PDF2MD 模型预热失败：{exc}") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise ConversionError("PDF2MD 模型预热返回了无效结果。")
        self.emit("message", "模型已加载")
        return payload

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
            ("return_content_list", "true"),
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
        process = self.process
        self.process = None
        if process is not None:
            _terminate_process_tree(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.session.close()
        if self.session_root.exists():
            shutil.rmtree(self.session_root, ignore_errors=True)


class ConversionSession:
    """Foreground OCR session that keeps one model process alive across PDFs."""

    def __init__(
        self,
        *,
        profile: str = "balanced",
        method: str = "auto",
        language: str = "ch",
        emit: EventCallback = _noop_emit,
        cancel_event: threading.Event | None = None,
        preload_model: bool = True,
        startup_timeout: float = 600.0,
    ) -> None:
        if profile not in PROFILE_SETTINGS:
            raise ConversionError(f"未知转换模式：{profile}")
        if method not in {"auto", "txt", "ocr"}:
            raise ConversionError(f"未知解析方法：{method}")
        self.profile = profile
        self.method = method
        self.language = language
        self.emit = emit
        self.cancel_event = cancel_event or threading.Event()
        self.preload_model = preload_model
        self.startup_timeout = startup_timeout
        self.service: OCRService | None = None
        self.preload_result: dict[str, object] | None = None

    @property
    def running(self) -> bool:
        return self.service is not None and self.service.running

    def start(self) -> "ConversionSession":
        if self.running:
            return self
        stale_service = self.service
        self.service = None
        if stale_service is not None:
            stale_service.stop()
        paths = validate_runtime()
        service = OCRService(paths, self.emit, self.cancel_event)
        try:
            service.start(timeout=self.startup_timeout)
            if self.preload_model:
                self.preload_result = service.preload(
                    self.profile,
                    self.method,
                    self.language,
                    timeout=self.startup_timeout,
                )
        except Exception:
            service.stop()
            raise
        self.service = service
        return self

    def convert(self, options: ConversionOptions) -> RunResult:
        if (
            options.profile != self.profile
            or options.method != self.method
            or options.language != self.language
        ):
            raise ConversionError(
                "同一 OCR 会话必须固定转换模式、解析方式和 OCR 语言，"
                "以避免重复加载多套模型。"
            )
        self.start()
        assert self.service is not None
        return run_conversion(
            options,
            emit=self.emit,
            cancel_event=self.cancel_event,
            ocr_service=self.service,
        )

    def close(self) -> None:
        service = self.service
        self.service = None
        if service is not None:
            try:
                self.emit("message", "释放模型")
            except Exception:
                pass
            try:
                service.stop()
            finally:
                try:
                    self.emit("message", "模型已释放")
                except Exception:
                    pass

    def __enter__(self) -> "ConversionSession":
        return self.start()

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


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
    item: dict[str, object] = {
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
    content_lists = sorted(extracted.rglob("*_content_list_v2.json"))
    if content_lists:
        try:
            payload = json.loads(content_lists[0].read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            payload = None
        if isinstance(payload, list):
            content_list_path = layout.content_lists / f"{task_key}.json"
            temporary = content_list_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            temporary.replace(content_list_path)
            item["content_list_v2"] = content_list_path.relative_to(layout.root).as_posix()
    return item


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
        content_list_value = cached_item.get("content_list_v2")
        if isinstance(content_list_value, str):
            content_list_path = layout.root / content_list_value
            if not content_list_path.is_file():
                cached_item.pop("content_list_v2", None)
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


def _native_recovery_pages(
    report: Mapping[str, object],
) -> list[dict[str, object]]:
    """Keep only explicit damaged-navigation signals; never copy page text."""
    recovery: list[dict[str, object]] = []
    raw_pages = report.get("pages")
    if not isinstance(raw_pages, list):
        return recovery
    navigation_kinds = {"contents", "list_of_figures", "list_of_tables"}
    for raw_page in raw_pages[:64]:
        if (
            not isinstance(raw_page, Mapping)
            or raw_page.get("accepted") is not False
        ):
            continue
        page = raw_page.get("page")
        strength = raw_page.get("rule_strength")
        candidates = raw_page.get("top_candidates")
        evidence = raw_page.get("evidence")
        if (
            not isinstance(page, int)
            or isinstance(page, bool)
            or page < 1
            or not isinstance(strength, (int, float))
            or isinstance(strength, bool)
            or float(strength) < 0.60
            or not isinstance(candidates, list)
            or not candidates
            or not isinstance(candidates[0], Mapping)
            or not isinstance(evidence, Mapping)
        ):
            continue
        kind = candidates[0].get("kind")
        rule_evidence = evidence.get("rule")
        stats = evidence.get("stats")
        navigation_candidates = evidence.get("navigation_candidates")
        evidence_set = {
            value for value in rule_evidence if isinstance(value, str)
        } if isinstance(rule_evidence, list) else set()
        if (
            kind not in navigation_kinds
            or not {
                "explicit_title",
                "unusable_navigation_debris",
            }
            <= evidence_set
            or not isinstance(stats, Mapping)
            or not isinstance(stats.get("navigation_blocks"), int)
            or isinstance(stats.get("navigation_blocks"), bool)
            or int(stats["navigation_blocks"]) < 1
            or not isinstance(navigation_candidates, list)
            or not navigation_candidates
        ):
            continue
        recovery.append(
            {
                "page": page,
                "kind": kind,
                "confidence": round(float(strength), 4),
                "evidence": sorted(evidence_set),
            }
        )
    return recovery


def _front_region_report(
    layout: OutputLayout,
    selected: list[dict[str, object]],
) -> dict[str, object] | None:
    """Classify cached MinerU structure without rerunning OCR or layout."""
    if len(selected) != 1:
        return None
    selected_pages = _selection_physical_pages(selected[0])
    if selected_pages is None:
        return None
    start_page, _physical_pages = selected_pages
    relative = selected[0].get("content_list_v2")
    if not isinstance(relative, str) or not relative:
        return None
    candidate = (layout.root / relative).resolve()
    try:
        candidate.relative_to(layout.root.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None

    model_dir = project_root() / "models" / "front-region" / "v1"
    policy = _read_json(model_dir / "policy.json") or {}
    models_approved = _front_region_models_approved(model_dir, policy)
    active_model_dir = model_dir if models_approved else None
    thresholds = (
        policy.get("thresholds")
        if models_approved and isinstance(policy.get("thresholds"), dict)
        else None
    )
    margins = (
        policy.get("margins")
        if models_approved and isinstance(policy.get("margins"), dict)
        else None
    )
    fingerprint = classifier_fingerprint(
        active_model_dir,
        thresholds=thresholds,
        margins=margins,
    )
    task_key = selected[0].get("task_key")
    if not isinstance(task_key, str) or not re.fullmatch(r"[0-9a-f]{16,64}", task_key):
        task_key = _file_sha256(candidate)[:16]
    report_dir = layout.cache / "front-regions" / task_key
    report_dir.mkdir(parents=True, exist_ok=True)
    v2_path = report_dir / f"{fingerprint}.json"
    content_sha256 = _file_sha256(candidate)
    v2_report = _read_json(v2_path)
    cached_inputs = v2_report.get("inputs") if isinstance(v2_report, dict) else None
    cached_classifier = (
        v2_report.get("classifier") if isinstance(v2_report, dict) else None
    )
    cache_valid = bool(
        isinstance(v2_report, dict)
        and v2_report.get("schema") == FRONT_REGIONS_V2_SCHEMA
        and isinstance(cached_inputs, dict)
        and cached_inputs.get("content_list_sha256") == content_sha256
        and cached_inputs.get("start_page") == start_page
        and cached_inputs.get("max_pages") == 64
        and isinstance(cached_classifier, dict)
        and cached_classifier.get("fingerprint") == fingerprint
    )
    if not cache_valid:
        v2_report = classify_front_regions_v2(
            candidate,
            start_page=start_page,
            max_pages=64,
            model_dir=active_model_dir,
            thresholds=thresholds,
            margins=margins,
        )
        if v2_report.get("schema") != FRONT_REGIONS_V2_SCHEMA:
            return None
        # The classifier hashes canonical JSON while the cache key verifies the
        # exact saved evidence bytes.  Store the exact digest as the cache
        # contract so reformatting or replacing a content-list cannot reuse a
        # stale classification.
        inputs = v2_report.get("inputs")
        if not isinstance(inputs, dict):
            return None
        inputs["content_list_sha256"] = content_sha256
        _write_json(v2_path, v2_report)
    try:
        report = project_front_regions_v1(v2_report)
    except (TypeError, ValueError):
        return None
    native_recovery_pages = _native_recovery_pages(v2_report)
    if native_recovery_pages:
        report["native_recovery_pages"] = native_recovery_pages
    _write_json(layout.front_regions, report)
    return report


def _front_region_models_approved(
    model_dir: Path,
    policy: object,
) -> bool:
    """Require an explicit release decision bound to the exact model bytes."""
    if (
        not isinstance(policy, dict)
        or policy.get("schema") != "pdf2md.region-cascade-policy.v1"
        or policy.get("approved_for_auto_action") is not True
        or policy.get("experimental") is not False
        or policy.get("experiment_only") is True
    ):
        return False
    expected = policy.get("artifact_sha256")
    if not isinstance(expected, dict):
        return False
    for kind in ("layout", "text"):
        digest = expected.get(kind)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return False
        artifact = next(
            (candidate for candidate in (model_dir / f"{kind}.json", model_dir / f"{kind}.npz") if candidate.is_file()),
            None,
        )
        if artifact is None or _file_sha256(artifact) != digest:
            return False
        # Production release metadata is intentionally required inside the
        # exact hashed JSON artifact as well as in policy.json.  NPZ remains a
        # supported experimental inference format, but cannot be promoted
        # until its schema carries the same auditable metadata.
        if artifact.suffix.casefold() != ".json":
            return False
        payload = _read_json(artifact)
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if (
            not isinstance(metadata, dict)
            or metadata.get("approved_for_auto_action") is not True
            or metadata.get("experimental") is not False
            or metadata.get("experiment_only") is True
            or metadata.get("training_eligible") is not True
            or metadata.get("redistributable") is not True
        ):
            return False
    return True


def _selection_physical_pages(
    item: Mapping[str, object],
) -> tuple[int, Collection[int] | None] | None:
    value = str(item.get("pages", "")).strip().casefold()
    if value == "all":
        return 1, None
    match = re.fullmatch(r"(?P<start>[0-9]+)(?:-(?P<end>[0-9]+))?", value)
    if match is None:
        return None
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if start < 1 or end < start:
        return None
    return start, range(start, end + 1)


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
    front_regions = _front_region_report(
        layout,
        selected,
    )
    selection_pages = (
        _selection_physical_pages(selected[0])
        if len(selected) == 1
        else None
    )
    selected_physical_pages = selection_pages[1] if selection_pages else None
    native_source = source if selection_pages is not None else None
    native_cache = None
    if native_source is not None:
        page_token = str(selected[0].get("pages", "")).casefold()
        native_cache = layout.cache / f"frontmatter-v8-{page_token}.json"
    content = enhance_document_navigation(
        content,
        source=native_source,
        frontmatter_cache=native_cache,
        force_frontmatter=refresh_frontmatter and native_source is not None,
        front_regions=front_regions,
        selected_physical_pages=selected_physical_pages,
    )
    temporary = layout.markdown.with_suffix(".md.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(layout.markdown)


def run_conversion(
    options: ConversionOptions,
    emit: EventCallback = _noop_emit,
    cancel_event: threading.Event | None = None,
    ocr_service: OCRService | None = None,
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
    service = ocr_service
    owns_service = service is None
    service_log_start = len(service.log_lines) if service is not None else 0
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
                owns_service = True
                _emit_progress(emit, 10, "启动引擎")
                service.start(timeout=min(120, max(10, deadline - time.monotonic())))
                _emit_progress(emit, 18, "引擎就绪")
            elif not service.running:
                raise ConversionError("共享 OCR 会话已经退出。")
            service.set_progress_window(task_start, task_end - 2)
            _emit_progress(emit, task_start, f"准备页面 {page_range}")
            if cached_replacements:
                emit(
                    "message",
                    f"缓存页 {page_range} 含 {cached_replacements} 个损坏字符，正在用局部修复引擎重建…",
                )
            extracted = service.convert_range(source, start, end, options, deadline)
            try:
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
            finally:
                shutil.rmtree(extracted, ignore_errors=True)
            _update_manifest_selection(manifest, item)
            _write_json(layout.manifest, manifest)
            completed.append(item)
    finally:
        if service is not None:
            logs = "\n".join(service.log_lines[service_log_start:]).rstrip()
            if owns_service:
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
