# MinerU Agent PDF 阅读工具完整方案

## 1. 结论

推荐把现有 `MinerU-Local.exe` 升级为同时面向人和 AI Agent 的两层工具：

- 保留当前无参数启动的图形界面，供人工使用。
- 增加稳定的 Agent CLI 子命令、JSON 输出、同级缓存、文档探测、页码定位和按页 Markdown。
- 创建一个 `mineru-read-pdf` Skill，要求 Agent 遇到论文、datasheet、规格书、标准、报告、手册等本地 PDF 时先调用工具，再阅读生成的 Markdown。
- 在系统级提示词中只放一条强制触发规则；具体决策流程放在 Skill 中，避免长期占用上下文。

最重要的策略不是“直接读 PDF”和“整本转换”二选一，而是：

> 先用不进入模型上下文的本地程序快速探测和搜索 PDF，确定相关页码；再让 MinerU 只转换最少的相关页面，Agent 最后读取带原始页码的 Markdown。只有短文档或用户明确要求全文时才整本转换。

这同时兼顾 Token、时间、版面还原质量和可追溯性，其中 Token 优先级最高。

## 2. 目标与原则

### 2.1 目标

1. Agent 遇到需要理解内容的 PDF 时自动触发 Skill。
2. Agent 不直接把整份 PDF、整份 Markdown 或大量图片放入上下文。
3. 支持文本型 PDF、扫描 PDF、论文、datasheet、复杂表格和公式。
4. 支持按页、连续页段和非连续页集转换。
5. 所有派生内容保存在源 PDF 同级目录，不污染 MinerU 安装目录。
6. 转换结果可缓存、可复用、可定位回原始 PDF 页码。
7. CLI 输出适合程序解析，执行失败时有稳定退出码和错误信息。

### 2.2 核心原则

- **先定位，后转换**：长 PDF 不先做全文高精度转换。
- **先结构，后正文**：先读书签、目录、标题、页数和文本密度。
- **最小充分页面集**：只转换回答当前问题所需页面，并补充必要上下文页。
- **一次转换多个目标页**：先汇总候选页，再一次性提交，避免重复加载模型。
- **Markdown 是阅读接口**：Agent 默认阅读 Markdown，不直接读取 MinerU 中间 JSON 或图片。
- **原始页码必须保留**：每段 Markdown 都应能追溯到 PDF 的物理页码。
- **缓存默认开启**：源文件内容与参数不变时直接复用。
- **机器输出与日志分离**：JSON 写 stdout，进度和诊断写 stderr。

## 3. 现状与需要补齐的能力

现有 `MinerU-Local.exe` 已具备：

- GUI 和 CLI 双模式。
- `hybrid-engine`、`pipeline`、`vlm-engine` 后端。
- 单页和连续页段转换，用户页码从 1 开始。
- Markdown 复制、公式/表格/图片分析开关。
- 本地 Conda、CUDA、模型和缓存路径。

面向 Agent 还缺少：

- PDF 快速探测和全文轻量搜索。
- 非连续页码，例如 `1-3,18,24-30`。
- JSON 机器输出和稳定退出码。
- 默认输出到 PDF 同级目录。
- 文档指纹、任务缓存、并发锁和缓存失效机制。
- 原始页码到 Markdown 的映射。
- 按页 Markdown、目录索引和 Token 估算。
- 长文档自动选择页面的编排命令。
- Skill、系统提示词和可验证的触发策略。

## 4. 推荐架构

```text
用户任务
   │
   ▼
系统提示词：遇到需要阅读的 PDF，必须使用 mineru-read-pdf
   │
   ▼
mineru-read-pdf Skill
   │
   ├── inspect：页数、书签、目录候选、文本密度、扫描件判断
   ├── search：本地文本索引搜索，返回候选原始页码和短摘要
   ├── prepare：按问题自动制定最小页面集并调用转换
   └── convert：显式转换指定页码
           │
           ▼
MinerU-Local.exe / MinerU Agent CLI
           │
           ├── 快速层：PyMuPDF 或 pypdf，不加载 GPU 模型
           ├── 高质量层：MinerU，只处理选定页面
           └── 后处理层：发布一个主 Markdown、归集图片，隐藏内部缓存
           │
           ▼
<源文件名>.mineru/
           │
           ├── <源文件名>.md
           ├── images/*
           └── raw/*
```

