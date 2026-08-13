from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import webview


APP_NAME = "PDF2MD"
APP_VERSION = "3.0.0"
VALID_PROFILES = {"balanced", "fast", "accurate"}
VALID_METHODS = {"auto", "txt", "ocr"}
VALID_LANGUAGES = {"ch", "korean", "east_slavic", "arabic"}


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(relative: str) -> Path:
    bundle = getattr(sys, "_MEIPASS", None)
    return Path(bundle) / relative if bundle else project_root() / relative


def cli_paths() -> tuple[Path, Path]:
    root = project_root()
    return root / "runtime" / "env" / "python.exe", root / "src" / "pdf2md_cli.py"


def validate_cli() -> None:
    python, cli = cli_paths()
    core = project_root() / "src" / "pdf2md_core.py"
    missing = [str(path) for path in (python, cli, core) if not path.is_file()]
    if missing:
        raise RuntimeError("PDF2MD CLI 不完整：\n" + "\n".join(f"- {path}" for path in missing))


def default_output_for(source: Path) -> Path:
    return source.parent / f"{source.stem}.pdf2md"


def normalize_pages(expression: str | None) -> str | None:
    if expression is None:
        return None
    cleaned = (
        expression.strip()
        .replace("，", ",")
        .replace("、", ",")
        .replace("；", ",")
        .replace(";", ",")
        .replace("—", "-")
        .replace("–", "-")
    )
    if not cleaned or cleaned.casefold() in {"all", "全文"}:
        return None
    normalized: list[str] = []
    for part in cleaned.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not match:
            raise ValueError("页码格式示例：1, 3, 5-12")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise ValueError("页码从 1 开始，结束页不能小于起始页。")
        normalized.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(normalized)


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )


def configure_standard_streams() -> None:
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass


def native_error(message: str) -> None:
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, str(message), APP_NAME, 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


