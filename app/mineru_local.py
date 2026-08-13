from __future__ import annotations

import argparse
import ctypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "MinerU 本地转换器"
APP_VERSION = "1.0.0"

BG = "#0B0F14"
SURFACE = "#111720"
CARD = "#161D27"
CARD_ALT = "#121923"
BORDER = "#283241"
TEXT = "#EAF0F8"
MUTED = "#8E99AA"
ACCENT = "#5B8CFF"
ACCENT_HOVER = "#77A2FF"
SUCCESS = "#37D399"
DANGER = "#FF6B78"

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

STAGES = (
    "准备任务",
    "检查服务",
    "提交任务",
    "等待队列",
    "解析文档",
    "整理输出",
    "完成",
)


@dataclass(slots=True)
class ConversionOptions:
    source: Path
    output: Path
    backend: str = "hybrid-engine"
    method: str = "auto"
    effort: str = "medium"
    language: str = "ch"
    page_start: int | None = None
    page_end: int | None = None
    formula: bool = True
    table: bool = True
    image_analysis: bool = True
    md_output: Path | None = None
    open_output: bool = False


@dataclass(slots=True)
class RunResult:
    exit_code: int
    markdown_files: list[Path]
    command: list[str]
    elapsed_seconds: float


EventCallback = Callable[[str, object], None]


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def runtime_paths() -> dict[str, Path]:
    root = project_root()
    return {
        "root": root,
        "env": root / ".conda-env",
        "mineru": root / ".conda-env" / "Scripts" / "mineru.exe",
        "config": root / "mineru.json",
        "cuda": root / ".cuda",
        "cache": root / ".cache",
        "temp": root / ".tmp",
        "default_output": root / "output",
    }


def validate_runtime() -> dict[str, Path]:
    paths = runtime_paths()
    missing = [str(paths[key]) for key in ("mineru", "config", "cuda") if not paths[key].exists()]
    if missing:
        joined = "\n".join(f"- {item}" for item in missing)
        raise RuntimeError(
            "找不到 MinerU 本地运行环境。请把 MinerU-Local.exe 放在 MinerU 安装目录根部。\n\n"
            f"缺少：\n{joined}"
        )
    paths["temp"].mkdir(parents=True, exist_ok=True)
    paths["default_output"].mkdir(parents=True, exist_ok=True)
    return paths