`inspect` 和 `search` 应尽量在几秒内完成且不加载 MinerU 模型。只有页面范围确定后才进入 GPU 转换阶段。

## 5. “直接读 PDF”还是“转换后读”的决策

### 5.1 推荐决策

| 情况 | 推荐动作 | 原因 |
|---|---|---|
| 只需元数据、页数、书签或关键词位置 | 调用 `inspect/search`，不让模型直接读 PDF | 几乎不消耗模型 Token，速度最快 |
| 短文本 PDF，约 25 页以内且估算不超过 30k Token | 整本转换为按页 Markdown 后读 | 转换成本可控，后续检索稳定 |
| 中长文本 PDF | 先搜索/目录定位，再转换相关页段 | 避免把无关章节放入上下文 |
| 扫描 PDF | 先抽样判断，再 OCR 目录页或候选页 | 原生文本搜索不可用，仍避免整本 OCR |
| 表格、公式、复杂多栏版面 | 对候选页使用 MinerU | 原生文本抽取容易打乱结构 |
| 需要核对图片、示意图或转换结果可疑 | 最后直接查看 1～3 个原始 PDF 页面 | 视觉阅读只作为验证和兜底 |
| 用户明确要求全文转换、全文归档或全书总结 | 整本转换，但仍分块阅读 | 用户目标本身需要完整覆盖 |

### 5.2 为什么不默认让 AI 直接阅读整个 PDF

- Agent 的 PDF 视觉读取常会把页面渲染、OCR 或长文本直接带入上下文，Token 成本不可控。
- 对超长文档，模型并不需要绝大多数页面。
- 直接阅读不容易形成可复用的本地缓存。
- 关键词定位、跨页搜索和二次任务复用较弱。
- 表格、公式、多栏排版的稳定性取决于具体 PDF 阅读器。

允许 Agent 直接查看少量原始页面，但用途限定为：验证图片、核对公式、确认 OCR 错误或 MinerU 失败兜底。

## 6. 分层处理流程

### 6.1 第 0 层：确认任务意图

先把用户问题转成一组检索目标，例如器件型号、寄存器、参数名称、论文方法名、章节标题或结论主题。用户只是要求复制、移动或删除 PDF 时，不触发解析。

### 6.2 第 1 层：快速探测 `inspect`

读取但不送入模型上下文的内容：

- 文件绝对路径、大小、修改时间和 SHA-256。
- PDF 页数、元数据、书签和内置目录。
- 每页可提取字符数、文本覆盖率和图片比例。
- 前几页、末页以及均匀抽样页的文本密度。
- 是否属于原生文本、混合型或扫描型 PDF。
- 估算全文字符数和保守 Token 数。
- 目录候选页和页码偏移候选。

探测结果以短 JSON 返回，禁止输出整页正文。

### 6.3 第 2 层：快速定位 `search`

对文本型 PDF 建立页级本地索引，搜索用户问题中的关键词和同义词。只返回：

- 原始 PDF 页码。
- 命中分数。
- 每个命中不超过约 240 字符的片段。
- 可能的章节标题。

默认返回前 8 个命中，Agent 汇总后合并相邻页，并为每段增加前后 1 页上下文。

优先使用 PDF 书签和真实目录；没有目录时再使用全文页级搜索。对扫描件，可先 OCR 前 8～12 页寻找目录，必要时按 4 页一组逐步扩展，但默认不超过前 20 页。

### 6.4 第 3 层：制定最小页面集

页面选择规则：

1. 将搜索命中、目标章节范围和用户明确指定页合并。
2. 对段落解释增加前后 1 页；对跨页表格增加前后 2 页。
3. 合并重叠或相邻范围。
4. 同一任务先形成完整页集，再发起一次转换。
5. 如果候选页超过全文的 60%，且全文 Token 预算允许，改为全文转换。
6. 如果候选页仍超过单轮阅读预算，先转换目录和最高分页段，再迭代扩展。

### 6.5 第 4 层：MinerU 转换

推荐三个配置档：