class PDF2MDBridge:
    def __init__(self) -> None:
        # Keep the native Window private. pywebview exposes public JS API
        # attributes recursively, and a native window is not serializable.
        self._window: webview.Window | None = None
        self._lock = threading.Lock()
        self._running = False
        self._maximized = False
        self._cancel_event = threading.Event()
        self._active_process: subprocess.Popen[str] | None = None
        self._last_output: Path | None = None

    def attach_window(self, window: webview.Window) -> None:
        self._window = window

    @staticmethod
    def _clean_path(value: object) -> str:
        return str(value or "").strip().strip('"')

    @staticmethod
    def _existing_directory(*candidates: Path) -> Path:
        for candidate in candidates:
            current = candidate.expanduser()
            if current.is_file():
                current = current.parent
            while not current.exists() and current != current.parent:
                current = current.parent
            if current.is_dir():
                return current.resolve()
        return project_root()

    def _emit(self, kind: str, payload: object) -> None:
        if self._window is None:
            return
        kind_json = json.dumps(kind, ensure_ascii=True)
        payload_json = json.dumps(payload, ensure_ascii=True, default=str)
        script = (
            "if (window.PDF2MD && window.PDF2MD.receive) "
            f"window.PDF2MD.receive({kind_json}, {payload_json});"
        )
        try:
            self._window.run_js(script)
        except Exception:
            pass

    def default_output(self, source_value: object) -> dict[str, object]:
        try:
            source_text = self._clean_path(source_value)
            if not source_text:
                return {"ok": True, "output": ""}
            source = Path(source_text).expanduser().resolve()
            return {"ok": True, "output": str(default_output_for(source))}
        except OSError as exc:
            return {"ok": False, "message": str(exc)}

    def choose_pdf(self, current_value: object = "", output_is_custom: object = False) -> dict[str, object]:
        if self._window is None:
            return {"ok": False, "message": "窗口尚未就绪。"}
        current_text = self._clean_path(current_value)
        current = Path(current_text).expanduser() if current_text else project_root()
        initial = self._existing_directory(current, project_root())
        try:
            selected = self._window.create_file_dialog(
                webview.FileDialog.OPEN,
                directory=str(initial),
                allow_multiple=False,
                file_types=("PDF 文件 (*.pdf)",),
            )
        except Exception as exc:
            return {"ok": False, "message": f"无法打开文件选择器：{exc}"}
        if not selected:
            return {"ok": True}
        source = Path(selected[0]).expanduser().resolve()
        response: dict[str, object] = {"ok": True, "path": str(source)}
        if not bool(output_is_custom):
            response["output"] = str(default_output_for(source))
        return response

    def choose_output(self, current_value: object = "", source_value: object = "") -> dict[str, object]:
        if self._window is None:
            return {"ok": False, "message": "窗口尚未就绪。"}
        current_text = self._clean_path(current_value)
        source_text = self._clean_path(source_value)
        candidates = [
            Path(current_text).expanduser() if current_text else project_root(),
            Path(source_text).expanduser().parent if source_text else project_root(),
            project_root(),
        ]
        initial = self._existing_directory(*candidates)
        try:
            selected = self._window.create_file_dialog(
                webview.FileDialog.FOLDER,
                directory=str(initial),
                allow_multiple=False,
            )
        except Exception as exc:
            return {"ok": False, "message": f"无法打开目录选择器：{exc}"}
        if not selected:
            return {"ok": True}
        return {"ok": True, "path": str(Path(selected[0]).expanduser().resolve())}

    def _prepare_config(self, config: object) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise ValueError("转换参数无效。")
        source_text = self._clean_path(config.get("source"))
        if not source_text:
            raise ValueError("请选择 PDF 文件。")
        source = Path(source_text).expanduser().resolve()
        if not source.is_file() or source.suffix.casefold() != ".pdf":
            raise ValueError(f"找不到有效的 PDF：{source}")
        output_text = self._clean_path(config.get("output"))
        output = Path(output_text).expanduser().resolve() if output_text else default_output_for(source)
        pages = normalize_pages(str(config.get("pages") or ""))
        profile = str(config.get("profile") or "balanced")
        method = str(config.get("method") or "auto")
        language = str(config.get("language") or "ch")
        if profile not in VALID_PROFILES:
            raise ValueError("转换模式无效。")
        if method not in VALID_METHODS:
            raise ValueError("解析方式无效。")
        if language not in VALID_LANGUAGES:
            raise ValueError("OCR 语言无效。")
        try:
            timeout = int(config.get("timeout") or 1800)
        except (TypeError, ValueError) as exc:
            raise ValueError("超时时间无效。") from exc
        if not 60 <= timeout <= 86400:
            raise ValueError("超时时间必须在 1 分钟到 24 小时之间。")
        return {
            "source": source,
            "output": output,
            "pages": pages,
            "profile": profile,
            "method": method,
            "language": language,
            "timeout": timeout,
            "force": bool(config.get("force")),
        }

    def start_conversion(self, config: object) -> dict[str, object]:
        try:
            prepared = self._prepare_config(config)
            validate_cli()
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        with self._lock:
            if self._running:
                return {"ok": False, "message": "已有转换任务正在运行。"}
            self._running = True
            self._cancel_event = threading.Event()
            self._last_output = None
        try:
            threading.Thread(target=self._conversion_worker, args=(prepared,), daemon=True).start()
        except Exception as exc:
            with self._lock:
                self._running = False
            return {"ok": False, "message": f"无法启动转换任务：{exc}"}
        return {"ok": True, "output": str(prepared["output"])}

    def _conversion_worker(self, config: dict[str, Any]) -> None:
        diagnostics: list[str] = []
        process: subprocess.Popen[str] | None = None
        try:
            if self._cancel_event.is_set():
                self._emit("cancelled", {"message": "已取消"})
                return
            python, cli = cli_paths()
            command = [
                str(python), str(cli), str(config["source"]),
                "--output", str(config["output"]),
                "--profile", config["profile"],
                "--method", config["method"],
                "--lang", config["language"],
                "--timeout", str(config["timeout"]),
                "--json",
            ]
            if config["pages"]:
                command.extend(("--pages", config["pages"]))
            if config["force"]:
                command.append("--force")
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            process = subprocess.Popen(
                command,
                cwd=str(project_root()),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creation_flags,
            )
            with self._lock:
                self._active_process = process
            if self._cancel_event.is_set():
                terminate_process_tree(process)

            def read_stderr() -> None:
                assert process is not None and process.stderr is not None
                for raw_line in process.stderr:
                    line = raw_line.strip()
                    if not line:
                        continue
                    diagnostics.append(line)
                    if len(diagnostics) > 80:
                        del diagnostics[:20]
                    if line.startswith("[进度] "):
                        try:
                            payload = json.loads(line.removeprefix("[进度] "))
                        except ValueError:
                            continue
                        self._emit("progress", payload)
                    elif line.startswith("[状态] "):
                        self._emit("message", {"message": line.removeprefix("[状态] ")})

            reader = threading.Thread(target=read_stderr, daemon=True)
            reader.start()
            assert process.stdout is not None
            stdout = process.stdout.read()
            exit_code = process.wait()
            reader.join(timeout=2)
            if self._cancel_event.is_set():
                self._emit("cancelled", {"message": "已取消"})
                return
            try:
                payload = json.loads(stdout.strip())
            except ValueError as exc:
                detail = diagnostics[-1] if diagnostics else f"CLI 退出码 {exit_code}"
                raise RuntimeError(f"CLI 未返回有效结果。{detail}") from exc
            if exit_code != 0 or not payload.get("ok"):
                raise RuntimeError(str(payload.get("message") or f"CLI 退出码：{exit_code}"))
            output = Path(str(payload.get("output_dir") or config["output"])).resolve()
            with self._lock:
                self._last_output = output
            self._emit("complete", payload)
        except Exception as exc:
            if self._cancel_event.is_set():
                self._emit("cancelled", {"message": "已取消"})
            else:
                self._emit("error", {"message": str(exc)})
        finally:
            with self._lock:
                if self._active_process is process:
                    self._active_process = None
                self._running = False

    def cancel(self) -> dict[str, object]:
        with self._lock:
            if not self._running:
                return {"ok": True}
            self._cancel_event.set()
            process = self._active_process
        if process is not None:
            terminate_process_tree(process)
        return {"ok": True}

    def open_output(self) -> dict[str, object]:
        with self._lock:
            output = self._last_output
        if output is None or not output.exists():
            return {"ok": False, "message": "输出目录尚不存在。"}
        try:
            if os.name == "nt":
                os.startfile(str(output))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(output)])
        except Exception as exc:
            return {"ok": False, "message": f"无法打开输出目录：{exc}"}
        return {"ok": True}

    def minimize(self) -> dict[str, object]:
        if self._window is not None:
            self._window.minimize()
        return {"ok": True}

    def toggle_maximize(self) -> dict[str, object]:
        if self._window is None:
            return {"ok": False, "message": "窗口尚未就绪。"}
        if self._maximized:
            self._window.restore()
        else:
            self._window.maximize()
        self._maximized = not self._maximized
        return {"ok": True, "maximized": self._maximized}

    def _stop_active_process(self) -> None:
        with self._lock:
            self._cancel_event.set()
            process = self._active_process
        if process is not None:
            terminate_process_tree(process)

    def on_closing(self) -> None:
        self._stop_active_process()

    def close_window(self) -> dict[str, object]:
        self._stop_active_process()
        if self._window is not None:
            self._window.destroy()
        return {"ok": True}


