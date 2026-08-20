from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path

from pdf2md_core import (
    CORE_VERSION,
    ConversionError,
    ConversionOptions,
    ConversionSession,
    RunResult,
    run_conversion,
)


def _add_page_options(parser: argparse.ArgumentParser) -> None:
    pages = parser.add_mutually_exclusive_group()
    pages.add_argument("--page", type=int, help="只转换一个物理 PDF 页码（从 1 开始）")
    pages.add_argument("--pages", help="页码或页段，例如 3-8 或 1-3,8,12-15")


def _add_conversion_options(parser: argparse.ArgumentParser) -> None:
    _add_page_options(parser)
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
    parser.add_argument("--timeout", type=int, default=1800, help="每个 PDF 的超时秒数，默认 1800")
    parser.add_argument("--json", action="store_true", help="仅在 stdout 返回机器可读 JSON")


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
            "批量与模型会话：\n"
            "  pdf2md batch D:\\docs --recursive --load-model\n"
            "  pdf2md preload --profile balanced\n\n"
            "默认在 PDF 同级创建 <文件名>.pdf2md，公开结果只有 Markdown、images 和 raw。"
        ),
    )
    parser.add_argument("pdf", help="输入 PDF 文件")
    parser.add_argument("-o", "--output", help="指定输出目录；默认是 PDF 同级的 <文件名>.pdf2md")
    _add_conversion_options(parser)
    parser.add_argument("--version", action="version", version=f"PDF2MD {CORE_VERSION}")
    return parser


def build_batch_parser(prog: str = "pdf2md batch") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="在一个 OCR 进程内连续转换多个 PDF，模型只加载一次。",
    )
    parser.add_argument("inputs", nargs="+", help="一个或多个 PDF 文件/目录")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归查找目录内 PDF")
    parser.add_argument(
        "-o",
        "--output-root",
        help="批量输出根目录；默认每个 PDF 仍输出到自身同级目录",
    )
    parser.add_argument(
        "--load-model",
        "--preload-model",
        dest="load_model",
        action="store_true",
        help="处理队列前真实加载模型；未指定时在首个任务中加载并继续保留",
    )
    parser.add_argument(
        "--startup-timeout",
        type=int,
        default=600,
        help="引擎启动/模型预热超时秒数，默认 600",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="首个 PDF 失败后停止；默认继续处理其余文件",
    )
    _add_conversion_options(parser)
    return parser


def build_session_parser(prog: str = "pdf2md preload") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="加载模型并进入前台 OCR 会话；exit、EOF 或 Ctrl+C 自动释放 GPU。",
    )
    parser.add_argument(
        "--profile",
        choices=("fast", "balanced", "accurate"),
        default="balanced",
    )
    method = parser.add_mutually_exclusive_group()
    method.add_argument("--method", choices=("auto", "txt", "ocr"), default="auto")
    method.add_argument("--ocr", action="store_true", help="等同于 --method ocr")
    parser.add_argument("-l", "--lang", default="ch")
    parser.add_argument("--timeout", type=int, default=1800, help="会话内每个 PDF 的默认超时")
    parser.add_argument("--startup-timeout", type=int, default=600)
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


def _method(args: argparse.Namespace) -> str:
    return "ocr" if getattr(args, "ocr", False) else str(args.method)


def _page_expression(args: argparse.Namespace) -> str | None:
    return str(args.page) if getattr(args, "page", None) is not None else args.pages


def _conversion_options(
    source: Path,
    args: argparse.Namespace,
    *,
    output: Path | None,
) -> ConversionOptions:
    return ConversionOptions(
        source=source,
        output=output,
        pages=_page_expression(args),
        profile=str(args.profile),
        method=_method(args),
        language=str(args.lang),
        force=bool(args.force),
        timeout=int(args.timeout),
    )


def _emit_callback(
    *,
    json_mode: bool,
    label: dict[str, str] | None = None,
):
    def emit(kind: str, value: object) -> None:
        prefix = f"[{label['value']}] " if label and label.get("value") else ""
        if kind == "message":
            print(f"{prefix}[状态] {value}", file=sys.stderr)
        elif kind == "progress":
            print(
                f"{prefix}[进度] {json.dumps(value, ensure_ascii=False)}",
                file=sys.stderr,
            )
        elif kind == "line" and not json_mode:
            print(f"{prefix}{value}", file=sys.stderr)

    return emit