- `fast-text`：仅用于原生文本探测和定位，不启动 MinerU。
- `balanced`：默认使用 `hybrid-engine + medium`，关闭图片分析，保留公式和表格。
- `accurate`：对扫描件、复杂表格、公式或图表使用 `hybrid-engine + high`，必要时使用 OCR。

非连续页面不要逐页启动 MinerU。应先用 pypdf 将所选原始页面合并成一个临时子集 PDF，一次转换完成，并在 manifest 中保存“子集页码 → 原始页码”的映射。连续页段仍可直接使用 MinerU 的 `-s/-e`。

### 6.6 第 5 层：Agent 阅读 Markdown

Agent 按以下顺序读取：

1. 只打开 CLI 返回的顶层 `<源文件名>.md`。
2. 使用 `rg` 在主 Markdown 中定位标题和关键词，再分块读取相关内容。
3. 只有问题依赖图表、示意图或视觉核对时才打开 `images/` 中的对应图片。
4. 正常阅读时不得枚举或载入 `raw/`；该目录仅供缓存、排错和异常纠错审计。
5. 回答时使用主 Markdown 中保留的原始 PDF 页码。

不要默认一次读取完整 Markdown；即使已全文转换，也要先搜索再分块读取。

## 7. Token 优化方案

### 7.1 Token 预算分层

建议每个 PDF 任务使用独立的“检索阅读预算”，默认 12k Token，可由 Agent 或用户调整：

- 探测结果：不超过 1k Token。
- 目录和标题索引：不超过 2k Token。
- 搜索命中片段：不超过 2k Token。
- 首轮正文阅读：约 6k～8k Token。
- 保留约 2k Token 用于补页、核对和组织答案。

预算不是限制转换文件大小，而是限制 Agent 实际载入上下文的内容量。

### 7.2 保守 Token 估算

不调用模型 tokenizer 时，可使用保守估算：

```text
estimated_tokens = latin_characters / 3.5 + cjk_characters * 1.2
```

再增加约 15% 作为 Markdown、表格和公式开销。估算值只用于选择全文或局部策略，不作为计费精确值。

### 7.3 降低上下文占用的具体措施

- Skill 主文件保持简短，CLI 细节放入按需读取的 `references/cli.md`。
- `inspect/search` 只返回摘要 JSON，不返回全文。
- 转换后按页生成 Markdown，避免为了读两页而打开一个几十万行文件。
- 为每页记录标题、字符数、估算 Token 和关键词，Agent 先看索引。
- 图片仅保存路径和简短替代文本，Agent 需要视觉信息时再打开。
- 表格保留 Markdown/HTML 表格，但搜索结果只返回表名和短片段。
- 参考文献默认不读；只有追踪引用或用户要求时才转换/读取。
- 日志不进入回答上下文；成功时 CLI 只返回路径、页码和耗时。
- 超出预算时停止扩展页面，先根据已有证据回答或说明还需读取哪些页。

### 7.4 不同文档类型的默认阅读计划

**论文**：优先摘要、引言末尾、方法目标段、结果、局限性和结论；参考文献按需读取。若任务要求复现方法，再扩展公式、算法和实验设置页。

**datasheet**：先查目录、型号、absolute maximum ratings、electrical characteristics、pinout、register、timing、package 等关键词；表格通常补前后 1～2 页。

**标准/规范**：先用书签或目录定位条款，再读取定义、被引用条款和目标条款；保留条款编号与原始页码。

**用户手册**：先搜索错误码、功能名或操作名，再读取对应章节和前置条件。

**扫描书籍**：先 OCR 目录，建立章节到物理页的映射，再转换目标章节；不默认整本 OCR。

## 8. 时间优化方案

1. `inspect/search` 使用轻量原生 PDF 库，不加载 GPU 模型。
2. 对同一 PDF 的所有候选页先合并，再只启动一次 MinerU。
3. 非连续页通过临时子集 PDF 一次解析，避免每个页段重复启动服务和加载模型。
4. 以源文件 SHA-256、页面集和解析参数计算任务键；命中缓存时直接返回。
5. 同一个任务使用文件锁，防止多个 Agent 重复转换。
6. 可选增加常驻 MinerU 服务模式，连续处理多个任务时复用已加载模型。
7. `balanced` 作为默认档；只有内容确实需要时才启用 `high` 和图片分析。
8. 先完成最高相关页段并允许 Agent阅读，再根据答案缺口增量扩展。