def gui_main() -> int:
    try:
        validate_cli()
        html = resource_path("ui/index.html")
        if not html.is_file():
            raise RuntimeError(f"找不到界面文件：{html}")
    except Exception as exc:
        native_error(str(exc))
        return 1
    webview.settings["ALLOW_FILE_URLS"] = True
    bridge = PDF2MDBridge()
    window = webview.create_window(
        APP_NAME,
        url=html.as_uri(),
        js_api=bridge,
        width=920,
        height=700,
        min_size=(780, 620),
        resizable=True,
        frameless=True,
        easy_drag=False,
        shadow=True,
        background_color="#E8EBF2",
        # WebView2 transparent composition can clip the child surface on
        # some Windows/GPU combinations. The CSS still provides the glass UI.
        transparent=False,
        text_select=True,
    )
    if window is None:
        native_error("无法创建 PDF2MD 窗口。")
        return 1
    bridge.attach_window(window)
    window.events.closing += bridge.on_closing
    storage = project_root() / "runtime" / "cache" / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        webview.start(
            gui="edgechromium",
            debug=False,
            private_mode=True,
            storage_path=str(storage),
            icon=str(resource_path("assets/pdf2md-icon.ico")),
        )
    except Exception as exc:
        native_error(f"无法启动 WebView2 界面：\n{exc}")
        return 1
    return 0


def main() -> int:
    configure_standard_streams()
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