def discover_pdf_inputs(values: list[str], recursive: bool = False) -> list[Path]:
    discovered: list[Path] = []
    for value in values:
        path = Path(value).expanduser().resolve()
        if path.is_file():
            if path.suffix.casefold() != ".pdf":
                raise ConversionError(f"批量输入不是 PDF：{path}")
            discovered.append(path)
            continue
        if not path.is_dir():
            raise ConversionError(f"找不到批量输入：{path}")
        iterator = path.rglob("*") if recursive else path.iterdir()
        for candidate in iterator:
            if not candidate.is_file() or candidate.suffix.casefold() != ".pdf":
                continue
            if any(
                parent.name.casefold().endswith((".pdf2md", ".mineru"))
                for parent in candidate.parents
            ):
                continue
            discovered.append(candidate.resolve())

    unique: dict[str, Path] = {}
    for path in discovered:
        unique.setdefault(os.path.normcase(str(path)), path)
    result = sorted(unique.values(), key=lambda item: str(item).casefold())
    if not result:
        raise ConversionError("批量输入中没有找到 PDF。")
    return result


def batch_output_paths(sources: list[Path], output_root: str | None) -> dict[Path, Path | None]:
    if not output_root:
        return {source: None for source in sources}
    root = Path(output_root).expanduser().resolve()
    outputs: dict[Path, Path | None] = {}
    for source in sources:
        # A path-derived suffix keeps the mapping stable even when a later
        # batch contains only one of several same-stem PDFs. The default
        # sibling output remains the simple <stem>.pdf2md form.
        digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:8]
        outputs[source] = root / f"{source.stem}-{digest}.pdf2md"
    return outputs


def execute_batch(
    args: argparse.Namespace,
    *,
    shared_session: ConversionSession | None = None,
) -> dict[str, object]:
    sources = discover_pdf_inputs(list(args.inputs), bool(args.recursive))
    outputs = batch_output_paths(sources, args.output_root)
    label = {"value": ""}
    started = time.monotonic()
    session = shared_session
    owns_session = session is None
    if session is None:
        session = ConversionSession(
            profile=str(args.profile),
            method=_method(args),
            language=str(args.lang),
            emit=_emit_callback(json_mode=bool(args.json), label=label),
            cancel_event=threading.Event(),
            preload_model=bool(args.load_model),
            startup_timeout=float(args.startup_timeout),
        )

    results: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    try:
        if owns_session:
            session.start()
        for source in sources:
            label["value"] = source.name
            try:
                result = session.convert(
                    _conversion_options(source, args, output=outputs[source])
                )
            except KeyboardInterrupt:
                session.cancel_event.set()
                raise
            except Exception as exc:
                errors.append(
                    {
                        "ok": False,
                        "source": str(source),
                        "error_code": "CONVERSION_FAILED",
                        "message": str(exc),
                    }
                )
                if args.fail_fast:
                    break
                continue
            payload = result_payload(result)
            payload["source"] = str(source)
            results.append(payload)
    finally:
        label["value"] = ""
        if owns_session:
            session.close()

    processed = len(results) + len(errors)
    return {
        "ok": not errors,
        "tool_version": CORE_VERSION,
        "mode": "batch",
        "total": len(sources),
        "processed": processed,
        "succeeded": len(results),
        "failed": len(errors),
        "skipped": len(sources) - processed,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "results": results,
        "errors": errors,
    }


def _print_batch_summary(summary: dict[str, object], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(summary, ensure_ascii=False))
        return
    for item in summary["results"]:
        assert isinstance(item, dict)
        print(f"完成：{item['source']}")
        print(f"  Markdown：{item['markdown']}")
    for item in summary["errors"]:
        assert isinstance(item, dict)
        print(f"失败：{item['source']}：{item['message']}", file=sys.stderr)
    print(
        "批量完成："
        f"{summary['succeeded']} 成功，{summary['failed']} 失败，"
        f"{summary['skipped']} 跳过；用时 {summary['elapsed_seconds']} 秒"
    )


def _split_session_line(line: str) -> list[str]:
    tokens = shlex.split(line, posix=False)
    cleaned: list[str] = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
            token = token[1:-1]
        cleaned.append(token)
    return cleaned


def _assert_session_settings(args: argparse.Namespace, session: ConversionSession) -> None:
    if (
        str(args.profile) != session.profile
        or _method(args) != session.method
        or str(args.lang) != session.language
    ):
        raise ConversionError("会话中的 profile、method 和 lang 已固定；请退出后重新加载模型。")


def _session_help() -> str:
    return (
        "命令：\n"
        "  convert <PDF> [--pages 1-3] [-o 输出目录] [--force]\n"
        "  batch <PDF或目录...> [-r] [-o 输出根目录] [--fail-fast]\n"
        "  status\n"
        "  help\n"
        "  exit\n"
        "也可以直接输入 PDF 路径，等同于 convert。"
    )