常驻服务是第二阶段优化。第一阶段先实现“候选页合并 + 单次子集转换 + 缓存”，收益更直接且稳定性更高。

## 9. Agent CLI 设计

### 9.1 向后兼容

保留现有命令：

```powershell
.\MinerU-Local.exe paper.pdf --pages 3-8
```

新增子命令时，Skill 使用新的机器接口，原 GUI 和旧命令不受影响。

### 9.2 推荐子命令

```powershell
# 快速探测，不启动 MinerU
.\MinerU-Local.exe inspect "D:\docs\chip.pdf" --json

# 搜索原生文本并返回候选页
.\MinerU-Local.exe search "D:\docs\chip.pdf" --query "power supply current" --top-k 8 --json

# 转换连续或非连续页面
.\MinerU-Local.exe convert "D:\docs\chip.pdf" --pages "1-3,18,24-30" --profile balanced --json

# 根据问题自动完成探测、定位、页面规划和转换
.\MinerU-Local.exe prepare "D:\docs\chip.pdf" --query "What is the maximum input voltage?" --token-budget 12000 --json

# 查询已有缓存和输出
.\MinerU-Local.exe status "D:\docs\chip.pdf" --json
```

### 9.3 参数约定

- `--json`：stdout 只输出一个 UTF-8 JSON 对象。
- `--quiet`：不显示非必要日志。
- `--pages`：支持 `3`、`3-8`、`1-3,8,10-12`，页码从 1 开始。
- `--profile`：`fast-text`、`balanced`、`accurate`。
- `--token-budget`：只影响页面规划和阅读建议。
- `--context-pages`：默认 1，表格任务可设为 2。
- `--output-mode sibling`：默认且推荐，输出到 PDF 同级目录。
- `--reuse`：默认开启，复用有效缓存。
- `--force`：忽略缓存重新转换。
- `--timeout`：供 Agent 控制最长任务时间。
- `--max-pages`：防止自动流程意外转换整本超长文档。
- `--lang`：自动判断失败时显式指定 OCR 语言。

### 9.4 JSON 输出契约

成功示例：

```json
{
  "ok": true,
  "command": "prepare",
  "source": "D:\\docs\\chip.pdf",
  "document_id": "sha256:...",
  "pdf_kind": "text",
  "page_count": 216,
  "selected_ranges": "3-5,87-88",
  "output_dir": "D:\\docs\\chip.mineru",
  "markdown": "D:\\docs\\chip.mineru\\chip.md",
  "images_dir": "D:\\docs\\chip.mineru\\images",
  "cache": "hit",
  "elapsed_seconds": 0.18
}
```

失败示例：

```json
{
  "ok": false,
  "error_code": "PDF_ENCRYPTED",
  "message": "PDF requires a password",
  "retryable": false
}
```

### 9.5 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 成功，包括有效缓存命中 |
| 2 | 参数错误 |
| 3 | 输入文件不存在或格式不支持 |
| 4 | PDF 加密或损坏 |
| 5 | MinerU 环境或模型不可用 |
| 6 | 转换失败 |
| 7 | 超时或取消 |
| 8 | 输出或缓存锁冲突 |

Agent 不应通过匹配中文日志判断成功与否，只使用退出码和 JSON 字段。

## 10. 同级输出目录与文件结构

源文件：

```text
D:\docs\STM32F4-datasheet.pdf
```

默认创建：

```text
D:\docs\STM32F4-datasheet.mineru\
├── STM32F4-datasheet.md
├── images\
│   └── <content-hash>.png
└── raw\
    ├── inspect.json
    ├── manifest.json
    ├── index\
    ├── selections\
    ├── jobs\
    └── logs\
```

规则：

