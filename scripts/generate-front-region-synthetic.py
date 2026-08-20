#!/usr/bin/env python3
"""Generate a deterministic, CC0 front-matter classification corpus.

The generated PDFs are intentionally small and synthetic.  They exercise page-role
classification without importing documents whose redistribution or training rights
are unclear.  Every annotation is bound to the exact PDF bytes by SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import reportlab
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas


SCHEMA = "pdf2md.front-page-label.v1"
NAVIGATION_SCHEMA = "pdf2md.front-navigation-label.v1"
MANIFEST_SCHEMA = "pdf2md.synthetic-front-corpus.v2"
LEGACY_MANIFEST_SCHEMA = "pdf2md.synthetic-front-corpus.v1"
GENERATOR_VERSION = 2
LEGACY_GENERATOR_VERSION = 1
DEFAULT_DOCUMENTS = 8
DEFAULT_SEED = 20260820
DEFAULT_OUTPUT = Path("data/training/generated")
CC0_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
OWNED_PDF_RE = re.compile(r"synthetic-(?:en|zh)-(?:full|no_toc)-[0-9]{4}\.pdf\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GENERATOR_NAME = "generate-front-region-synthetic.py"
ANNOTATIONS_NAME = "annotations.jsonl"
NAVIGATION_ANNOTATIONS_NAME = "navigation-annotations.jsonl"
CORPUS_NAME = "corpus.json"
PROVENANCE_NAME = "provenance.json"

NAVIGATION_KINDS = ("contents", "list_of_figures", "list_of_tables")

LEGACY_MANIFEST_FIELDS = {
    "schema",
    "annotation_schema",
    "generator",
    "provenance",
    "page_roles",
    "annotations",
    "training_corpus",
    "documents",
}
CURRENT_MANIFEST_FIELDS = LEGACY_MANIFEST_FIELDS | {
    "navigation_annotation_schema",
    "navigation_kinds",
    "navigation_annotations",
}

PAGE_ROLES = (
    "cover",
    "legal",
    "revision_history",
    "preface",
    "abstract",
    "acknowledgements",
    "contents",
    "list_of_figures",
    "list_of_tables",
    "abbreviations",
    "nomenclature",
    "body_start",
    "other_front",
)

FULL_SEQUENCE = (
    "cover",
    "legal",
    "revision_history",
    "preface",
    "abstract",
    "acknowledgements",
    "contents",
    "contents",
    "list_of_figures",
    "list_of_tables",
    "abbreviations",
    "nomenclature",
    "other_front",
    "body_start",
)

NEGATIVE_SEQUENCE = ("cover", "legal", "abstract", "other_front", "body_start")


@dataclass(frozen=True)
class DocumentPlan:
    document_id: str
    language: str
    template: str
    sequence: tuple[str, ...]
    navigation_sequence: tuple[tuple[str, ...], ...]
    style: str
    seed: int


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _register_fonts() -> None:
    if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


def _font(language: str, *, bold: bool = False) -> str:
    if language == "zh-CN":
        return "STSong-Light"
    return "Helvetica-Bold" if bold else "Helvetica"


def _measure(text: str, font: str, size: float) -> float:
    return pdfmetrics.stringWidth(text, font, size)


def _wrap(text: str, font: str, size: float, width: float, language: str) -> list[str]:
    if not text:
        return [""]
    units = list(text) if language == "zh-CN" else text.split()
    separator = "" if language == "zh-CN" else " "
    lines: list[str] = []
    current = ""
    for unit in units:
        candidate = unit if not current else current + separator + unit
        if current and _measure(candidate, font, size) > width:
            lines.append(current)
            current = unit
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _paragraph(
    pdf: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    width: float,
    language: str,
    *,
    size: float = 10.5,
    leading: float = 15,
) -> float:
    font = _font(language)
    pdf.setFont(font, size)
    for line in _wrap(text, font, size, width, language):
        pdf.drawString(x, y, line)
        y -= leading
    return y


def _header_footer(
    pdf: canvas.Canvas,
    language: str,
    page_number: int,
    role: str,
    style: str,
) -> None:
    width, height = A4
    pdf.saveState()
    pdf.setStrokeColor(HexColor("#64748B"))
    pdf.setFillColor(HexColor("#475569"))
    pdf.setLineWidth(0.45)
    if style == "single":
        pdf.line(46, height - 38, width - 46, height - 38)
        pdf.setFont(_font(language), 7.5)
        label = "Synthetic Reference Manual" if language == "en" else "合成参考手册"
        pdf.drawString(48, height - 32, label)
        pdf.drawRightString(width - 48, 28, str(page_number))
    elif style == "double":
        pdf.line(42, 33, width - 42, 33)
        pdf.setFont(_font(language), 7.5)
        pdf.drawString(44, 22, role.replace("_", " "))
        pdf.drawRightString(width - 44, height - 29, f"{page_number:02d}")
    else:
        pdf.line(44, height - 34, width - 44, height - 34)
        pdf.line(44, 31, width - 44, 31)
        pdf.setFont(_font(language), 7.5)
        pdf.drawCentredString(width / 2, 20, f"— {page_number} —")
    pdf.restoreState()


def _title(pdf: canvas.Canvas, text: str, language: str, *, y: float = 752, size: float = 23) -> float:
    pdf.setFillColor(HexColor("#111827"))
    pdf.setFont(_font(language, bold=True), size)
    pdf.drawString(52, y, text)
    pdf.setStrokeColor(HexColor("#0F766E"))
    pdf.setLineWidth(1.2)
    pdf.line(52, y - 11, 176, y - 11)
    return y - 38


def _cover(pdf: canvas.Canvas, language: str, rng: random.Random) -> None:
    width, height = A4
    title = "Adaptive Sensor Systems" if language == "en" else "自适应传感系统"
    subtitle = "A Synthetic Technical Monograph" if language == "en" else "合成技术专著"
    edition = rng.choice(("First Edition", "Reference Edition", "Laboratory Edition"))
    if language == "zh-CN":
        edition = rng.choice(("第一版", "参考版", "实验室版"))
    pdf.setFillColor(HexColor("#0F172A"))
    pdf.rect(0, 0, width, height, fill=1, stroke=0)
    pdf.setFillColor(HexColor("#5EEAD4"))
    pdf.rect(58, height - 196, 8, 94, fill=1, stroke=0)
    pdf.setFont(_font(language, bold=True), 30)
    pdf.drawString(86, height - 126, title)
    pdf.setFont(_font(language), 14)
    pdf.drawString(87, height - 159, subtitle)
    pdf.setFillColor(HexColor("#CBD5E1"))
    pdf.setFont(_font(language), 11)
    pdf.drawString(87, height - 190, edition)
    author = "PDF2MD Synthetic Corpus" if language == "en" else "PDF2MD 合成语料"
    pdf.drawString(87, 88, author)


def _legal(pdf: canvas.Canvas, language: str) -> None:
    heading = "Copyright and License" if language == "en" else "版权与许可"
    body = (
        "This document is generated entirely by the PDF2MD project. No third-party "
        "text or artwork is included. To the extent possible under law, its authors "
        "waive all copyright and related rights under CC0 1.0."
        if language == "en"
        else "本文档完全由 PDF2MD 项目自动生成，不包含第三方文本或图稿。作者在法律允许的范围内，"
        "依据 CC0 1.0 放弃全部版权及相关权利。"
    )
    y = _title(pdf, heading, language)
    _paragraph(pdf, body, 52, y, 485, language, size=11, leading=18)


def _revision_history(pdf: canvas.Canvas, language: str) -> None:
    heading = "Revision History" if language == "en" else "修订记录"
    headers = ("Revision", "Date", "Description") if language == "en" else ("版本", "日期", "说明")
    rows = (
        (("A", "2026-01", "Initial synthetic release"), ("B", "2026-08", "Added layout variants"))
        if language == "en"
        else (("A", "2026-01", "首次合成发布"), ("B", "2026-08", "增加版式变体"))
    )
    y = _title(pdf, heading, language)
    xs = (52, 166, 278, 542)
    pdf.setFillColor(HexColor("#E2E8F0"))
    pdf.rect(xs[0], y - 26, xs[-1] - xs[0], 27, fill=1, stroke=0)
    pdf.setFillColor(HexColor("#0F172A"))
    pdf.setFont(_font(language, bold=True), 9)
    for index, value in enumerate(headers):
        pdf.drawString(xs[index] + 6, y - 17, value)
    y -= 26
    pdf.setFont(_font(language), 9)
    for row in rows:
        y -= 28
        pdf.setStrokeColor(HexColor("#CBD5E1"))
        pdf.line(xs[0], y, xs[-1], y)
        for index, value in enumerate(row):
            pdf.drawString(xs[index] + 6, y + 9, value)


def _prose_page(pdf: canvas.Canvas, role: str, language: str) -> None:
    titles_en = {
        "preface": "Preface",
        "abstract": "Abstract",
        "acknowledgements": "Acknowledgements",
        "other_front": "Dedication",
    }
    titles_zh = {
        "preface": "前言",
        "abstract": "摘要",
        "acknowledgements": "致谢",
        "other_front": "献词",
    }
    title = (titles_en if language == "en" else titles_zh)[role]
    if language == "en":
        body = {
            "preface": "This synthetic manual demonstrates realistic front matter for layout and page-role evaluation. Its examples vary spacing, columns, running heads, and page numbering while remaining compact and reproducible.",
            "abstract": "We describe a fictional sensing platform that combines optical timing, calibrated conversion, and low-power control. The document exists only to train and evaluate document-structure recognition.",
            "acknowledgements": "The project acknowledges its test authors, reviewers, and maintainers. Every sentence on this page is newly generated for the corpus.",
            "other_front": "For readers who build careful tools from small, verifiable parts.",
        }[role]
    else:
        body = {
            "preface": "本合成手册用于评估前置页的版面与页面角色识别。示例包含不同的间距、分栏、页眉和页码形式，同时保持体量小且可重复生成。",
            "abstract": "本文描述一个虚构的传感平台，结合光学计时、校准转换与低功耗控制。该文档仅用于训练和评估文档结构识别。",
            "acknowledgements": "项目感谢测试作者、审阅者与维护者。本页全部句子均为语料库全新生成。",
            "other_front": "谨献给以细小而可验证的组件构建可靠工具的读者。",
        }[role]
    y = _title(pdf, title, language)
    _paragraph(pdf, body, 52, y, 478, language, size=11.2, leading=19)


def _toc_entries(language: str, continuation: bool) -> list[tuple[str, str]]:
    if language == "en":
        first = [
            ("Preface", "iii"),
            ("Abstract", "v"),
            ("1  System Overview", "1"),
            ("1.1  Measurement chain", "3"),
            ("1.2  Timing architecture", "7"),
            ("2  Signal Conversion", "15"),
        ]
        second = [
            ("2.1 Calibration procedure", "18"),
            ("2.2 Error sources", "24"),
            ("3 Control Interface", "31"),
            ("3.1 Register map", "35"),
            ("Appendix A Test vectors", "A-1"),
            ("Index", "I-1"),
        ]
    else:
        first = [
            ("前言", "iii"),
            ("摘要", "v"),
            ("第 1 章 系统概述", "1"),
            ("1.1 测量链路", "3"),
            ("1.2 计时架构", "7"),
            ("第 2 章 信号转换", "15"),
        ]
        second = [
            ("2.1 校准流程", "18"),
            ("2.2 误差来源", "24"),
            ("第 3 章 控制接口", "31"),
            ("3.1 寄存器映射", "35"),
            ("附录 A 测试向量", "A-1"),
            ("索引", "I-1"),
        ]
    return second if continuation else first


def _draw_index_entries(
    pdf: canvas.Canvas,
    entries: Sequence[tuple[str, str]],
    language: str,
    *,
    y: float,
    style: str,
    continuation: bool,
) -> None:
    font = _font(language)
    pdf.setFont(font, 10.2)
    if continuation or style == "double":
        columns = (52, 302)
        column_width = 223
        split = (len(entries) + 1) // 2
        groups = (entries[:split], entries[split:])
    else:
        columns = (52,)
        column_width = 490
        groups = (entries,)
    for x, group in zip(columns, groups):
        row_y = y
        for row_index, (label, page) in enumerate(group):
            # Continuation pages deliberately use a single space before the page
            # number; other pages alternate dot leaders and Roman references.
            if continuation:
                line = f"{label} {page}"
                pdf.drawString(x, row_y, line)
            else:
                pdf.drawString(x, row_y, label)
                page_width = _measure(page, font, 10.2)
                pdf.drawRightString(x + column_width, row_y, page)
                leader_start = x + min(_measure(label, font, 10.2) + 9, column_width - page_width - 24)
                leader_end = x + column_width - page_width - 8
                dot_width = max(_measure(".", font, 10.2), 1.0)
                count = max(2, int((leader_end - leader_start) / (dot_width * 2.0)))
                pdf.setFillColor(HexColor("#64748B"))
                pdf.drawString(leader_start, row_y, ". " * count)
                pdf.setFillColor(HexColor("#111827"))
            row_y -= 29


def _contents(pdf: canvas.Canvas, language: str, continuation: bool, style: str) -> None:
    if language == "en":
        heading = "Contents (continued)" if continuation else "Contents"
    else:
        heading = "目录（续）" if continuation else "目录"
    y = _title(pdf, heading, language)
    _draw_index_entries(
        pdf,
        _toc_entries(language, continuation),
        language,
        y=y,
        style=style,
        continuation=continuation,
    )


def _list_page(pdf: canvas.Canvas, role: str, language: str) -> None:
    if role == "list_of_figures":
        heading = "List of Figures" if language == "en" else "插图目录"
        names = (
            ["Figure 1-1 Measurement path", "Figure 2-1 Converter timing", "Figure 3-1 Control states"]
            if language == "en"
            else ["图 1-1 测量通路", "图 2-1 转换时序", "图 3-1 控制状态"]
        )
    else:
        heading = "List of Tables" if language == "en" else "表格目录"
        names = (
            ["Table 1-1 System limits", "Table 2-1 Calibration values", "Table 3-1 Register fields"]
            if language == "en"
            else ["表 1-1 系统限制", "表 2-1 校准值", "表 3-1 寄存器字段"]
        )
    y = _title(pdf, heading, language)
    _draw_index_entries(pdf, list(zip(names, ("2", "19", "34"))), language, y=y, style="single", continuation=False)


def _glossary_page(pdf: canvas.Canvas, role: str, language: str) -> None:
    if role == "abbreviations":
        heading = "Abbreviations" if language == "en" else "缩略语"
        rows = (
            [("ADC", "Analog-to-digital converter"), ("DSP", "Digital signal processing"), ("PLL", "Phase-locked loop")]
            if language == "en"
            else [("ADC", "模数转换器"), ("DSP", "数字信号处理"), ("PLL", "锁相环")]
        )
    else:
        heading = "Nomenclature" if language == "en" else "符号表"
        rows = (
            [("f_s", "sampling frequency"), ("T_c", "conversion period"), ("V_ref", "reference voltage")]
            if language == "en"
            else [("f_s", "采样频率"), ("T_c", "转换周期"), ("V_ref", "参考电压")]
        )
    y = _title(pdf, heading, language)
    pdf.setFont(_font(language), 10.5)
    for row_index, (term, definition) in enumerate(rows):
        x = 52 if row_index % 2 == 0 else 302
        row_y = y - (row_index // 2) * 56
        pdf.setFont("Helvetica-Bold", 10.5)
        pdf.drawString(x, row_y, term)
        pdf.setFont(_font(language), 9.7)
        pdf.drawString(x + 64, row_y, definition)


def _body_start(pdf: canvas.Canvas, language: str) -> None:
    heading = "1 System Overview" if language == "en" else "第 1 章 系统概述"
    y = _title(pdf, heading, language, size=25)
    subheading = "1.1 Measurement chain" if language == "en" else "1.1 测量链路"
    pdf.setFont(_font(language, bold=True), 15)
    pdf.drawString(52, y, subheading)
    y -= 27
    body = (
        "The sensor path begins at a balanced optical input and ends at a calibrated digital sample. Figure 1-1 summarizes the fictional measurement path used by this generated document."
        if language == "en"
        else "传感通路始于平衡光学输入，止于校准后的数字采样。图 1-1 概括了本文档虚构的测量通路。"
    )
    _paragraph(pdf, body, 52, y, 484, language, size=10.8, leading=18)


def _negative_note(pdf: canvas.Canvas, language: str) -> None:
    heading = "Reader Note" if language == "en" else "读者说明"
    body = (
        "This short note proceeds directly to the first chapter. Navigation is provided by descriptive headings in the body, not by a separate index page."
        if language == "en"
        else "本短篇说明直接进入第一章。导航依靠正文中的描述性标题，不另设独立索引页。"
    )
    y = _title(pdf, heading, language)
    _paragraph(pdf, body, 52, y, 480, language, size=11, leading=18)


def _render_page(
    pdf: canvas.Canvas,
    role: str,
    language: str,
    page_number: int,
    occurrence: int,
    plan: DocumentPlan,
    rng: random.Random,
) -> None:
    if role == "cover":
        _cover(pdf, language, rng)
    elif role == "legal":
        _legal(pdf, language)
    elif role == "revision_history":
        _revision_history(pdf, language)
    elif role in {"preface", "abstract", "acknowledgements"}:
        _prose_page(pdf, role, language)
    elif role == "contents":
        _contents(pdf, language, occurrence > 0, plan.style)
    elif role in {"list_of_figures", "list_of_tables"}:
        _list_page(pdf, role, language)
    elif role in {"abbreviations", "nomenclature"}:
        _glossary_page(pdf, role, language)
    elif role == "other_front":
        if plan.template == "no_toc":
            _negative_note(pdf, language)
        else:
            _prose_page(pdf, role, language)
    elif role == "body_start":
        _body_start(pdf, language)
    else:  # pragma: no cover - guarded by the fixed plans
        raise ValueError(f"Unsupported page role: {role}")
    if role != "cover":
        _header_footer(pdf, language, page_number, role, plan.style)
    pdf.showPage()


def _plans(documents: int, seed: int) -> list[DocumentPlan]:
    if documents < 1:
        raise ValueError("--documents must be at least 1")
    plans: list[DocumentPlan] = []
    styles = ("single", "double", "compact")
    for index in range(documents):
        slot = index % 4
        language = "en" if slot in (0, 2) else "zh-CN"
        template = "full" if slot in (0, 1) else "no_toc"
        sequence = FULL_SEQUENCE if template == "full" else NEGATIVE_SEQUENCE
        short_language = "zh" if language == "zh-CN" else "en"
        document_id = f"synthetic-{short_language}-{template}-{index + 1:04d}"
        plans.append(
            DocumentPlan(
                document_id=document_id,
                language=language,
                template=template,
                sequence=sequence,
                # Navigation presence is an independent page plan rather than
                # being inferred while labels are serialized.  A future mixed
                # page can therefore list navigation kinds even when its primary
                # role is, for example, ``abstract``.
                navigation_sequence=tuple(
                    (role,) if role in NAVIGATION_KINDS else ()
                    for role in sequence
                ),
                style=styles[index % len(styles)],
                seed=seed + index * 104729,
            )
        )
    return plans


def _write_pdf(path: Path, plan: DocumentPlan) -> None:
    rng = random.Random(plan.seed)
    pdf = canvas.Canvas(str(path), pagesize=A4, pageCompression=1, invariant=1)
    pdf.setTitle(f"PDF2MD synthetic corpus: {plan.document_id}")
    pdf.setAuthor("PDF2MD project")
    pdf.setCreator(f"PDF2MD synthetic generator v{GENERATOR_VERSION}")
    occurrences: dict[str, int] = {}
    for page_number, role in enumerate(plan.sequence, start=1):
        occurrence = occurrences.get(role, 0)
        _render_page(pdf, role, plan.language, page_number, occurrence, plan, rng)
        occurrences[role] = occurrence + 1
    pdf.save()


def _annotation(plan: DocumentPlan, digest: str, page_number: int, role: str) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "document_id": plan.document_id,
        "source_sha256": digest,
        "page": page_number,
        "kind": role,
        "status": "verified",
        "reviewer": "project-synthetic-generator-v1",
    }


def _navigation_annotation(
    plan: DocumentPlan,
    digest: str,
    page_number: int,
    kind: str,
    presence: str,
) -> dict[str, object]:
    return {
        "schema": NAVIGATION_SCHEMA,
        "document_id": plan.document_id,
        "source_sha256": digest,
        "page": page_number,
        "kind": kind,
        "presence": presence,
        "status": "verified",
        "reviewer": "project-synthetic-generator-v2",
    }


def _write_jsonl(path: Path, rows: Iterable[dict[str, object]]) -> None:
    text = "".join(_stable_json(row) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8", newline="\n")


class UnsafeOutputError(ValueError):
    """Raised when generation would overwrite an artifact we cannot prove we own."""


@dataclass(frozen=True)
class _OwnershipState:
    manifest: dict[str, object] | None
    provenance_sha256: str | None
    artifacts: tuple[tuple[str, str], ...]
    sizes: tuple[tuple[str, int], ...]
    existing: tuple[str, ...]


def _require_sha256(value: object, context: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise UnsafeOutputError(f"Refusing to use invalid {context} SHA-256 in {PROVENANCE_NAME}")
    return value


def _manifest_artifacts(
    manifest: object,
) -> tuple[dict[str, str], dict[str, int]]:
    """Validate a prior manifest and return its exact owned artifact set."""
    if not isinstance(manifest, dict):
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: manifest is not an object"
        )
    schema = manifest.get("schema")
    if schema == MANIFEST_SCHEMA:
        legacy = False
        expected_fields = CURRENT_MANIFEST_FIELDS
        expected_generator_version = GENERATOR_VERSION
    elif schema == LEGACY_MANIFEST_SCHEMA:
        legacy = True
        expected_fields = LEGACY_MANIFEST_FIELDS
        expected_generator_version = LEGACY_GENERATOR_VERSION
    else:
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: unsupported manifest schema"
        )
    if set(manifest) != expected_fields:
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: manifest fields are invalid"
        )

    if (
        manifest.get("annotation_schema") != SCHEMA
        or manifest.get("page_roles") != list(PAGE_ROLES)
    ):
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: primary annotation metadata is invalid"
        )
    provenance = manifest.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("source") != "project-generated"
        or provenance.get("contains_third_party_content") is not False
        or provenance.get("license") != "CC0-1.0"
        or provenance.get("license_url") != CC0_URL
    ):
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: provenance metadata is invalid"
        )

    generator = manifest.get("generator")
    if not isinstance(generator, dict) or generator.get("name") != GENERATOR_NAME:
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: generator identity is not trusted"
        )
    version = generator.get("version")
    document_count = generator.get("documents")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != expected_generator_version
        or isinstance(document_count, bool)
        or not isinstance(document_count, int)
        or document_count < 1
        or isinstance(generator.get("seed"), bool)
        or not isinstance(generator.get("seed"), int)
    ):
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: generator metadata is invalid"
        )

    source = generator.get("source")
    if not isinstance(source, dict) or source.get("path") != GENERATOR_NAME:
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: generator source metadata is invalid"
        )
    _require_sha256(source.get("sha256"), "generator source")

    if not legacy and (
        manifest.get("navigation_annotation_schema") != NAVIGATION_SCHEMA
        or manifest.get("navigation_kinds") != list(NAVIGATION_KINDS)
    ):
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: annotation metadata is invalid"
        )

    artifacts: dict[str, str] = {}
    sizes: dict[str, int] = {}
    documents = manifest.get("documents")
    if not isinstance(documents, list) or len(documents) != document_count:
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: document inventory is invalid"
        )
    for item in documents:
        if not isinstance(item, dict):
            raise UnsafeOutputError(
                f"Refusing to overwrite {PROVENANCE_NAME}: document entry is invalid"
            )
        name = item.get("pdf_path")
        document_id = item.get("document_id")
        size = item.get("bytes")
        page_count = item.get("page_count")
        page_labels = item.get("page_labels")
        page_navigation_labels = item.get("page_navigation_labels")
        expected_document_fields = {
            "document_id",
            "pdf_path",
            "pdf_sha256",
            "bytes",
            "page_count",
            "language",
            "template",
            "layout_style",
            "has_contents",
            "page_labels",
        }
        if not legacy:
            expected_document_fields.add("page_navigation_labels")
        if (
            set(item) != expected_document_fields
            or not isinstance(name, str)
            or not OWNED_PDF_RE.fullmatch(name)
            or document_id != name[:-4]
            or name in artifacts
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or isinstance(page_count, bool)
            or not isinstance(page_count, int)
            or page_count < 1
            or not isinstance(page_labels, list)
            or len(page_labels) != page_count
            or any(role not in PAGE_ROLES for role in page_labels)
            or (
                not legacy
                and (
                    not isinstance(page_navigation_labels, list)
                    or len(page_navigation_labels) != page_count
                    or any(
                        not isinstance(kinds, list)
                        or len(kinds) != len(set(kinds))
                        or any(kind not in NAVIGATION_KINDS for kind in kinds)
                        for kinds in page_navigation_labels
                    )
                )
            )
        ):
            raise UnsafeOutputError(
                f"Refusing to overwrite {PROVENANCE_NAME}: PDF inventory is invalid"
            )
        artifacts[name] = _require_sha256(item.get("pdf_sha256"), f"{name}")
        sizes[name] = size

    metadata_specs = [("annotations", ANNOTATIONS_NAME)]
    if not legacy:
        metadata_specs.append(("navigation_annotations", NAVIGATION_ANNOTATIONS_NAME))
    metadata_specs.append(("training_corpus", CORPUS_NAME))
    for field, expected_name in metadata_specs:
        item = manifest.get(field)
        if not isinstance(item, dict) or item.get("path") != expected_name:
            raise UnsafeOutputError(
                f"Refusing to overwrite {PROVENANCE_NAME}: {field} inventory is invalid"
            )
        artifacts[expected_name] = _require_sha256(item.get("sha256"), expected_name)

    annotation_inventory = manifest["annotations"]
    if (
        set(annotation_inventory) != {"path", "sha256", "records"}
        or annotation_inventory.get("records") != sum(
            item["page_count"] for item in documents
        )
    ):
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: annotations inventory is invalid"
        )
    corpus_inventory = manifest["training_corpus"]
    if (
        set(corpus_inventory) != {"path", "sha256", "license"}
        or corpus_inventory.get("license") != "CC0-1.0"
    ):
        raise UnsafeOutputError(
            f"Refusing to overwrite {PROVENANCE_NAME}: training corpus inventory is invalid"
        )
    if not legacy:
        navigation_inventory = manifest["navigation_annotations"]
        if (
            set(navigation_inventory) != {"path", "sha256", "records"}
            or navigation_inventory.get("records")
            != sum(item["page_count"] for item in documents) * len(NAVIGATION_KINDS)
        ):
            raise UnsafeOutputError(
                f"Refusing to overwrite {PROVENANCE_NAME}: navigation inventory is invalid"
            )
    return artifacts, sizes


def _is_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _inspect_output(output: Path, planned_names: Iterable[str]) -> _OwnershipState:
    """Verify ownership and reject every unowned path that would be replaced."""
    provenance_path = output / PROVENANCE_NAME
    manifest: dict[str, object] | None = None
    provenance_sha256: str | None = None
    artifacts: dict[str, str] = {}
    sizes: dict[str, int] = {}
    existing: list[str] = []

    if _is_present(provenance_path):
        if provenance_path.is_symlink() or not provenance_path.is_file():
            raise UnsafeOutputError(
                f"Refusing to overwrite unsafe path: {provenance_path}"
            )
        try:
            candidate = json.loads(provenance_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise UnsafeOutputError(
                f"Refusing to overwrite unreadable {PROVENANCE_NAME}"
            ) from error
        artifacts, sizes = _manifest_artifacts(candidate)
        manifest = candidate
        provenance_sha256 = _sha256(provenance_path)
        existing.append(PROVENANCE_NAME)
        legacy_migration = candidate.get("schema") == LEGACY_MANIFEST_SCHEMA

        for name, expected_sha256 in artifacts.items():
            path = output / name
            if not _is_present(path):
                if legacy_migration:
                    raise UnsafeOutputError(
                        f"Refusing legacy migration with missing owned artifact: {path}"
                    )
                continue
            if path.is_symlink() or not path.is_file():
                raise UnsafeOutputError(f"Refusing to overwrite unsafe path: {path}")
            actual_sha256 = _sha256(path)
            if actual_sha256 != expected_sha256:
                raise UnsafeOutputError(
                    f"Refusing to overwrite modified artifact {name}: "
                    f"expected {expected_sha256}, found {actual_sha256}"
                )
            expected_size = sizes.get(name)
            if expected_size is not None and path.stat().st_size != expected_size:
                raise UnsafeOutputError(
                    f"Refusing to overwrite modified artifact {name}: size mismatch"
                )
            existing.append(name)

    owned = set(artifacts)
    if manifest is not None:
        owned.add(PROVENANCE_NAME)
    for name in planned_names:
        target = output / name
        if _is_present(target) and name not in owned:
            raise UnsafeOutputError(
                f"Refusing to overwrite unowned artifact: {target}"
            )

    return _OwnershipState(
        manifest=manifest,
        provenance_sha256=provenance_sha256,
        artifacts=tuple(sorted(artifacts.items())),
        sizes=tuple(sorted(sizes.items())),
        existing=tuple(sorted(existing)),
    )


def _generator_metadata(documents: int, seed: int) -> dict[str, object]:
    return {
        "name": GENERATOR_NAME,
        "version": GENERATOR_VERSION,
        "seed": seed,
        "documents": documents,
        "source": {
            "path": GENERATOR_NAME,
            "sha256": _sha256(Path(__file__).resolve()),
        },
        "runtime": {
            "python": {
                "implementation": platform.python_implementation(),
                "version": platform.python_version(),
            },
            "reportlab": {
                "version": str(reportlab.Version),
            },
            "fonts": [
                {
                    "name": "Helvetica",
                    "kind": "ReportLab built-in Type 1",
                    "language": "en",
                },
                {
                    "name": "Helvetica-Bold",
                    "kind": "ReportLab built-in Type 1",
                    "language": "en",
                },
                {
                    "name": "STSong-Light",
                    "kind": "ReportLab Unicode CID",
                    "encoding": "UniGB-UCS2-H",
                    "language": "zh-CN",
                },
            ],
        },
    }


def _build_staged_corpus(
    output: Path,
    plans: Sequence[DocumentPlan],
    documents: int,
    seed: int,
) -> dict[str, object]:
    """Build a complete corpus in an isolated staging directory."""
    annotations: list[dict[str, object]] = []
    navigation_annotations: list[dict[str, object]] = []
    manifest_documents: list[dict[str, object]] = []
    for plan in plans:
        if len(plan.navigation_sequence) != len(plan.sequence):
            raise ValueError(f"{plan.document_id}: navigation plan length does not match pages")
        pdf_path = output / f"{plan.document_id}.pdf"
        _write_pdf(pdf_path, plan)
        digest = _sha256(pdf_path)
        for page_number, (role, planned_navigation) in enumerate(
            zip(plan.sequence, plan.navigation_sequence),
            start=1,
        ):
            if (
                len(planned_navigation) != len(set(planned_navigation))
                or any(kind not in NAVIGATION_KINDS for kind in planned_navigation)
            ):
                raise ValueError(
                    f"{plan.document_id} page {page_number}: invalid navigation plan"
                )
            annotations.append(_annotation(plan, digest, page_number, role))
            present = set(planned_navigation)
            for kind in NAVIGATION_KINDS:
                navigation_annotations.append(
                    _navigation_annotation(
                        plan,
                        digest,
                        page_number,
                        kind,
                        "present" if kind in present else "absent",
                    )
                )
        manifest_documents.append(
            {
                "document_id": plan.document_id,
                "pdf_path": pdf_path.name,
                "pdf_sha256": digest,
                "bytes": pdf_path.stat().st_size,
                "page_count": len(plan.sequence),
                "language": plan.language,
                "template": plan.template,
                "layout_style": plan.style,
                "has_contents": any(
                    "contents" in kinds for kinds in plan.navigation_sequence
                ),
                "page_labels": list(plan.sequence),
                "page_navigation_labels": [
                    list(kinds) for kinds in plan.navigation_sequence
                ],
            }
        )

    annotations_path = output / ANNOTATIONS_NAME
    _write_jsonl(annotations_path, annotations)
    navigation_annotations_path = output / NAVIGATION_ANNOTATIONS_NAME
    _write_jsonl(navigation_annotations_path, navigation_annotations)
    corpus_path = output / CORPUS_NAME
    corpus_documents = [
        {
            "id": item["document_id"],
            "title": f"PDF2MD synthetic front matter: {item['document_id']}",
            "language": item["language"],
            "document_type": "synthetic-front-matter",
            "field": "document-ai",
            "source_page": None,
            "url": None,
            "license_class": "cc0-1.0",
            "redistributable": True,
            "training_eligible": True,
            "suite": "core",
            "expected_front_regions": list(dict.fromkeys(item["page_labels"])),
            "expected_sha256": item["pdf_sha256"],
            "expected_size": item["bytes"],
            "local_path": item["pdf_path"],
        }
        for item in manifest_documents
    ]
    corpus = {
        "schema_version": 1,
        "front_region_schema": "pdf2md.front-regions.v1",
        "description": "Project-generated CC0 synthetic front-matter training corpus.",
        "documents": corpus_documents,
    }
    corpus_path.write_text(_stable_json(corpus) + "\n", encoding="utf-8", newline="\n")
    manifest: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "annotation_schema": SCHEMA,
        "navigation_annotation_schema": NAVIGATION_SCHEMA,
        "navigation_kinds": list(NAVIGATION_KINDS),
        "generator": _generator_metadata(documents, seed),
        "provenance": {
            "source": "project-generated",
            "contains_third_party_content": False,
            "license": "CC0-1.0",
            "license_url": CC0_URL,
        },
        "page_roles": list(PAGE_ROLES),
        "annotations": {
            "path": annotations_path.name,
            "sha256": _sha256(annotations_path),
            "records": len(annotations),
        },
        "navigation_annotations": {
            "path": navigation_annotations_path.name,
            "sha256": _sha256(navigation_annotations_path),
            "records": len(navigation_annotations),
        },
        "training_corpus": {
            "path": corpus_path.name,
            "sha256": _sha256(corpus_path),
            "license": "CC0-1.0",
        },
        "documents": manifest_documents,
    }
    (output / PROVENANCE_NAME).write_text(
        _stable_json(manifest) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def _planned_names(plans: Sequence[DocumentPlan]) -> tuple[str, ...]:
    pdfs = sorted(f"{plan.document_id}.pdf" for plan in plans)
    return tuple(
        [
            *pdfs,
            ANNOTATIONS_NAME,
            NAVIGATION_ANNOTATIONS_NAME,
            CORPUS_NAME,
            PROVENANCE_NAME,
        ]
    )


def _same_ownership(left: _OwnershipState, right: _OwnershipState) -> bool:
    return (
        left.provenance_sha256 == right.provenance_sha256
        and left.artifacts == right.artifacts
        and left.sizes == right.sizes
        and left.existing == right.existing
    )


def _atomic_commit(
    stage: Path,
    output: Path,
    previous: _OwnershipState,
    new_names: Sequence[str],
) -> None:
    """Replace the owned artifact set, rolling back ordinary commit failures."""
    for name in new_names:
        staged = stage / name
        if staged.is_symlink() or not staged.is_file():
            raise RuntimeError(f"Staged artifact is missing or unsafe: {staged}")

    old_owned = {name for name, _digest in previous.artifacts}
    if previous.manifest is not None:
        old_owned.add(PROVENANCE_NAME)
    prior_existing = set(previous.existing)
    new_name_set = set(new_names)
    install_order = [
        *sorted(name for name in new_names if name.endswith(".pdf")),
        ANNOTATIONS_NAME,
        NAVIGATION_ANNOTATIONS_NAME,
        CORPUS_NAME,
        PROVENANCE_NAME,
    ]

    with tempfile.TemporaryDirectory(
        prefix=".pdf2md-synthetic-backup-",
        dir=output.parent,
    ) as temporary:
        backup = Path(temporary)
        for name in sorted(prior_existing):
            shutil.copy2(output / name, backup / name)

        installed: list[str] = []
        try:
            for name in install_order[:-1]:
                os.replace(stage / name, output / name)
                installed.append(name)

            for name in sorted(old_owned - new_name_set):
                stale = output / name
                if _is_present(stale):
                    stale.unlink()

            os.replace(stage / PROVENANCE_NAME, output / PROVENANCE_NAME)
            installed.append(PROVENANCE_NAME)
        except BaseException as error:
            rollback_errors: list[str] = []
            for name in reversed(installed):
                if name in prior_existing:
                    continue
                target = output / name
                try:
                    if _is_present(target):
                        target.unlink()
                except OSError as rollback_error:
                    rollback_errors.append(f"remove {name}: {rollback_error}")
            for name in sorted(prior_existing):
                try:
                    os.replace(backup / name, output / name)
                except OSError as rollback_error:
                    rollback_errors.append(f"restore {name}: {rollback_error}")
            if rollback_errors:
                raise RuntimeError(
                    "Synthetic corpus commit failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from error
            raise


def generate(output: Path, documents: int, seed: int) -> dict[str, object]:
    """Generate the corpus and return its stable provenance manifest."""
    plans = _plans(documents, seed)
    _register_fonts()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    new_names = _planned_names(plans)

    # Check once before expensive rendering, then again immediately before the
    # commit so concurrent or accidental edits cannot be mistaken for ours.
    previous = _inspect_output(output, new_names)
    with tempfile.TemporaryDirectory(
        prefix=".pdf2md-synthetic-stage-",
        dir=output.parent,
    ) as temporary:
        stage = Path(temporary)
        manifest = _build_staged_corpus(stage, plans, documents, seed)
        current = _inspect_output(output, new_names)
        if not _same_ownership(previous, current):
            raise UnsafeOutputError(
                "Refusing to commit because output ownership changed during generation"
            )
        _atomic_commit(stage, output, current, new_names)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=DEFAULT_DOCUMENTS, help="number of PDFs to generate")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="deterministic generation seed")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="output directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = generate(args.output, args.documents, args.seed)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(
        f"Generated {len(manifest['documents'])} PDFs and "
        f"{manifest['annotations']['records']} primary page labels plus "
        f"{manifest['navigation_annotations']['records']} navigation presence labels "
        f"in {args.output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