def build_runtime_env(paths: dict[str, Path]) -> dict[str, str]:
    root = paths["root"]
    conda_env = paths["env"]
    env = os.environ.copy()
    env.update(
        {
            "CONDARC": str(root / ".condarc"),
            "CONDA_PKGS_DIRS": str(root / ".conda-pkgs"),
            "PIP_CACHE_DIR": str(root / ".cache" / "pip"),
            "UV_CACHE_DIR": str(root / ".cache" / "uv"),
            "XDG_CACHE_HOME": str(root / ".cache"),
            "HF_HOME": str(root / ".cache" / "huggingface"),
            "HUGGINGFACE_HUB_CACHE": str(root / ".cache" / "huggingface" / "hub"),
            "MODELSCOPE_CACHE": str(root / ".cache" / "modelscope"),
            "TORCH_HOME": str(root / ".cache" / "torch"),
            "CUDA_PATH": str(root / ".cuda"),
            "GRADIO_TEMP_DIR": str(root / ".cache" / "gradio"),
            "MPLCONFIGDIR": str(root / ".cache" / "matplotlib"),
            "NUMBA_CACHE_DIR": str(root / ".cache" / "numba"),
            "MINERU_TOOLS_CONFIG_JSON": str(root / "mineru.json"),
            "MINERU_MODEL_SOURCE": "local",
            "MINERU_API_OUTPUT_ROOT": str(root / "output"),
            "TEMP": str(root / ".tmp"),
            "TMP": str(root / ".tmp"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    path_parts = [
        str(conda_env),
        str(conda_env / "Scripts"),
        str(conda_env / "Library" / "bin"),
        str(root / ".cuda" / "bin"),
        env.get("PATH", ""),
    ]
    env["PATH"] = os.pathsep.join(part for part in path_parts if part)
    return env


def parse_pages(value: str) -> tuple[int, int]:
    cleaned = value.strip().replace("—", "-").replace("–", "-")
    match = re.fullmatch(r"(\d+)\s*(?:-\s*(\d+))?", cleaned)
    if not match:
        raise ValueError("页码格式应为单页（如 3）或连续范围（如 3-8）。")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start < 1 or end < 1:
        raise ValueError("页码从 1 开始。")
    if end < start:
        raise ValueError("结束页不能小于开始页。")
    return start - 1, end - 1


def build_command(options: ConversionOptions, paths: dict[str, Path]) -> list[str]:
    command = [
        str(paths["mineru"]),
        "-p",
        str(options.source),
        "-o",
        str(options.output),
        "-b",
        options.backend,
        "-m",
        options.method,
        "--effort",
        options.effort,
        "-l",
        options.language,
        "-f",
        str(options.formula).lower(),
        "-t",
        str(options.table).lower(),
        "--image-analysis",
        str(options.image_analysis).lower(),
    ]
    if options.page_start is not None:
        command.extend(["-s", str(options.page_start)])
    if options.page_end is not None:
        command.extend(["-e", str(options.page_end)])
    return command


def strip_console_codes(line: str) -> str:
    line = ANSI_RE.sub("", line).replace("\r", "").strip()
    if len(line) > 800:
        line = line[-800:]
    return line


def detect_stage(line: str) -> int | None:
    lower = line.lower()
    if "started local mineru-api" in lower or "start mineru fastapi" in lower:
        return 1
    if "submitting batch" in lower:
        return 2
    if "queued" in lower or "waiting in queue" in lower:
        return 3
    parsing_markers = (
        "processing window",
        "processing pages",
        "layout predict",
        "ocr-det",
        "ocr-rec",
        "table predict",
        "using lmdeploy",
        "get lmdeploy-engine predictor",
        "convert to turbomind",
    )
    if any(marker in lower for marker in parsing_markers):
        return 4
    if "completed batch" in lower or "downloading result" in lower or "generating output" in lower:
        return 5
    return None


def find_markdown(output: Path, started_at: float) -> list[Path]:
    if not output.exists():
        return []
    files = []
    for item in output.rglob("*.md"):
        try:
            if item.stat().st_mtime >= started_at - 3:
                files.append(item)
        except OSError:
            continue
    return sorted(files, key=lambda item: item.stat().st_mtime)


def copy_markdown(markdown_files: list[Path], destination: Path) -> list[Path]:
    if not markdown_files:
        return []
    copied: list[Path] = []
    if len(markdown_files) == 1 and destination.suffix.lower() == ".md":
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(markdown_files[0], destination)
        return [destination]

    destination.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    for index, source in enumerate(markdown_files, start=1):
        name = source.name
        if name.lower() in used_names:
            name = f"{source.stem}-{index}.md"
        used_names.add(name.lower())
        target = destination / name
        shutil.copy2(source, target)
        copied.append(target)
    return copied


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
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


def run_conversion(
    options: ConversionOptions,
    emit: EventCallback,
    cancel_event: threading.Event,
    process_ready: Callable[[subprocess.Popen[str]], None] | None = None,
) -> RunResult:
    paths = validate_runtime()
    if not options.source.exists():
        raise FileNotFoundError(f"输入不存在：{options.source}")
    if (options.page_start is not None or options.page_end is not None) and options.source.is_file():
        if options.source.suffix.lower() != ".pdf":
            raise ValueError("分页参数目前只适用于 PDF 文件。")

    options.output.mkdir(parents=True, exist_ok=True)
    command = build_command(options, paths)
    emit("stage", 0)
    emit("message", "正在准备本地运行环境…")
    emit("command", command)

    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )

    started_at = time.time()
    process = subprocess.Popen(
        command,
        cwd=str(paths["root"]),
        env=build_runtime_env(paths),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creation_flags,
    )
    if process_ready:
        process_ready(process)

    line_queue: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                line_queue.put(raw_line)
        finally:
            line_queue.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    last_stage = 0
    reader_done = False

    while process.poll() is None or not reader_done:
        if cancel_event.is_set() and process.poll() is None:
            emit("message", "正在取消任务…")
            terminate_process_tree(process)
        try:
            raw = line_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if raw is None:
            reader_done = True
            continue
        clean = strip_console_codes(raw)
        if not clean:
            continue
        emit("line", clean)
        stage = detect_stage(clean)
        if stage is not None and stage > last_stage:
            last_stage = stage
            emit("stage", stage)
            emit("message", STAGES[stage])

    exit_code = process.wait()
    elapsed = time.time() - started_at
    if cancel_event.is_set():
        raise RuntimeError("任务已取消。")
    if exit_code != 0:
        raise RuntimeError(f"MinerU 转换失败，退出码：{exit_code}")

    emit("stage", 5)
    emit("message", "正在定位 Markdown 输出…")
    markdown_files = find_markdown(options.output, started_at)
    if not markdown_files:
        raise RuntimeError("MinerU 已结束，但没有找到本次生成的 Markdown 文件。请查看日志。")
    if options.md_output:
        copied = copy_markdown(markdown_files, options.md_output)
        if copied:
            markdown_files = copied

    emit("stage", 6)
    emit("message", f"转换完成，共生成 {len(markdown_files)} 个 Markdown 文件。")
    return RunResult(exit_code, markdown_files, command, elapsed)


def display_command(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return " ".join(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="MinerU-Local.exe",
        description="MinerU 本地 GUI/CLI 启动器。无参数启动图形界面；带参数直接解析文档。",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "示例：\n"
            "  MinerU-Local.exe paper.pdf\n"
            "  MinerU-Local.exe paper.pdf -o .\\result --page 3\n"
            "  MinerU-Local.exe paper.pdf --pages 3-8 --md-output .\\result\\pages-3-8.md\n"
            "  MinerU-Local.exe .\\input -o .\\output -b pipeline\n"
            "\n页码按阅读习惯从 1 开始；MinerU 原生命令的 0 基页码会由本程序自动换算。"
        ),
    )
    parser.add_argument("input", nargs="?", help="输入文件或目录")
    parser.add_argument("-p", "--path", dest="input_option", help="输入文件或目录（等同于位置参数）")
    parser.add_argument("-o", "--output", help="输出目录，默认是程序目录下的 output")
    parser.add_argument(
        "-b",
        "--backend",
        choices=("hybrid-engine", "vlm-engine", "pipeline"),
        default="hybrid-engine",
        help="解析后端，默认 hybrid-engine",
    )
    parser.add_argument("-m", "--method", choices=("auto", "txt", "ocr"), default="auto")
    parser.add_argument("--effort", choices=("medium", "high"), default="medium")
    parser.add_argument("-l", "--lang", default="ch", help="OCR 语言，默认 ch")
    pages = parser.add_mutually_exclusive_group()
    pages.add_argument("--page", type=int, help="只解析某一页，页码从 1 开始")
    pages.add_argument("--pages", help="解析连续页码，例如 3-8（从 1 开始）")
    parser.add_argument("--no-formula", action="store_true", help="关闭公式识别")
    parser.add_argument("--no-table", action="store_true", help="关闭表格识别")
    parser.add_argument("--no-image-analysis", action="store_true", help="关闭图片/图表分析")
    parser.add_argument(
        "--md-output",
        help="额外复制 Markdown 到指定 .md 文件或目录；不会删除 MinerU 的完整输出",
    )
    parser.add_argument("--open-output", action="store_true", help="完成后打开输出目录")
    parser.add_argument("--dry-run", action="store_true", help="只显示将执行的 MinerU 命令")
    parser.add_argument("--gui", action="store_true", help="强制启动图形界面")
    parser.add_argument("--version", action="version", version=f"MinerU Local {APP_VERSION}")
    return parser


def cli_main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    source_value = args.input_option or args.input
    if not source_value:
        parser.error("请提供输入文件/目录，或不带参数启动 GUI。")

    source = Path(source_value).expanduser().resolve()
    output = Path(args.output).expanduser().resolve() if args.output else runtime_paths()["default_output"]
    page_start = page_end = None
    if args.page is not None:
        page_start, page_end = parse_pages(str(args.page))
    elif args.pages:
        page_start, page_end = parse_pages(args.pages)

    options = ConversionOptions(
        source=source,
        output=output,
        backend=args.backend,
        method=args.method,
        effort=args.effort,
        language=args.lang,
        page_start=page_start,
        page_end=page_end,
        formula=not args.no_formula,
        table=not args.no_table,
        image_analysis=not args.no_image_analysis,
        md_output=Path(args.md_output).expanduser().resolve() if args.md_output else None,
        open_output=args.open_output,
    )
    paths = validate_runtime()
    command = build_command(options, paths)
    if args.dry_run:
        print(display_command(command))
        return 0

    print(f"MinerU Local {APP_VERSION}")
    print(f"输入：{source}")
    print(f"输出：{output}")
    print(f"后端：{options.backend}")
    if page_start is not None:
        print(f"页码：{page_start + 1}-{page_end + 1}")
    print()

    cancel_event = threading.Event()
    current_stage = -1

    def emit(kind: str, value: object) -> None:
        nonlocal current_stage
        if kind == "stage":
            stage = int(value)
            if stage != current_stage:
                current_stage = stage
                print(f"[状态] {STAGES[stage]}")
        elif kind == "line":
            print(str(value))
        elif kind == "command":
            print(f"[命令] {display_command(value)}")  # type: ignore[arg-type]

    active_process: list[subprocess.Popen[str]] = []
    try:
        result = run_conversion(options, emit, cancel_event, active_process.append)
    except KeyboardInterrupt:
        cancel_event.set()
        if active_process:
            terminate_process_tree(active_process[-1])
        print("\n任务已取消。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1

    print(f"\n完成，用时 {result.elapsed_seconds:.1f} 秒。")
    for markdown in result.markdown_files:
        print(f"Markdown：{markdown}")
    if options.open_output and os.name == "nt":
        os.startfile(str(options.output))  # type: ignore[attr-defined]
    return 0


class MinerUApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.paths = runtime_paths()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.active_process: subprocess.Popen[str] | None = None
        self.running = False
        self.last_result: RunResult | None = None
        self.stage_index = 0

        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(self.paths["default_output"]))
        self.pages_var = tk.StringVar()
        self.backend_var = tk.StringVar(value="hybrid-engine")
        self.method_var = tk.StringVar(value="auto")
        self.effort_var = tk.StringVar(value="medium")
        self.formula_var = tk.BooleanVar(value=True)
        self.table_var = tk.BooleanVar(value=True)
        self.image_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="选择文件后开始转换。")

        self._configure_window()
        self._build_styles()
        self._build_ui()
        self._set_stage(0)
        self.root.after(100, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_window(self) -> None:
        self.root.title(f"{APP_NAME}  ·  v{APP_VERSION}")
        self.root.configure(bg=BG)
        self.root.geometry("940x650")
        self.root.minsize(840, 600)
        self.root.update_idletasks()
        width, height = 940, 650
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Dark.TCombobox",
            fieldbackground=CARD_ALT,
            background=CARD_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            arrowcolor=MUTED,
            padding=7,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", CARD_ALT)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", CARD_ALT)],
            selectforeground=[("readonly", TEXT)],
        )
        self.root.option_add("*TCombobox*Listbox.background", CARD_ALT)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor=CARD_ALT,
            background=ACCENT,
            bordercolor=CARD_ALT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        primary: bool = False,
        width: int | None = None,
    ) -> tk.Button:
        background = ACCENT if primary else CARD_ALT
        active = ACCENT_HOVER if primary else BORDER
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=background,
            fg="white" if primary else TEXT,
            activebackground=active,
            activeforeground="white",
            disabledforeground="#667080",
            relief="flat",
            bd=0,
            padx=14,
            pady=9,
            width=width,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold" if primary else "normal"),
        )

    def _entry(self, parent: tk.Misc, variable: tk.StringVar) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            bg=CARD_ALT,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground=ACCENT,
            selectforeground="white",
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=("Microsoft YaHei UI", 9),
        )

    def _label(self, parent: tk.Misc, text: str, muted: bool = False, **kwargs: object) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=kwargs.pop("bg", CARD),
            fg=MUTED if muted else TEXT,
            font=kwargs.pop("font", ("Microsoft YaHei UI", 9)),
            **kwargs,
        )

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=26, pady=(22, 18))

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", pady=(0, 18))
        tk.Label(
            header,
            text="MinerU 本地转换器",
            bg=BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 20, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="选择文档，使用本地 GPU 与模型转换为 Markdown",
            bg=BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(4, 0))

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=11, uniform="column")
        body.grid_columnconfigure(1, weight=9, uniform="column")
        body.grid_rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        right = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._build_form(left)
        self._build_status(right)

        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", pady=(12, 0))
        tk.Label(
            footer,
            text=f"MinerU Local v{APP_VERSION}  ·  模型与数据均在本机处理",
            bg=BG,
            fg="#647083",
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left")

    def _build_form(self, card: tk.Frame) -> None:
        content = tk.Frame(card, bg=CARD)
        content.pack(fill="both", expand=True, padx=22, pady=20)
        content.grid_columnconfigure(0, weight=1)

        self._label(content, "转换设置", font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 15)
        )

        self._label(content, "输入文件或目录", muted=True).grid(row=1, column=0, sticky="w", pady=(0, 6))
        source_row = tk.Frame(content, bg=CARD)
        source_row.grid(row=2, column=0, sticky="ew")
        source_row.grid_columnconfigure(0, weight=1)
        source_entry = self._entry(source_row, self.source_var)
        source_entry.grid(row=0, column=0, sticky="ew", ipady=9)
        self._button(source_row, "文件", self._choose_file).grid(row=0, column=1, padx=(8, 0))
        self._button(source_row, "目录", self._choose_folder).grid(row=0, column=2, padx=(6, 0))

        self._label(content, "输出目录", muted=True).grid(row=3, column=0, sticky="w", pady=(14, 6))
        output_row = tk.Frame(content, bg=CARD)
        output_row.grid(row=4, column=0, sticky="ew")
        output_row.grid_columnconfigure(0, weight=1)
        self._entry(output_row, self.output_var).grid(row=0, column=0, sticky="ew", ipady=9)
        self._button(output_row, "选择", self._choose_output).grid(row=0, column=1, padx=(8, 0))

        fields = tk.Frame(content, bg=CARD)
        fields.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        for column in (0, 1):
            fields.grid_columnconfigure(column, weight=1, uniform="field")

        page_box = tk.Frame(fields, bg=CARD)
        page_box.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self._label(page_box, "页码（可选）", muted=True).pack(anchor="w", pady=(0, 6))
        self._entry(page_box, self.pages_var).pack(fill="x", ipady=8)
        self._label(page_box, "如 3 或 3-8；留空解析全部", muted=True, font=("Microsoft YaHei UI", 8)).pack(
            anchor="w", pady=(4, 0)
        )

        backend_box = tk.Frame(fields, bg=CARD)
        backend_box.grid(row=0, column=1, sticky="ew", padx=(7, 0))
        self._label(backend_box, "解析后端", muted=True).pack(anchor="w", pady=(0, 6))
        ttk.Combobox(
            backend_box,
            textvariable=self.backend_var,
            values=("hybrid-engine", "pipeline", "vlm-engine"),
            state="readonly",
            style="Dark.TCombobox",
        ).pack(fill="x")
        self._label(backend_box, "hybrid-engine 推荐", muted=True, font=("Microsoft YaHei UI", 8)).pack(
            anchor="w", pady=(4, 0)
        )

        modes = tk.Frame(content, bg=CARD)
        modes.grid(row=6, column=0, sticky="ew", pady=(13, 0))
        for column in (0, 1):
            modes.grid_columnconfigure(column, weight=1, uniform="mode")
        method_box = tk.Frame(modes, bg=CARD)
        method_box.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self._label(method_box, "识别方式", muted=True).pack(anchor="w", pady=(0, 6))
        ttk.Combobox(
            method_box,
            textvariable=self.method_var,
            values=("auto", "txt", "ocr"),
            state="readonly",
            style="Dark.TCombobox",
        ).pack(fill="x")
        effort_box = tk.Frame(modes, bg=CARD)
        effort_box.grid(row=0, column=1, sticky="ew", padx=(7, 0))
        self._label(effort_box, "解析强度", muted=True).pack(anchor="w", pady=(0, 6))
        ttk.Combobox(
            effort_box,
            textvariable=self.effort_var,
            values=("medium", "high"),
            state="readonly",
            style="Dark.TCombobox",
        ).pack(fill="x")

        toggles = tk.Frame(content, bg=CARD)
        toggles.grid(row=7, column=0, sticky="w", pady=(14, 0))
        for column, (text, variable) in enumerate(
            (("公式识别", self.formula_var), ("表格识别", self.table_var), ("图片分析", self.image_var))
        ):
            tk.Checkbutton(
                toggles,
                text=text,
                variable=variable,
                bg=CARD,
                fg=TEXT,
                activebackground=CARD,
                activeforeground=TEXT,
                selectcolor=CARD_ALT,
                highlightthickness=0,
                font=("Microsoft YaHei UI", 9),
            ).grid(row=0, column=column, padx=(0, 14))

        action_row = tk.Frame(content, bg=CARD)
        action_row.grid(row=8, column=0, sticky="ew", pady=(20, 0))
        action_row.grid_columnconfigure(0, weight=1)
        self.start_button = self._button(action_row, "开始转换", self._start_conversion, primary=True)
        self.start_button.grid(row=0, column=0, sticky="ew")
        self.cancel_button = self._button(action_row, "取消", self._cancel_conversion, width=7)
        self.cancel_button.grid(row=0, column=1, padx=(8, 0))
        self.cancel_button.configure(state="disabled")

    def _build_status(self, card: tk.Frame) -> None:
        content = tk.Frame(card, bg=CARD)
        content.pack(fill="both", expand=True, padx=22, pady=20)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(5, weight=1)

        self._label(content, "任务状态", font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        stages_frame = tk.Frame(content, bg=CARD)
        stages_frame.grid(row=1, column=0, sticky="ew", pady=(15, 12))
        for column in (0, 1):
            stages_frame.grid_columnconfigure(column, weight=1, uniform="stage")
        self.stage_dots: list[tk.Label] = []
        self.stage_labels: list[tk.Label] = []
        for index, stage in enumerate(STAGES):
            row = index // 2
            column = index % 2
            item = tk.Frame(stages_frame, bg=CARD)
            item.grid(row=row, column=column, sticky="w", pady=5)
            dot = tk.Label(item, text="●", bg=CARD, fg="#3C4554", font=("Segoe UI Symbol", 9))
            dot.pack(side="left")
            label = self._label(item, stage, muted=True)
            label.pack(side="left", padx=(7, 0))
            self.stage_dots.append(dot)
            self.stage_labels.append(label)

        ttk.Separator(content, orient="horizontal").grid(row=2, column=0, sticky="ew", pady=(2, 12))
        self.progress = ttk.Progressbar(
            content,
            mode="determinate",
            maximum=100,
            value=0,
            style="Accent.Horizontal.TProgressbar",
        )
        self.progress.grid(row=3, column=0, sticky="ew")
        self._label(content, "", muted=True, textvariable=self.status_var, wraplength=360, justify="left").grid(
            row=4, column=0, sticky="w", pady=(8, 12)
        )

        log_frame = tk.Frame(content, bg=CARD_ALT, highlightthickness=1, highlightbackground=BORDER)
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)
        self.log = tk.Text(
            log_frame,
            bg=CARD_ALT,
            fg="#B9C4D4",
            insertbackground=TEXT,
            relief="flat",
            bd=0,
            padx=10,
            pady=9,
            wrap="word",
            height=10,
            font=("Cascadia Mono", 8),
            state="disabled",
        )
        scrollbar = tk.Scrollbar(log_frame, command=self.log.yview, bg=CARD_ALT, troughcolor=CARD_ALT)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.open_button = self._button(content, "打开输出目录", self._open_output)
        self.open_button.grid(row=6, column=0, sticky="ew", pady=(12, 0))

    def _choose_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择要转换的文件",
            initialdir=str(self.paths["root"] / "input"),
            filetypes=(
                ("支持的文档", "*.pdf *.png *.jpg *.jpeg *.webp *.docx *.pptx *.xlsx"),
                ("PDF", "*.pdf"),
                ("所有文件", "*.*"),
            ),
        )
        if selected:
            self.source_var.set(selected)

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="选择输入目录", initialdir=str(self.paths["root"] / "input"))
        if selected:
            self.source_var.set(selected)

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="选择输出目录", initialdir=self.output_var.get())
        if selected:
            self.output_var.set(selected)

    def _open_output(self) -> None:
        target = Path(self.output_var.get().strip() or self.paths["default_output"])
        target.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 350:
            self.log.delete("1.0", "80.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_stage(self, stage: int, error: bool = False) -> None:
        self.stage_index = stage
        for index, (dot, label) in enumerate(zip(self.stage_dots, self.stage_labels)):
            if error and index == stage:
                color = DANGER
            elif index < stage or (stage == len(STAGES) - 1 and index <= stage):
                color = SUCCESS
            elif index == stage:
                color = ACCENT
            else:
                color = "#3C4554"
            dot.configure(fg=color)
            label.configure(fg=TEXT if index <= stage else MUTED)
        self.progress["value"] = round(stage / (len(STAGES) - 1) * 100)

    def _start_conversion(self) -> None:
        if self.running:
            return
        try:
            validate_runtime()
            source_text = self.source_var.get().strip().strip('"')
            if not source_text:
                raise ValueError("请先选择输入文件或目录。")
            source = Path(source_text).expanduser().resolve()
            if not source.exists():
                raise ValueError(f"输入不存在：{source}")
            output_text = self.output_var.get().strip().strip('"')
            output = Path(output_text).expanduser().resolve() if output_text else self.paths["default_output"]
            page_start = page_end = None
            if self.pages_var.get().strip():
                page_start, page_end = parse_pages(self.pages_var.get())
            options = ConversionOptions(
                source=source,
                output=output,
                backend=self.backend_var.get(),
                method=self.method_var.get(),
                effort=self.effort_var.get(),
                page_start=page_start,
                page_end=page_end,
                formula=self.formula_var.get(),
                table=self.table_var.get(),
                image_analysis=self.image_var.get(),
            )
        except Exception as exc:
            messagebox.showerror("无法开始", str(exc), parent=self.root)
            return

        self.running = True
        self.last_result = None
        self.active_process = None
        self.cancel_event.clear()
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_var.set("正在准备任务…")
        self._set_stage(0)
        self._append_log("—" * 48)
        self._append_log(f"输入：{options.source}")
        self._append_log(f"输出：{options.output}")

        def emit(kind: str, value: object) -> None:
            self.events.put((kind, value))

        def worker() -> None:
            try:
                result = run_conversion(options, emit, self.cancel_event, self._remember_process)
                self.events.put(("result", result))
            except Exception as exc:
                self.events.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _remember_process(self, process: subprocess.Popen[str]) -> None:
        self.active_process = process

    def _cancel_conversion(self) -> None:
        if not self.running:
            return
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.status_var.set("正在取消任务…")
        if self.active_process:
            threading.Thread(target=terminate_process_tree, args=(self.active_process,), daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "line":
                    self._append_log(str(value))
                elif kind == "stage":
                    self._set_stage(int(value))
                elif kind == "message":
                    self.status_var.set(str(value))
                elif kind == "command":
                    self._append_log("命令：" + display_command(value))  # type: ignore[arg-type]
                elif kind == "result":
                    self._finish_success(value)  # type: ignore[arg-type]
                elif kind == "error":
                    self._finish_error(value)  # type: ignore[arg-type]
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _finish_success(self, result: RunResult) -> None:
        self.running = False
        self.last_result = result
        self.active_process = None
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self._set_stage(6)
        self.status_var.set(f"转换完成 · {len(result.markdown_files)} 个 Markdown · {result.elapsed_seconds:.1f} 秒")
        self._append_log(f"完成，用时 {result.elapsed_seconds:.1f} 秒")
        for markdown in result.markdown_files:
            self._append_log(f"Markdown：{markdown}")

    def _finish_error(self, error: Exception) -> None:
        self.running = False
        self.active_process = None
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self._set_stage(self.stage_index, error=True)
        self.status_var.set(str(error))
        self._append_log(f"错误：{error}")
        if "取消" not in str(error):
            messagebox.showerror("转换失败", str(error), parent=self.root)

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("任务仍在运行", "关闭窗口将取消当前任务，是否继续？", parent=self.root):
                return
            self.cancel_event.set()
            if self.active_process:
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
        # A PyInstaller one-file app has a bootloader parent and an app child.
        # Explorer launch therefore reports two attached processes; a real
        # PowerShell/CMD launch reports at least one additional console host.
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
        validate_runtime()
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("MinerU 环境不可用", str(exc))
        root.destroy()
        return 1
    MinerUApp(root)
    root.mainloop()
    return 0


def main() -> int:
    configure_standard_streams()
    if len(sys.argv) == 1 or "--gui" in sys.argv[1:]:
        return gui_main()
    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