- 文件夹名使用 `<PDF 文件名去后缀>.mineru`。
- Agent 的正常阅读接口只有顶层 `<PDF 文件名去后缀>.md` 和 `images/`。
- 主 Markdown 包含当前任务所选页面的完整转换正文；多个页段按原始 PDF 页码顺序合并。
- 图片统一归集到 `images/`，使用内容哈希去重，Markdown 使用相对路径。
- `raw/` 保存索引、manifest、日志、分段 Markdown 和 MinerU 原始输出，正常阅读时不得打开。
- 主 Markdown 保留来源标记，例如：

```markdown
<!-- source: STM32F4-datasheet.pdf; pdf_page: 87 -->
```

- PDF 显示页码和物理页码不一致时，同时记录 `pdf_page`、`printed_page` 和映射置信度。
- 临时子集 PDF 放在对应任务目录，任务成功后可删除；页码映射必须保留。
- 默认不修改或覆盖源 PDF。

## 11. 缓存与幂等性

`raw/manifest.json` 至少记录：

- 源路径、文件大小、修改时间、SHA-256 和页数。
- MinerU、工具、模型和配置版本。
- 已转换原始页码。
- 每个页面使用的后端、method、effort、语言和功能开关。
- Markdown、图片和原始中间文件路径。
- 页面映射、创建时间、耗时和任务状态。

任务键建议为：

```text
SHA256(source_hash + selected_pages + backend + method + effort + lang + feature_flags + tool_version)
```

同一页已有同等或更高质量缓存时直接复用。源文件内容变化、工具主版本变化或参数不兼容时自动失效。缓存命中不复制重复图片。

## 12. Markdown 后处理要求

MinerU 的原始输出需要再经过一层确定性后处理：

1. 将当前任务的一个或多个页段按原始 PDF 页码顺序合并为顶层单一 Markdown。
2. 在主 Markdown 中保留源文件名、原始页段和解析配置标记。
3. 为标题、段落、公式、表格和图片保留顺序。
4. 把实际图片归集到顶层 `images/`，用内容哈希去重并重写相对链接。
5. 将索引、manifest、日志、分段 Markdown 和所有 MinerU 原始文件下沉到 `raw/`。
6. CLI 只返回主 Markdown 和图片目录，不向 Agent暴露内部文件列表。
7. 清除重复页眉页脚时保持保守，内部原始输出仍应可追溯。

如果无法可靠按页拆分，工具应明确返回 `page_mapping_reliable: false`，Agent 不得伪造页码引用。

## 13. 系统提示词设计

系统提示词只负责强制触发，不应复制完整工作流。建议使用以下内容：

```text
When a task requires facts, tables, formulas, specifications, or other content from a local PDF
(including papers, datasheets, standards, manuals, and reports), you MUST use the
$mineru-read-pdf skill before substantive reading. Inspect and search the document first, then
convert only the smallest relevant set of pages to Markdown. Do not convert or load an entire
long PDF unless the user explicitly requests full coverage or the skill determines that full
conversion fits the reading token budget. Read the generated Markdown, preserve original PDF
page provenance, and reuse the sibling .mineru cache when valid. Direct visual PDF reading is
only a fallback for validating a few pages, figures, or failed extraction.
```

中文等价规则：

```text
当任务需要从本地 PDF（包括论文、datasheet、标准、手册、报告）获取事实、表格、公式、
规格或正文时，必须先使用 $mineru-read-pdf。先探测和搜索文档，再把最少的相关原始页
转换成 Markdown。除非用户明确要求全文，或全文在阅读 Token 预算内，否则不要整本转换或
整本载入上下文。优先读取生成的 Markdown，保留原始 PDF 页码，并复用 PDF 同级的
.mineru 缓存。只有核对少量页面、图片或转换失败时才直接视觉阅读 PDF。
```

落地方式：

- Codex：把短规则加入适用工作区的 `AGENTS.md` 或产品提供的全局用户指令；Skill 的 description 同时承担自动触发。
- 其他 Agent 框架：把同一段加入 system message，并注册 CLI/Skill 工具。
- 不要只依赖用户每次手动说“先转换”；Skill 元数据和系统规则应共同触发。

如果用户只要求重命名、复制、移动或删除 PDF，而不需要理解内容，则不触发转换。

## 14. Skill 目录设计

建议将物理文件保存在当前 MinerU 目录：

