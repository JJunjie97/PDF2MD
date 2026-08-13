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
APP_VERSION = "2.7.0"

WINDOW = "#0A0F1D"
PANEL = "#121A2A"
PANEL_SOFT = "#162137"
INPUT = "#0D1525"
BORDER = "#293A59"
BORDER_FOCUS = "#6478FF"
TEXT = "#F5F7FC"
MUTED = "#8D9AB1"
SUBTLE = "#627087"
ACCENT = "#6676F6"
ACCENT_HOVER = "#7887FF"
CYAN = "#3CCCEB"
SUCCESS = "#39D7A2"
DANGER = "#FF6B7C"

DISPLAY_FONT = "Segoe UI Variable Display"
BODY_FONT = "Microsoft YaHei UI"

PROFILE_LABELS = {
    "均衡": "balanced",
    "高速": "fast",
    "精确": "accurate",
}
METHOD_LABELS = {
    "自动": "auto",
    "文本": "txt",
    "OCR": "ocr",
}
LANGUAGE_LABELS = {
    "中 / 英 / 日": "ch",
    "韩文": "korean",
    "俄文 / 乌克兰文": "east_slavic",
    "阿拉伯文": "arabic",
}
TIMEOUT_LABELS = {
    "30 分钟": 1800,
    "60 分钟": 3600,
    "120 分钟": 7200,
}


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(relative: str) -> Path:
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / relative
    return project_root() / relative


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
        .replace("–", "-")
        .replace("—", "-")
    )
    if not cleaned or cleaned.casefold() in {"all", "全文"}:
        return None
    normalized: list[str] = []
    for part in cleaned.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not match:
            raise ValueError("页码格式：1, 3, 5-12")
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


def apply_windows_glass(root: tk.Tk) -> None:
    """Enable Windows 11 dark title bar, rounded corners, and acrylic backdrop."""
    if os.name != "nt":
        return
    try:
        root.update_idletasks()
        child = root.winfo_id()
        parent = ctypes.windll.user32.GetParent(child)
        hwnd = parent or child
        dwm = ctypes.windll.dwmapi

        def set_attribute(attribute: int, value: int) -> None:
            data = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))

        set_attribute(19, 1)  # Older DWMWA_USE_IMMERSIVE_DARK_MODE
        set_attribute(20, 1)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        set_attribute(33, 2)  # DWMWA_WINDOW_CORNER_PREFERENCE: round
        set_attribute(34, 0x00593A29)  # DWMWA_BORDER_COLOR
        set_attribute(35, 0x001D0F0A)  # DWMWA_CAPTION_COLOR
        set_attribute(36, 0x00FCF7F5)  # DWMWA_TEXT_COLOR
        set_attribute(38, 3)  # DWMWA_SYSTEMBACKDROP_TYPE: transient/acrylic
    except Exception:
        pass