def _configure_piped_session_stdin() -> None:
    """Decode PowerShell's BOM-prefixed native pipe as UTF-8.

    Windows PowerShell writes a UTF-8 BOM for the first piped string while the
    embedded Python runtime may expose stdin as GBK.  Looking at the raw prefix
    before TextIOWrapper consumes it lets us select the correct decoder without
    changing interactive consoles or non-UTF-8 pipes.
    """
    stream = sys.stdin
    try:
        if stream.isatty():
            return
    except (AttributeError, OSError):
        return
    buffer = getattr(stream, "buffer", None)
    reconfigure = getattr(stream, "reconfigure", None)
    peek = getattr(buffer, "peek", None)
    if not callable(reconfigure) or not callable(peek):
        return
    try:
        prefix = bytes(peek(3)[:3])
    except (OSError, ValueError):
        return
    if prefix == b"\xef\xbb\xbf":
        try:
            reconfigure(encoding="utf-8-sig")
        except (LookupError, OSError, ValueError):
            return


def run_interactive_session(args: argparse.Namespace) -> int:
    _configure_piped_session_stdin()
    method = _method(args)
    cancel_event = threading.Event()
    emit = _emit_callback(json_mode=False)
    session = ConversionSession(
        profile=str(args.profile),
        method=method,
        language=str(args.lang),
        emit=emit,
        cancel_event=cancel_event,
        preload_model=True,
        startup_timeout=float(args.startup_timeout),
    )
    try:
        with session:
            preload = session.preload_result or {}
            elapsed = preload.get("elapsed_seconds", "?")
            gpu = preload.get("gpu", {})
            print(
                f"模型已加载：profile={session.profile} method={session.method} "
                f"lang={session.language}；预热 {elapsed} 秒"
            )
            if isinstance(gpu, dict) and gpu:
                print(f"设备：{json.dumps(gpu, ensure_ascii=False)}")
            print(_session_help())
            while True:
                try:
                    # A correctly decoded BOM is normally consumed by
                    # utf-8-sig; lstrip also supports direct callers/tests.
                    line = input("PDF2MD> ").lstrip("\ufeff").strip()
                except EOFError:
                    print()
                    break
                if not line:
                    continue
                tokens = _split_session_line(line)
                if not tokens:
                    continue
                command = tokens[0].casefold()
                if command in {"exit", "quit"}:
                    break
                if command == "help":
                    print(_session_help())
                    continue
                if command == "status":
                    print(
                        f"ready profile={session.profile} method={session.method} "
                        f"lang={session.language}"
                    )
                    continue
                if command not in {"convert", "batch"}:
                    tokens.insert(0, "convert")
                    command = "convert"
                try:
                    if command == "convert":
                        parser = build_parser()
                        parser.set_defaults(
                            profile=session.profile,
                            method=session.method,
                            lang=session.language,
                            timeout=args.timeout,
                        )
                        parsed = parser.parse_args(tokens[1:])
                        _assert_session_settings(parsed, session)
                        source = Path(parsed.pdf).expanduser().resolve()
                        output = (
                            Path(parsed.output).expanduser().resolve()
                            if parsed.output
                            else None
                        )
                        result = session.convert(
                            _conversion_options(source, parsed, output=output)
                        )
                        print(f"Markdown：{result.markdown}")
                        print(f"用时：{result.elapsed_seconds:.1f} 秒；缓存：{result.cache}")
                    else:
                        parser = build_batch_parser("pdf2md session batch")
                        parser.set_defaults(
                            profile=session.profile,
                            method=session.method,
                            lang=session.language,
                            timeout=args.timeout,
                            startup_timeout=args.startup_timeout,
                            load_model=False,
                        )
                        parsed = parser.parse_args(tokens[1:])
                        _assert_session_settings(parsed, session)
                        summary = execute_batch(parsed, shared_session=session)
                        _print_batch_summary(summary, bool(parsed.json))
                except SystemExit:
                    continue
                except Exception as exc:
                    print(f"错误：{exc}", file=sys.stderr)
    except KeyboardInterrupt:
        cancel_event.set()
        print("\n会话已取消，正在释放模型。", file=sys.stderr)
        return 130
    return 0


def _single_main(args: argparse.Namespace) -> int:
    source = Path(args.pdf).expanduser().resolve()
    output = Path(args.output).expanduser().resolve() if args.output else None
    options = _conversion_options(source, args, output=output)
    cancel_event = threading.Event()
    emit = _emit_callback(json_mode=bool(args.json))

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


def main(argv: list[str] | None = None) -> int:
    configure_streams()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0].casefold() == "batch":
        parser = build_batch_parser()
        args = parser.parse_args(raw_args[1:])
        try:
            summary = execute_batch(args)
        except KeyboardInterrupt:
            payload = {"ok": False, "error_code": "CANCELLED", "message": "批量转换已取消。"}
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
        _print_batch_summary(summary, bool(args.json))
        return 0 if summary["ok"] else 1

    if raw_args and raw_args[0].casefold() in {"preload", "session"}:
        parser = build_session_parser(f"pdf2md {raw_args[0].casefold()}")
        args = parser.parse_args(raw_args[1:])
        try:
            return run_interactive_session(args)
        except Exception as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1

    return _single_main(build_parser().parse_args(raw_args))


if __name__ == "__main__":
    raise SystemExit(main())