```text
<project-root>\skills\mineru-read-pdf\
├── SKILL.md
├── agents\
│   └── openai.yaml
├── scripts\
│   ├── mineru-pdf.cmd
│   └── mineru_pdf.py
└── references\
    ├── cli.md
    ├── ocr-corrections.md
    └── strategies.md
```

各文件职责：

- `SKILL.md`：只保留触发后的核心决策和阅读流程，控制在约 100～180 行。
- `agents/openai.yaml`：显示名称、简短描述和默认提示。
- `scripts/mineru_pdf.py`：确定性调用 EXE、解析 JSON、处理超时和返回路径，避免 Agent 每次重写 PowerShell。
- `references/cli.md`：完整参数、JSON schema、退出码和错误恢复，仅在需要排错时读取。
- `references/strategies.md`：论文、datasheet、扫描书籍等细分策略，仅在对应任务触发时读取。

不要在 Skill 中加入额外 README、安装日志或大段 MinerU 文档。

若 Codex 只从用户 Skill 目录发现技能，可从用户 Skill 目录建立到上述目录的 NTFS 目录联接。这样 Skill 物理内容仍在当前 MinerU 目录，更新也只有一份。其他框架则直接把 Skill 搜索路径指向该目录。

## 15. `SKILL.md` 建议草案

正式创建时使用 `skill-creator` 的初始化和验证脚本。核心内容可采用下面的精简版本：

```markdown
---
name: mineru-read-pdf
description: Prepare and read local PDF content with the local MinerU Agent CLI. Use whenever an agent needs facts, text, tables, formulas, specifications, figures, or citations from papers, datasheets, standards, manuals, reports, scanned PDFs, or other PDF documents. Inspect and search first, convert the smallest relevant original-page set to Markdown, preserve page provenance, reuse sibling caches, and avoid loading entire long documents into context.
---

# Read PDFs with MinerU

Use the bundled wrapper; do not reconstruct shell commands when the wrapper is available.

1. Resolve the source PDF and the user's information need.
2. Run `scripts/mineru_pdf.py inspect <pdf>`.
3. For a specific question, run `scripts/mineru_pdf.py prepare <pdf> --query <query> --token-budget 12000`.
4. For an explicit page request, run `scripts/mineru_pdf.py convert <pdf> --pages <ranges>`.
5. Read only the returned top-level `<pdf-stem>.md`; do not inspect `raw/` during normal reading.
6. Search the main Markdown before opening large sections. Load files in `images/` only when the question depends on them.
7. Cite original PDF physical pages from page provenance markers.
8. Reuse a valid cache. Never modify the source PDF.

For long PDFs, do not convert all pages by default. Inspect bookmarks and native text, locate all candidate pages, add minimal context pages, merge ranges, and convert once. If native text is unavailable, convert likely contents/front-matter pages first and expand incrementally.

Use direct PDF page viewing only to validate at most a few figures/pages, resolve extraction ambiguity, or recover from conversion failure.

Read `references/strategies.md` only for document-type-specific planning. Read `references/cli.md` only for advanced flags, JSON fields, or error recovery.
```

Skill description 必须覆盖所有触发词，因为模型是否加载 Skill 主要由 name 和 description 决定。正文使用命令式表达，不重复系统提示词中的解释。

## 16. Skill 包装脚本设计

`scripts/mineru_pdf.py` 应：

1. 从脚本真实路径向上定位当前 MinerU 根目录和 `MinerU-Local.exe`，不写死用户名。
2. 使用参数数组调用 EXE，禁止 `shell=True`，正确处理空格和中文路径。
3. 强制 UTF-8，解析 stdout JSON，把进度 stderr 原样保留。
4. 对超时、取消、无效 JSON 和非零退出码生成统一错误。
5. 成功时只向 Agent 返回必要字段：原始页码、Markdown 路径、outline、Token 估算、缓存状态和耗时。
6. 不把完整 Markdown 内容打印到 stdout。
7. 支持 `inspect`、`search`、`prepare`、`convert` 和 `status`。

当 Skill 目录通过联接安装时，应先对脚本路径调用 `resolve()`，确保仍能找到物理目录中的 EXE。

## 17. Agent 的阅读与回答规则

