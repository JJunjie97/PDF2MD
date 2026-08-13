from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "PDF2MD"
APP_VERSION = "2.4.0"

BG = "#0C1118"
SURFACE = "#121923"
CARD = "#171F2B"
BORDER = "#293547"
TEXT = "#EEF3FA"
MUTED = "#94A0B2"
ACCENT = "#5B8CFF"
ACCENT_HOVER = "#78A3FF"
SUCCESS = "#39D19C"
DANGER = "#FF6B78"

PROFILE_LABELS = {
    "均衡（推荐）": "balanced",
    "高速": "fast",
    "精确": "accurate",
}


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


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


def validate_pages(expression: str | None) -> None:
    if not expression:
        return
    cleaned = expression.replace("–", "-").replace("—", "-")
    for part in cleaned.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not match:
            raise ValueError("页码格式应为 3、3-8 或 1-3,8,12-15。")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise ValueError("PDF 页码从 1 开始，结束页不能小于起始页。")


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


class PDF2MDApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.active_process: subprocess.Popen[str] | None = None
        self.running = False
        self.last_result: dict[str, object] | None = None

        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar(value="选择 PDF 后自动生成同级结果目录")
        self.pages_var = tk.StringVar()
        self.profile_var = tk.StringVar(value="均衡（推荐）")
        self.status_var = tk.StringVar(value="请选择一个 PDF 文件")

        self._configure_window()
        self._build_styles()
        self._build_ui()
        self.root.after(100, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_window(self) -> None:
        self.root.title(f"{APP_NAME}  ·  v{APP_VERSION}")
        self.root.configure(bg=BG)
        self.root.geometry("780x590")
        self.root.minsize(700, 540)
        self.root.update_idletasks()
        width, height = 780, 590
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "PDF2MD.TCombobox",
            fieldbackground=SURFACE,
            background=SURFACE,
            foreground=TEXT,
            bordercolor=BORDER,
            arrowcolor=MUTED,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=8,
        )
        style.map(
            "PDF2MD.TCombobox",
            fieldbackground=[("readonly", SURFACE)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", SURFACE)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure(
            "PDF2MD.Horizontal.TProgressbar",
            troughcolor=SURFACE,
            background=ACCENT,
            bordercolor=SURFACE,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

    def _label(self, parent: tk.Misc, text: str, *, color: str = MUTED, size: int = 10) -> tk.Label:
        return tk.Label(parent, text=text, bg=CARD, fg=color, font=("Microsoft YaHei UI", size))

    def _entry(self, parent: tk.Misc, variable: tk.StringVar) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            bg=SURFACE,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Microsoft YaHei UI", 10),
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: object,
        *,
        primary: bool = False,
        width: int | None = None,
    ) -> tk.Button:
        bg = ACCENT if primary else SURFACE
        active = ACCENT_HOVER if primary else BORDER
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=bg,
            fg="white" if primary else TEXT,
            activebackground=active,
            activeforeground="white",
            disabledforeground="#657083",
            relief="flat",
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold" if primary else "normal"),
            padx=14,
            pady=9,
        )

    def _build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True, padx=30, pady=24)

        header = tk.Frame(shell, bg=BG)
        header.pack(fill="x", pady=(0, 18))
        tk.Label(
            header,
            text="PDF → Markdown",
            bg=BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 22, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="只输出一个 Markdown、images 和内部 raw 缓存",
            bg=BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w", pady=(5, 0))

        card = tk.Frame(shell, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True)
        content = tk.Frame(card, bg=CARD)
        content.pack(fill="both", expand=True, padx=22, pady=20)
        content.grid_columnconfigure(0, weight=1)

        self._label(content, "PDF 文件", color=TEXT).grid(row=0, column=0, sticky="w")
        source_row = tk.Frame(content, bg=CARD)
        source_row.grid(row=1, column=0, sticky="ew", pady=(7, 15))
        source_row.grid_columnconfigure(0, weight=1)
        self.source_entry = self._entry(source_row, self.source_var)
        self.source_entry.grid(row=0, column=0, sticky="ew", ipady=9)
        self.browse_button = self._button(source_row, "选择文件", self._choose_source)
        self.browse_button.grid(row=0, column=1, padx=(9, 0))

        self._label(content, "结果目录", color=TEXT).grid(row=2, column=0, sticky="w")
        output_label = tk.Label(
            content,
            textvariable=self.output_var,
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
            padx=12,
            pady=11,
            relief="flat",
            font=("Microsoft YaHei UI", 9),
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        output_label.grid(row=3, column=0, sticky="ew", pady=(7, 15))

        options_row = tk.Frame(content, bg=CARD)
        options_row.grid(row=4, column=0, sticky="ew")
        options_row.grid_columnconfigure(0, weight=1)
        options_row.grid_columnconfigure(1, weight=1)

        pages_box = tk.Frame(options_row, bg=CARD)
        pages_box.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._label(pages_box, "页码（留空为全文）", color=TEXT).pack(anchor="w")
        self.pages_entry = self._entry(pages_box, self.pages_var)
        self.pages_entry.pack(fill="x", pady=(7, 0), ipady=9)

        profile_box = tk.Frame(options_row, bg=CARD)
        profile_box.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self._label(profile_box, "转换模式", color=TEXT).pack(anchor="w")
        self.profile_combo = ttk.Combobox(
            profile_box,
            textvariable=self.profile_var,
            values=tuple(PROFILE_LABELS),
            state="readonly",
            style="PDF2MD.TCombobox",
            font=("Microsoft YaHei UI", 10),
        )
        self.profile_combo.pack(fill="x", pady=(7, 0))

        status_row = tk.Frame(content, bg=CARD)
        status_row.grid(row=5, column=0, sticky="ew", pady=(20, 7))
        self.status_dot = tk.Label(status_row, text="●", bg=CARD, fg=MUTED, font=("Segoe UI", 10))
        self.status_dot.pack(side="left")
        tk.Label(
            status_row,
            textvariable=self.status_var,
            bg=CARD,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left", padx=(7, 0))

        self.progress = ttk.Progressbar(content, mode="indeterminate", style="PDF2MD.Horizontal.TProgressbar")
        self.progress.grid(row=6, column=0, sticky="ew")

        self.log = tk.Text(
            content,
            height=7,
            bg="#0E141D",
            fg=MUTED,
            insertbackground=TEXT,
            relief="flat",
            wrap="word",
            state="disabled",
            font=("Cascadia Mono", 9),
            padx=10,
            pady=9,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self.log.grid(row=7, column=0, sticky="nsew", pady=(12, 14))
        content.grid_rowconfigure(7, weight=1)

        actions = tk.Frame(content, bg=CARD)
        actions.grid(row=8, column=0, sticky="ew")
        self.open_button = self._button(actions, "打开结果", self._open_output)
        self.open_button.pack(side="left")
        self.open_button.configure(state="disabled")
        self.cancel_button = self._button(actions, "取消", self._cancel)
        self.cancel_button.pack(side="right", padx=(9, 0))
        self.cancel_button.configure(state="disabled")
        self.convert_button = self._button(actions, "开始转换", self._start, primary=True, width=12)
        self.convert_button.pack(side="right")

    def _choose_source(self) -> None:
        selected = filedialog.askopenfilename(title="选择 PDF", filetypes=[("PDF 文件", "*.pdf")])
        if selected:
            source = Path(selected).resolve()
            self.source_var.set(str(source))
            self.output_var.set(str(default_output_for(source)))
            self.status_var.set("已选择 PDF，可以开始转换")

    def _open_output(self) -> None:
        if self.last_result and os.name == "nt":
            output = Path(str(self.last_result.get("output_dir", "")))
            if output.exists():
                os.startfile(str(output))  # type: ignore[attr-defined]

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 300:
            self.log.delete("1.0", f"{lines - 250}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.running = running
        state = "disabled" if running else "normal"
        self.source_entry.configure(state=state)
        self.pages_entry.configure(state=state)
        self.browse_button.configure(state=state)
        self.profile_combo.configure(state="disabled" if running else "readonly")
        self.convert_button.configure(state=state)
        self.cancel_button.configure(state="normal" if running else "disabled")
        if running:
            self.progress.start(12)
            self.status_dot.configure(fg=ACCENT)
        else:
            self.progress.stop()

    def _start(self) -> None:
        source_text = self.source_var.get().strip().strip('"')
        if not source_text:
            messagebox.showwarning("请选择 PDF", "请先选择一个 PDF 文件。", parent=self.root)
            return
        source = Path(source_text).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() != ".pdf":
            messagebox.showerror("输入无效", f"找不到有效的 PDF：\n{source}", parent=self.root)
            return
        pages = self.pages_var.get().strip() or None
        try:
            validate_pages(pages)
        except ValueError as exc:
            messagebox.showerror("页码无效", str(exc), parent=self.root)
            return
        output = default_output_for(source)
        profile = PROFILE_LABELS[self.profile_var.get()]
        self.output_var.set(str(output))
        self.last_result = None
        self.open_button.configure(state="disabled")
        self.cancel_event = threading.Event()
        self._set_running(True)
        self.status_var.set("正在准备转换…")
        self._append_log(f"PDF：{source}")
        self._append_log(f"页码：{pages or '全文'} · 模式：{profile}")

        def worker() -> None:
            try:
                python, cli = cli_paths()
                command = [
                    str(python),
                    str(cli),
                    str(source),
                    "--output",
                    str(output),
                    "--profile",
                    profile,
                    "--json",
                ]
                if pages:
                    command.extend(("--pages", pages))
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
                self.active_process = process

                def read_stderr() -> None:
                    assert process.stderr is not None
                    for raw_line in process.stderr:
                        line = raw_line.strip()
                        if not line:
                            continue
                        if line.startswith("[状态] "):
                            self.events.put(("message", line[5:]))
                        else:
                            self.events.put(("line", line))

                reader = threading.Thread(target=read_stderr, daemon=True)
                reader.start()
                assert process.stdout is not None
                stdout = process.stdout.read()
                exit_code = process.wait()
                reader.join(timeout=2)
                if self.cancel_event.is_set():
                    self.events.put(("cancelled", "转换已取消。"))
                    return
                try:
                    payload = json.loads(stdout.strip())
                except ValueError as exc:
                    raise RuntimeError(f"CLI 没有返回有效 JSON（退出码 {exit_code}）。") from exc
                if exit_code != 0 or not payload.get("ok"):
                    raise RuntimeError(str(payload.get("message") or f"CLI 退出码：{exit_code}"))
                self.events.put(("complete", payload))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.active_process = None

        threading.Thread(target=worker, daemon=True).start()

    def _cancel(self) -> None:
        if self.running:
            self.cancel_event.set()
            self.status_var.set("正在取消…")
            self.cancel_button.configure(state="disabled")
            if self.active_process is not None:
                terminate_process_tree(self.active_process)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "message":
                    self.status_var.set(str(value))
                    self._append_log(str(value))
                elif kind == "line":
                    line = str(value)
                    if any(marker in line.lower() for marker in ("error", "warning", "processing", "completed")):
                        self._append_log(line)
                elif kind == "complete":
                    result = value
                    assert isinstance(result, dict)
                    self.last_result = result
                    self._set_running(False)
                    self.status_dot.configure(fg=SUCCESS)
                    elapsed = float(result.get("elapsed_seconds", 0))
                    cache = str(result.get("cache", "updated"))
                    self.status_var.set(f"转换完成 · {elapsed:.1f} 秒 · 缓存 {cache}")
                    self._append_log(f"Markdown：{result.get('markdown')}")
                    self._append_log(f"图片：{result.get('images_dir')}")
                    self.open_button.configure(state="normal")
                elif kind == "cancelled":
                    self._set_running(False)
                    self.status_dot.configure(fg=MUTED)
                    self.status_var.set(str(value))
                elif kind == "error":
                    self._set_running(False)
                    self.status_dot.configure(fg=DANGER)
                    self.status_var.set("转换失败")
                    self._append_log(f"错误：{value}")
                    messagebox.showerror("转换失败", str(value), parent=self.root)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_events)

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("任务仍在运行", "关闭窗口将取消当前转换，是否继续？", parent=self.root):
                return
            self.cancel_event.set()
            if self.active_process is not None:
                terminate_process_tree(self.active_process)
        self.root.destroy()


def enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


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


def hide_owned_console() -> None:
    if os.name != "nt":
        return
    try:
        processes = (ctypes.c_uint * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(processes, len(processes))
        if count <= 2:
            window = ctypes.windll.kernel32.GetConsoleWindow()
            if window:
                ctypes.windll.user32.ShowWindow(window, 0)
    except Exception:
        pass


def gui_main() -> int:
    hide_owned_console()
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        validate_cli()
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("PDF2MD 环境不可用", str(exc))
        root.destroy()
        return 1
    PDF2MDApp(root)
    root.mainloop()
    return 0


def main() -> int:
    configure_standard_streams()
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
