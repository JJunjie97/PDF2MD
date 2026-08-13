from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
from pathlib import Path

from pdf2md_core import CORE_VERSION, ConversionOptions, RunResult, run_conversion


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2md",
        description="将 PDF 转换为简洁的 Markdown + images。",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "示例：\n"
            "  pdf2md paper.pdf\n"
            "  pdf2md paper.pdf --pages 3-8\n"
            "  pdf2md paper.pdf --pages 1-3,8,12-15 --profile fast --json\n\n"
            "默认在 PDF 同级创建 <文件名>.pdf2md，公开结果只有 Markdown、images 和 raw。"
        ),
    )
    parser.add_argument("pdf", help="输入 PDF 文件")
    parser.add_argument("-o", "--output", help="指定输出目录；默认是 PDF 同级的 <文件名>.pdf2md")
    pages = parser.add_mutually_exclusive_group()
    pages.add_argument("--page", type=int, help="只转换一个物理 PDF 页码（从 1 开始）")
    pages.add_argument("--pages", help="页码或页段，例如 3-8 或 1-3,8,12-15")
    parser.add_argument(
        "--profile",
        choices=("fast", "balanced", "accurate"),
        default="balanced",
        help="fast=Pipeline 高速；balanced=Hybrid 均衡；accurate=Hybrid 高精度",
    )
    method = parser.add_mutually_exclusive_group()
    method.add_argument(
        "--method",
        choices=("auto", "txt", "ocr"),
        default="auto",
        help="解析方式：auto=自动；txt=文本优先；ocr=强制 OCR",
    )
    method.add_argument("--ocr", action="store_true", help="强制 OCR；等同于 --method ocr")
    parser.add_argument("-l", "--lang", default="ch", help="OCR 语言，默认 ch")
    parser.add_argument("--force", action="store_true", help="忽略匹配缓存并重新转换")
    parser.add_argument("--timeout", type=int, default=1800, help="总超时秒数，默认 1800")
    parser.add_argument("--json", action="store_true", help="仅在 stdout 返回机器可读 JSON")
    parser.add_argument("--version", action="version", version=f"PDF2MD {CORE_VERSION}")
    return parser


def result_payload(result: RunResult) -> dict[str, object]:
    return {
        "ok": True,
        "tool_version": CORE_VERSION,
        "markdown": str(result.markdown),
        "images_dir": str(result.images),
        "output_dir": str(result.output),
        "pages": result.pages,
        "profile": result.profile,
        "cache": result.cache,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
    }


def configure_streams() -> None:
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


def main(argv: list[str] | None = None) -> int:
    configure_streams()
    args = build_parser().parse_args(argv)
    page_expression = str(args.page) if args.page is not None else args.pages
    options = ConversionOptions(
        source=Path(args.pdf).expanduser().resolve(),
        output=Path(args.output).expanduser().resolve() if args.output else None,
        pages=page_expression,
        profile=args.profile,
        method="ocr" if args.ocr else args.method,
        language=args.lang,
        force=args.force,
        timeout=args.timeout,
    )
    cancel_event = threading.Event()

    def emit(kind: str, value: object) -> None:
        if kind == "message":
            print(f"[状态] {value}", file=sys.stderr)
        elif kind == "progress":
            print(f"[进度] {json.dumps(value, ensure_ascii=False)}", file=sys.stderr)
        elif kind == "line" and not args.json:
            print(str(value), file=sys.stderr)

    try:
        result = run_conversion(options, emit=emit, cancel_event=cancel_event)
    except KeyboardInterrupt:
        cancel_event.set()
        payload = {"ok": False, "error_code": "CANCELLED", "message": "转换已取消。"}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(payload["message"], file=sys.stderr)
        return 130
    except Exception as exc:
        payload = {"ok": False, "error_code": "CONVERSION_FAILED", "message": str(exc)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"错误：{exc}", file=sys.stderr)
        return 1

    payload = result_payload(result)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"Markdown：{result.markdown}")
        print(f"图片目录：{result.images}")
        print(f"用时：{result.elapsed_seconds:.1f} 秒；缓存：{result.cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