- 在回答前确认至少一个事实来源对应到原始 PDF 页码。
- 区分物理 PDF 页码与文档印刷页码，例如“PDF 第 87 页（文档页码 75）”。
- 不把搜索片段当作最终证据；搜索只用于定位，最终阅读对应 Markdown 页面。
- 表格跨页时阅读完整表头、单位、脚注和条件。
- datasheet 数值必须同时核对单位、测试条件、typ/max/min 列和注释。
- 论文结论必须区分作者结论、实验结果和 Agent 推断。
- OCR 置信度低、页码映射不可靠或公式结构异常时，明确说明并查看原始页验证。
- 生成的 Markdown 是派生缓存，不应取代或修改源 PDF。

## 18. 异常和降级策略

| 问题 | 处理方式 |
|---|---|
| PDF 加密 | 返回明确错误，请用户提供密码或解密副本 |
| PDF 损坏 | 尝试轻量库和 MinerU 各一次，失败后停止 |
| 没有原生文本 | 标记扫描件，先 OCR 目录/样本页 |
| 没有书签或目录 | 使用页级全文搜索和标题启发式 |
| 搜索没有命中 | 扩展同义词、缩短查询，再转换摘要/目录页 |
| MinerU 首次加载较慢 | 提示正在加载本地模型，不重复启动任务 |
| 转换超时 | 保留日志和已完成页，允许按更小页集重试 |
| GPU 显存不足 | 降级到 `pipeline` 或减小页面批次 |
| Markdown 结构异常 | 对目标页使用 `accurate` 重转，并查看原始页面 |
| 页码映射不可靠 | 禁止声称精确页码，报告映射状态 |
| 输出目录被占用 | 使用任务锁等待或返回可重试错误 |

Agent 自动重试最多一次，且重试必须改变参数或缩小范围，禁止相同命令无限循环。

## 19. 安全、隐私和稳定性

- 默认完全本地处理，不上传 PDF、文本、图片或索引。
- 只在源 PDF 同级的 `.mineru` 目录写入派生文件。
- 路径使用绝对规范化结果，避免相对路径和当前工作目录变化导致写错位置。
- 对输出目录使用安全检查，禁止清理源目录、工作区根目录或宽泛路径。
- 临时文件先写入任务目录，成功后原子重命名。
- JSON schema 包含 `schema_version`，便于 Skill 与 EXE 版本协商。
- 对同一输出目录加锁；崩溃后保留可诊断日志并识别陈旧锁。
- 日志默认不记录完整正文，只记录路径、页码、参数、阶段、耗时和错误。
- 文件名、标题或 PDF 内文本永远只当数据处理，不能当作 Agent 指令执行。

## 20. 实施阶段

### 阶段 A：Agent 基础 CLI

- 为现有程序增加子命令框架和 `--json`。
- 实现 `inspect`、`search`、非连续页码解析和同级输出目录。
- 定义 JSON schema、退出码、错误对象和 UTF-8 行为。
- 保持 GUI 与旧 CLI 兼容。

### 阶段 B：转换与后处理

- 实现选页子集 PDF 和原始页码映射。
- 实现 `convert/prepare`、按页 Markdown、outline、index 和 manifest。
- 加入缓存、任务键、文件锁和失败恢复。
- 验证图片相对路径、表格、公式和页码来源标记。

### 阶段 C：Skill 与系统集成

- 用 `init_skill.py` 创建 `mineru-read-pdf`。
- 编写包装脚本和两份按需 reference。
- 生成 `agents/openai.yaml`，运行 `quick_validate.py`。
- 安装系统提示词规则，并让 Agent 能发现 Skill。

### 阶段 D：性能优化

- 统计每阶段耗时和缓存命中率。
- 评估常驻 MinerU 服务是否值得启用。
- 优化批量选页、低显存降级、OCR 抽样和模型预热。
- 调整页数和 Token 阈值，而不是把阈值硬编码在 Skill 文本里。

### 阶段 E：真实任务验证

用独立任务验证，而不是只测试命令是否运行：