class PDF2MDApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.active_process: subprocess.Popen[str] | None = None
        self.running = False
        self.last_result: dict[str, object] | None = None
        self.output_is_custom = False
        self.progress_value = 0.0
        self._window_icon: tk.PhotoImage | None = None
        self._header_icon: tk.PhotoImage | None = None

        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.pages_var = tk.StringVar(value="全文")
        self.profile_var = tk.StringVar(value="均衡")
        self.method_var = tk.StringVar(value="自动")
        self.language_var = tk.StringVar(value="中 / 英 / 日")
        self.timeout_var = tk.StringVar(value="30 分钟")
        self.force_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="就绪")
        self.percent_var = tk.StringVar(value="0%")

        self._configure_window()
        self._build_styles()
        self._build_ui()
        self.root.after(80, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(20, lambda: apply_windows_glass(self.root))

    def _configure_window(self) -> None:
        self.root.title(APP_NAME)
        self.root.configure(bg=WINDOW)
        width, height = 880, 660
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(800, 620)
        self.root.update_idletasks()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        icon = resource_path("assets/pdf2md-icon.png")
        if icon.is_file():
            try:
                self._window_icon = tk.PhotoImage(file=str(icon))
                self._header_icon = self._window_icon.subsample(6, 6)
                self.root.iconphoto(True, self._window_icon)
            except tk.TclError:
                self._window_icon = None
                self._header_icon = None

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.option_add("*TCombobox*Listbox.background", INPUT)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
        style.configure(
            "Glass.TCombobox",
            fieldbackground=INPUT,
            background=INPUT,
            foreground=TEXT,
            bordercolor=BORDER,
            arrowcolor=MUTED,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=(10, 9),
        )
        style.map(
            "Glass.TCombobox",
            fieldbackground=[("readonly", INPUT), ("disabled", PANEL)],
            foreground=[("readonly", TEXT), ("disabled", SUBTLE)],
            selectbackground=[("readonly", INPUT)],
            selectforeground=[("readonly", TEXT)],
            bordercolor=[("focus", BORDER_FOCUS)],
        )
        style.configure(
            "Glass.Horizontal.TProgressbar",
            troughcolor=INPUT,
            background=ACCENT,
            bordercolor=INPUT,
            lightcolor=CYAN,
            darkcolor=ACCENT,
            thickness=8,
        )
        style.configure(
            "Glass.TCheckbutton",
            background=PANEL,
            foreground=TEXT,
            indicatorbackground=INPUT,
            indicatorforeground=ACCENT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            font=(BODY_FONT, 9),
        )
        style.map(
            "Glass.TCheckbutton",
            background=[("active", PANEL)],
            foreground=[("disabled", SUBTLE), ("active", TEXT)],
            indicatorbackground=[("selected", ACCENT), ("disabled", PANEL_SOFT)],
        )

    def _label(
        self,
        parent: tk.Misc,
        text: str,
        *,
        color: str = MUTED,
        size: int = 9,
        bold: bool = False,
        background: str = PANEL,
    ) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=background,
            fg=color,
            font=(BODY_FONT, size, "bold" if bold else "normal"),
        )

    def _entry(self, parent: tk.Misc, variable: tk.StringVar) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            disabledbackground=PANEL,
            disabledforeground=SUBTLE,
            selectbackground=ACCENT,
            selectforeground="#FFFFFF",
            relief="flat",
            font=(BODY_FONT, 10),
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=BORDER_FOCUS,
        )

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: object,
        *,
        primary: bool = False,
        compact: bool = False,
        width: int | None = None,
    ) -> tk.Button:
        background = ACCENT if primary else PANEL_SOFT
        active = ACCENT_HOVER if primary else BORDER
        button = tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=background,
            fg="#FFFFFF" if primary else TEXT,
            activebackground=active,
            activeforeground="#FFFFFF",
            disabledforeground=SUBTLE,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=(BODY_FONT, 10, "bold" if primary else "normal"),
            padx=13 if compact else 18,
            pady=7 if compact else 10,
        )

        def enter(_event: object) -> None:
            if str(button.cget("state")) != "disabled":
                button.configure(bg=active)

        def leave(_event: object) -> None:
            if str(button.cget("state")) != "disabled":
                button.configure(bg=background)

        button.bind("<Enter>", enter)
        button.bind("<Leave>", leave)
        return button

    def _card(self, parent: tk.Misc) -> tuple[tk.Frame, tk.Frame]:
        border = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        card = tk.Frame(border, bg=PANEL)
        card.pack(fill="both", expand=True)
        return border, card

    def _path_row(
        self,
        parent: tk.Misc,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: object,
    ) -> tuple[tk.Entry, tk.Button]:
        self._label(parent, label, color=TEXT, bold=True).grid(row=row, column=0, sticky="w")
        holder = tk.Frame(parent, bg=PANEL)
        holder.grid(row=row + 1, column=0, sticky="ew", pady=(7, 15))
        holder.grid_columnconfigure(0, weight=1)
        entry = self._entry(holder, variable)
        entry.grid(row=0, column=0, sticky="ew", ipady=9)
        button = self._button(holder, "选择", command, compact=True)
        button.grid(row=0, column=1, padx=(8, 0))
        return entry, button

    def _option(
        self,
        parent: tk.Misc,
        column: int,
        label: str,
        variable: tk.StringVar,
        values: tuple[str, ...],
    ) -> ttk.Combobox:
        holder = tk.Frame(parent, bg=PANEL)
        holder.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 6 if column < 2 else 0))
        self._label(holder, label, color=TEXT, bold=True).pack(anchor="w")
        combo = ttk.Combobox(
            holder,
            textvariable=variable,
            values=values,
            state="readonly",
            style="Glass.TCombobox",
            font=(BODY_FONT, 9),
        )
        combo.pack(fill="x", pady=(7, 0))
        return combo

    def _build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=WINDOW)
        shell.pack(fill="both", expand=True, padx=30, pady=22)

        header = tk.Frame(shell, bg=WINDOW)
        header.pack(fill="x", pady=(0, 16))
        brand = tk.Frame(header, bg=WINDOW)
        brand.pack(side="left")
        if self._header_icon is not None:
            tk.Label(
                brand,
                image=self._header_icon,
                bg=WINDOW,
            ).pack(side="left", padx=(0, 12))
        title_box = tk.Frame(brand, bg=WINDOW)
        title_box.pack(side="left")
        tk.Label(
            title_box,
            text="PDF2MD",
            bg=WINDOW,
            fg=TEXT,
            font=(DISPLAY_FONT, 23, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="PDF → Markdown",
            bg=WINDOW,
            fg=MUTED,
            font=(BODY_FONT, 9),
        ).pack(anchor="w", pady=(1, 0))

        file_border, file_card = self._card(shell)
        file_border.pack(fill="x")
        file_content = tk.Frame(file_card, bg=PANEL)
        file_content.pack(fill="x", padx=20, pady=(17, 4))
        file_content.grid_columnconfigure(0, weight=1)

        self.source_entry, self.source_button = self._path_row(
            file_content, 0, "PDF 文件", self.source_var, self._choose_source
        )
        self.output_entry, self.output_button = self._path_row(
            file_content, 2, "输出目录", self.output_var, self._choose_output
        )
        self.output_entry.bind("<Key>", self._mark_output_custom)

        settings_border, settings_card = self._card(shell)
        settings_border.pack(fill="x", pady=(12, 0))
        settings = tk.Frame(settings_card, bg=PANEL)
        settings.pack(fill="x", padx=20, pady=17)
        for column in range(3):
            settings.grid_columnconfigure(column, weight=1, uniform="settings")

        pages_holder = tk.Frame(settings, bg=PANEL)
        pages_holder.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._label(pages_holder, "页码", color=TEXT, bold=True).pack(anchor="w")
        self.pages_entry = self._entry(pages_holder, self.pages_var)
        self.pages_entry.pack(fill="x", pady=(7, 0), ipady=9)
        self.pages_entry.bind("<FocusIn>", self._select_full_pages)

        self.profile_combo = self._option(
            settings, 1, "转换模式", self.profile_var, tuple(PROFILE_LABELS)
        )
        self.method_combo = self._option(
            settings, 2, "解析方式", self.method_var, tuple(METHOD_LABELS)
        )

        secondary = tk.Frame(settings_card, bg=PANEL)
        secondary.pack(fill="x", padx=20, pady=(0, 18))
        for column in range(3):
            secondary.grid_columnconfigure(column, weight=1, uniform="advanced")

        self.language_combo = self._option(
            secondary, 0, "OCR 语言", self.language_var, tuple(LANGUAGE_LABELS)
        )
        self.timeout_combo = self._option(
            secondary, 1, "超时", self.timeout_var, tuple(TIMEOUT_LABELS)
        )
        check_holder = tk.Frame(secondary, bg=PANEL)
        check_holder.grid(row=0, column=2, sticky="sew", padx=(6, 0), pady=(22, 3))
        self.force_check = ttk.Checkbutton(
            check_holder,
            text="忽略缓存",
            variable=self.force_var,
            style="Glass.TCheckbutton",
        )
        self.force_check.pack(anchor="w")

        progress_border, progress_card = self._card(shell)
        progress_border.pack(fill="x", pady=(12, 0))
        progress_content = tk.Frame(progress_card, bg=PANEL)
        progress_content.pack(fill="x", padx=20, pady=16)
        status_row = tk.Frame(progress_content, bg=PANEL)
        status_row.pack(fill="x", pady=(0, 9))
        self.status_dot = tk.Canvas(status_row, width=10, height=10, bg=PANEL, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 9))
        self.status_circle = self.status_dot.create_oval(1, 1, 9, 9, fill=SUBTLE, outline="")
        tk.Label(
            status_row,
            textvariable=self.status_var,
            bg=PANEL,
            fg=TEXT,
            font=(BODY_FONT, 9, "bold"),
        ).pack(side="left")
        tk.Label(
            status_row,
            textvariable=self.percent_var,
            bg=PANEL,
            fg=CYAN,
            font=(DISPLAY_FONT, 10, "bold"),
        ).pack(side="right")
        self.progress = ttk.Progressbar(
            progress_content,
            mode="determinate",
            maximum=100,
            value=0,
            style="Glass.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x")

        actions = tk.Frame(shell, bg=WINDOW)
        actions.pack(fill="x", pady=(15, 0))
        self.open_button = self._button(actions, "打开输出", self._open_output)
        self.open_button.pack(side="left")
        self.open_button.configure(state="disabled")
        self.cancel_button = self._button(actions, "取消", self._cancel)
        self.cancel_button.pack(side="right", padx=(8, 0))
        self.cancel_button.configure(state="disabled")
        self.convert_button = self._button(
            actions,
            "开始转换",
            self._start,
            primary=True,
            width=12,
        )
        self.convert_button.pack(side="right")

    def _mark_output_custom(self, _event: object) -> None:
        self.output_is_custom = True

    def _select_full_pages(self, _event: object) -> None:
        if self.pages_var.get().strip() == "全文":
            self.pages_entry.selection_range(0, "end")

    def _choose_source(self) -> None:
        selected = filedialog.askopenfilename(title="选择 PDF", filetypes=[("PDF 文件", "*.pdf")])
        if not selected:
            return
        source = Path(selected).resolve()
        previous_default = ""
        old_source = self.source_var.get().strip().strip('"')
        if old_source:
            try:
                previous_default = str(default_output_for(Path(old_source).expanduser().resolve()))
            except OSError:
                previous_default = ""
        self.source_var.set(str(source))
        self.last_result = None
        self.open_button.configure(state="disabled")
        self._set_progress(0, "就绪", reset=True)
        if (
            not self.output_var.get().strip()
            or not self.output_is_custom
            or self.output_var.get().strip() == previous_default
        ):
            self.output_var.set(str(default_output_for(source)))
            self.output_is_custom = False
        self.status_var.set("就绪")

    def _choose_output(self) -> None:
        source_text = self.source_var.get().strip().strip('"')
        initial = Path(source_text).expanduser().parent if source_text else project_root()
        selected = filedialog.askdirectory(title="选择输出目录", initialdir=str(initial), mustexist=True)
        if selected:
            self.output_var.set(str(Path(selected).resolve()))
            self.output_is_custom = True
            self.last_result = None
            self.open_button.configure(state="disabled")

    def _open_output(self) -> None:
        if self.last_result and os.name == "nt":
            output = Path(str(self.last_result.get("output_dir", "")))
            if output.exists():
                os.startfile(str(output))  # type: ignore[attr-defined]

    def _set_dot(self, color: str) -> None:
        self.status_dot.itemconfigure(self.status_circle, fill=color)

    def _set_progress(self, percent: float, message: str | None = None, *, reset: bool = False) -> None:
        value = max(0.0, min(100.0, float(percent)))
        if not reset:
            value = max(self.progress_value, value)
        self.progress_value = value
        self.progress.configure(value=value)
        self.percent_var.set(f"{int(round(value))}%")
        if message:
            self.status_var.set(message)

    def _set_running(self, running: bool) -> None:
        self.running = running
        state = "disabled" if running else "normal"
        for widget in (
            self.source_entry,
            self.output_entry,
            self.pages_entry,
            self.source_button,
            self.output_button,
            self.convert_button,
        ):
            widget.configure(state=state)
        for combo in (
            self.profile_combo,
            self.method_combo,
            self.language_combo,
            self.timeout_combo,
        ):
            combo.configure(state="disabled" if running else "readonly")
        self.force_check.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        if running:
            self._set_dot(ACCENT)

    def _start(self) -> None:
        source_text = self.source_var.get().strip().strip('"')
        if not source_text:
            messagebox.showwarning("PDF 文件", "请选择 PDF 文件。", parent=self.root)
            return
        source = Path(source_text).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() != ".pdf":
            messagebox.showerror("PDF 文件", f"找不到有效的 PDF：\n{source}", parent=self.root)
            return
        try:
            pages = normalize_pages(self.pages_var.get())
        except ValueError as exc:
            messagebox.showerror("页码", str(exc), parent=self.root)
            return

        output_text = self.output_var.get().strip().strip('"')
        output = Path(output_text).expanduser().resolve() if output_text else default_output_for(source)
        self.output_var.set(str(output))
        profile = PROFILE_LABELS[self.profile_var.get()]
        method = METHOD_LABELS[self.method_var.get()]
        language = LANGUAGE_LABELS[self.language_var.get()]
        timeout = TIMEOUT_LABELS[self.timeout_var.get()]
        force = self.force_var.get()

        self.last_result = None
        self.open_button.configure(state="disabled")
        self.cancel_event = threading.Event()
        self._set_progress(1, "准备", reset=True)
        self._set_running(True)

        def worker() -> None:
            diagnostics: list[str] = []
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
                    "--method",
                    method,
                    "--lang",
                    language,
                    "--timeout",
                    str(timeout),
                    "--json",
                ]
                if pages:
                    command.extend(("--pages", pages))
                if force:
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
                self.active_process = process

                def read_stderr() -> None:
                    assert process.stderr is not None
                    for raw_line in process.stderr:
                        line = raw_line.strip()
                        if not line:
                            continue
                        diagnostics.append(line)
                        if len(diagnostics) > 80:
                            del diagnostics[:20]
                        if line.startswith("[进度] "):
                            try:
                                payload = json.loads(line[5:])
                            except ValueError:
                                continue
                            self.events.put(("progress", payload))
                        elif line.startswith("[状态] "):
                            self.events.put(("message", line[5:]))

                reader = threading.Thread(target=read_stderr, daemon=True)
                reader.start()
                assert process.stdout is not None
                stdout = process.stdout.read()
                exit_code = process.wait()
                reader.join(timeout=2)
                if self.cancel_event.is_set():
                    self.events.put(("cancelled", "已取消"))
                    return
                try:
                    payload = json.loads(stdout.strip())
                except ValueError as exc:
                    detail = diagnostics[-1] if diagnostics else f"CLI 退出码 {exit_code}"
                    raise RuntimeError(f"CLI 未返回有效结果。{detail}") from exc
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
            self.status_var.set("取消中")
            self.cancel_button.configure(state="disabled")
            if self.active_process is not None:
                terminate_process_tree(self.active_process)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress" and isinstance(value, dict):
                    self._set_progress(
                        float(value.get("percent", self.progress_value)),
                        str(value.get("message") or ""),
                    )
                elif kind == "message":
                    self.status_var.set(str(value).rstrip("。…"))
                elif kind == "complete":
                    result = value
                    assert isinstance(result, dict)
                    self.last_result = result
                    self._set_progress(100, "完成")
                    self._set_running(False)
                    self._set_dot(SUCCESS)
                    elapsed = float(result.get("elapsed_seconds", 0))
                    self.status_var.set(f"完成 · {elapsed:.1f} 秒")
                    self.open_button.configure(state="normal")
                elif kind == "cancelled":
                    self._set_running(False)
                    self._set_dot(SUBTLE)
                    self.status_var.set(str(value))
                elif kind == "error":
                    self._set_running(False)
                    self._set_dot(DANGER)
                    self.status_var.set("转换失败")
                    messagebox.showerror("转换失败", str(value), parent=self.root)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(80, self._poll_events)

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("任务运行中", "关闭将取消转换。继续？", parent=self.root):
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