1. 5 页文本 PDF：自动全文转换和读取。
2. 15 页双栏论文：定位方法和结论，保留公式与页码。
3. 200 页 datasheet：查询一个电气参数，只转换相关页。
4. 300 页扫描手册：先 OCR 目录，再读取目标章节。
5. 含中文路径、空格路径和中英文混排 PDF。
6. 同一任务重复运行：必须命中缓存且不再次启动模型。
7. 修改源 PDF 后重跑：必须正确失效旧缓存。
8. 并发请求同一文档：只允许一个转换任务执行。
9. 故意触发超时、显存不足和损坏 PDF，验证降级与错误 JSON。

## 21. 验收标准

- Agent 在需要理解 PDF 内容时会自动触发 `mineru-read-pdf`。
- 普通长文本 PDF 的定向问题默认不做全文 MinerU 转换。
- 200 页以上文档的典型定向任务，首轮转换页面应尽量控制在全文 10% 以内；无法做到时要解释原因。
- 输出始终位于源 PDF 同级 `<文件名>.mineru` 目录。
- 每个 Markdown 页面可追溯到原始 PDF 物理页码。
- CLI 的 stdout 是可解析 JSON，日志不会污染 JSON。
- 相同源文件和参数重复执行可以复用缓存。
- Agent 不会默认把完整 Markdown、原始 JSON 或全部图片载入上下文。
- 扫描件、复杂表格和转换失败都有明确降级路径。
- GUI、旧 CLI 和新 Agent CLI 可以共存。

## 22. 推荐默认值

| 配置 | 默认值 |
|---|---|
| 输出模式 | PDF 同级 `<stem>.mineru` |
| Agent 阅读预算 | 12k Token |
| 全文转换页数阈值 | 25 页 |
| 全文转换 Token 阈值 | 30k Token |
| 搜索命中数量 | 8 |
| 普通上下文页 | 前后各 1 页 |
| 表格上下文页 | 前后各 2 页 |
| 默认解析档 | `balanced` |
| 默认后端 | `hybrid-engine` |
| 默认 effort | `medium` |
| 默认图片分析 | 关闭，按需开启 |
| 自动重试 | 最多 1 次，必须改变策略 |
| 缓存 | 开启 |

这些值应放进当前 MinerU 目录内的配置文件，例如 `agent-config.json`，而不是写死在 Skill 中，方便根据实际 GPU 性能和 Agent 上下文大小调整。

## 23. 最终建议

下一步实现时，优先顺序应为：

1. `inspect + search + JSON`，先解决“快速知道该读哪里”。
2. 非连续选页一次转换、同级输出和原始页码映射。
3. 按页 Markdown、缓存和 `prepare` 自动编排。
4. Skill 与系统提示词集成。
5. 最后再做常驻服务和更复杂的自动分类。

这样第一阶段就能显著降低 Token；后续优化主要降低等待时间，而不会推翻前面的接口和目录结构。

## 24. 按异常触发的 OCR 与排版纠错

MinerU/OCR 生成的 Markdown 可能出现少量识别或排版问题，目录、多栏文本、公式和表格尤其容易受影响。纠错不应成为默认步骤，否则会显著增加 Token 和等待时间。

推荐规则：

1. Agent 默认直接使用生成的 Markdown，不做全文校对、润色或格式统一。
2. 只有已经观察到的问题会妨碍导航、检索或改变含义时才触发纠错，例如目录条目错序、页码错配、多栏串行、明显 OCR 替换、表格列错位、公式符号损坏或可疑数值/单位。
3. 只打开并核对受影响的原始 PDF 页面；目录问题只检查相关目录页和必要的页码确认页。
4. 只修正主 Markdown 中最小受影响范围，并把审计副本写入 `<stem>.mineru/raw/reviewed/`。不得覆盖 MinerU 原始 Markdown 或源 PDF。
5. 数值、单位、型号、公式和技术结论的修改必须有原始页面视觉证据；无法确认时保留原文并报告不确定性。
6. 轻微换行、空格、标点风格或不影响理解的 Markdown 外观问题不处理。
7. 严重损坏时先对单个目标页用 `accurate` 重转一次，再考虑人工式修正；禁止对同一页无限重试。

为避免常规任务加载额外上下文，详细规则应单独存放在 `references/ocr-corrections.md`。`SKILL.md` 只保留一条异常触发条件，Agent 仅在实际发现问题时读取该 reference。
