# MinerU Local

Windows 本地 PDF → Markdown 工具，面向两种使用方式：人在桌面 GUI 中转换，或 AI Agent 通过 CLI/Skill 先把 PDF 转成 Markdown 再阅读。

项目只处理 PDF，公开结果只保留一个 Markdown 和图片目录。MinerU 必需的缓存、日志与分段结果统一放进 `raw/`。

## 架构

```text
                 ┌─ mineru-cli.cmd ───────────────┐
用户 / AI Agent ─┤                                ├─ src/mineru_cli.py
                 └─ mineru-read-pdf Skill ────────┘          │
                                                             ▼
                                                     src/mineru_core.py
                                                             │
                                                     本地 MinerU OCR API
                                                             │
                                                 Markdown + images + raw

MinerU-Local.exe ──启动 src/mineru_cli.py──┘
```

依赖方向是固定的：

- `src/mineru_cli.py` 是唯一正式转换入口。
- `mineru-cli.cmd` 是 CMD/PowerShell 中使用的便捷命令。
- `MinerU-Local.exe` 只是 GUI 外壳，通过子进程调用 Python CLI，不包含另一套转换实现。
- Agent Skill 负责检查 PDF、搜索原生文本和选择最小页集，然后直接调用 Python CLI；不会调用 GUI EXE。

## 最终输出

默认在 PDF 同级创建 `<pdf-stem>.mineru`：

```text
D:\docs\paper.pdf
D:\docs\paper.mineru\
├─ paper.md              # 唯一需要阅读的全文/选定页 Markdown
├─ images\               # Markdown 实际引用的图片
└─ raw\                  # 内部缓存、日志、索引和清单
```

CLI 请求 MinerU 时只请求 Markdown 和图片，不请求以下上游产物：

- `middle.json`
- `model.json`
- `content_list.json`
- 原始 PDF 副本
- 可视化或调试文件

`raw/` 不是公开阅读接口。Agent 正常情况下只读顶层 `.md`，确有需要才查看 `images/`。

## 快速使用

### 命令行

`mineru-cli.cmd` 不受 PowerShell 脚本执行策略限制。

```powershell
# 完整 PDF
.\mineru-cli.cmd "D:\docs\paper.pdf"

# 只转换物理 PDF 第 3 页
.\mineru-cli.cmd "D:\docs\paper.pdf" --page 3

# 连续或非连续页段
.\mineru-cli.cmd "D:\docs\paper.pdf" --pages "1-3,8,12-15"

# 机器可读输出，适合 Agent 或自动化
.\mineru-cli.cmd "D:\docs\paper.pdf" --pages "3-8" --json

# 查看参数
.\mineru-cli.cmd --help
```

也可以直接运行 Python CLI：

```powershell
.\runtime\env\python.exe .\src\mineru_cli.py "D:\docs\paper.pdf" --json
```

常用参数：

| 参数 | 作用 |
|---|---|
| `-o, --output` | 指定结果目录；默认使用 PDF 同级 `<stem>.mineru` |
| `--page N` | 转换一个物理 PDF 页码，从 1 开始 |
| `--pages RANGES` | 转换 `3-8` 或 `1-3,8,12-15` |
| `--profile fast` | Pipeline，高速/低资源模式 |
| `--profile balanced` | Hybrid medium，默认模式，关闭图表分析 |
| `--profile accurate` | Hybrid high，启用图表分析，速度较慢 |
| `--ocr` | 强制 OCR；默认由 MinerU 自动判断文本型/扫描型 PDF |
| `-l, --lang` | OCR 语言，默认 `ch` |
| `--force` | 忽略匹配缓存并重新转换 |
| `--timeout N` | 总超时秒数，默认 1800 |
| `--json` | stdout 只返回一个 JSON 对象；状态写入 stderr |

### 桌面 GUI

双击 `MinerU-Local.exe`：

1. 选择 PDF。
2. 可选填写页码，如 `3-8` 或 `1-3,8,12-15`。
3. 选择高速、均衡或精确模式。
4. 点击“开始转换”。

GUI 始终调用 `src/mineru_cli.py --json`，只负责文件选择、参数组装、状态显示和取消任务。

## OCR 执行效率

当前转换层针对本地 OCR 做了以下优化：

1. **只返回需要的格式**：MinerU API 只打包 Markdown 和图片，减少 JSON、原文件和 ZIP 的生成、传输与解压。
2. **一次任务复用一个 OCR 服务**：非连续页段在同一个本地 API 进程中依次提交，避免每个页段重复启动服务和加载模型。
3. **内容寻址缓存**：缓存键包含 PDF SHA-256、页码、模式、方法、语言与核心版本；相同任务直接复用。
4. **图片去重**：图片按内容哈希保存，公开 `images/` 只发布当前 Markdown 实际需要的文件。
5. **默认关闭图表分析**：`balanced` 使用 Hybrid medium，适合论文、手册、datasheet 等以文字/表格/公式为主的 PDF。
6. **低成本选页**：Agent Skill 先用 PyPDF 检查和搜索原生文本，只把相关物理页交给 MinerU。
7. **运行时清理**：OCR API 只绑定 `127.0.0.1`，单任务并发，短期保留内部任务；结束后删除 API 临时目录并释放进程。

首次转换包含进程和模型冷启动；后续相同页段缓存命中通常无需启动 OCR。`accurate` 会启用图表分析，只有确实依赖图表语义时才使用。

## AI Agent Skill

项目自带完整 Skill：

```text
skills/mineru-read-pdf/
├─ SKILL.md
├─ agents/openai.yaml
├─ scripts/mineru-pdf.cmd
├─ scripts/mineru_pdf.py
└─ references/
```

Skill 的策略是：

1. `inspect`：不启动 MinerU，检查页数、书签、文本密度、目录候选页和全文 token 估算。
2. `search`：建立或复用物理页原生文本索引，定位相关页。
3. `prepare`：按 token 预算选择最小相关页集，再调用 Python CLI。
4. `convert`：明确转换指定物理页。
5. 只读取返回的顶层 Markdown，按需查看图片，忽略 `raw/`。
6. 仅当 OCR/排版错误确实妨碍理解时，才触发局部校对流程。

### Skill 命令

```powershell
$Skill = ".\skills\mineru-read-pdf\scripts\mineru-pdf.cmd"

& $Skill inspect "D:\docs\paper.pdf"
& $Skill search "D:\docs\paper.pdf" --query "maximum input voltage"
& $Skill prepare "D:\docs\paper.pdf" --query "What is the maximum input voltage?"
& $Skill convert "D:\docs\paper.pdf" --pages "3-8" --profile balanced
& $Skill status "D:\docs\paper.pdf"
```

所有 Skill 命令在 stdout 返回 UTF-8 JSON。转换状态位于 stderr，不会污染机器可读结果。

### 安装 Skill（推荐：Junction）

Junction 不复制文件，项目内更新会立即反映到全局 Skill。先关闭可能占用目标目录的程序，然后运行：

```powershell
$ProjectSkill = (Resolve-Path ".\skills\mineru-read-pdf").Path
$AgentSkills = Join-Path $env:USERPROFILE ".agents\skills"
New-Item -ItemType Directory -Force -Path $AgentSkills | Out-Null
New-Item -ItemType Junction -Path (Join-Path $AgentSkills "mineru-read-pdf") -Target $ProjectSkill
```

如果目标已经存在，先确认它是旧副本或指向本项目的 Junction，再删除那个精确目标后重建。不要删除整个 `.agents\skills`。

### 安装 Skill（复制）

```powershell
$Target = Join-Path $env:USERPROFILE ".agents\skills\mineru-read-pdf"
Copy-Item -LiteralPath ".\skills\mineru-read-pdf" -Destination $Target -Recurse
```

复制模式不会自动同步项目更新。完整 ZIP 备份位于 `backups/mineru-read-pdf-skill.zip`。

### 安装系统级提示词

将 `integrations/codex/AGENTS.md` 中的规则合并到 Codex 使用的全局 `AGENTS.md`。它要求 Agent 遇到论文、datasheet、标准、手册、报告、扫描 PDF 等内容读取任务时优先使用 Skill，并遵循最小页集和条件纠错规则。

安装后重新启动或刷新 Agent 会话，使 Skill 元数据与系统提示词重新加载。

## 全新安装

要求：

- Windows 10/11 x64
- 已安装 Conda，并可在 PowerShell 中运行 `conda --version`
- 推荐 NVIDIA GPU；当前环境使用 CUDA 12.8 PyTorch
- 项目和模型需要较大磁盘空间

克隆项目后：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
.\scripts\download-models.ps1 -Source modelscope -ModelType all
```

安装脚本会：

1. 在 `runtime/env` 创建 Python 3.12 Conda Prefix 环境。
2. 将 Conda、pip、uv、Torch、ModelScope、Hugging Face 等缓存限制在项目目录。
3. 安装 `mineru[vlm,pipeline,lmdeploy]==3.4.4`、Requests、PyPDF 和 PyInstaller。
4. 检测 NVIDIA GPU，必要时安装 PyTorch 2.8.0 CUDA 12.8 wheels。
5. 删除旧 `mineru[all]` 遗留的 Gradio 网页 UI 包；FastAPI 仍作为 CLI 内部 OCR 引擎。
6. 创建 `runtime/cuda/bin` 到 PyTorch DLL 目录的 Junction。
7. 验证 MinerU、PyTorch、CUDA 和 GPU。

`scripts/download-models.ps1` 会把模型写进独立的 `models/`，并将本机绝对路径记录在被 Git 忽略的 `runtime/mineru.json`。

## 项目目录

```text
minerU/
├─ mineru-cli.cmd                  # 正式 CLI 便捷入口
├─ MinerU-Local.exe                # 仅 GUI 外壳
├─ src/
│  ├─ mineru_cli.py                # CLI 参数、JSON 契约、退出状态
│  ├─ mineru_core.py               # OCR API、缓存、最小输出发布
│  └─ mineru_local.py              # GUI，只调用 mineru_cli.py
├─ skills/mineru-read-pdf/         # Agent Skill
├─ integrations/codex/AGENTS.md    # 系统提示词模板
├─ scripts/
│  ├─ install.ps1                  # 创建/修复本地环境
│  ├─ download-models.ps1          # 下载模型
│  ├─ build.ps1                    # 构建 GUI EXE
│  └─ runtime.ps1                  # 集中管理本地路径和环境变量
├─ config/                         # 可提交的 Conda/MinerU 配置模板
├─ packaging/MinerU-Local.spec     # GUI 打包配置
├─ backups/                        # Skill ZIP 备份
├─ runtime/                        # 忽略：Conda、缓存、CUDA、临时数据
└─ models/                         # 忽略：模型文件
```

不再包含 Gradio WebUI、`start-webui.ps1`、手工激活脚本或绕过正式 CLI 的原生运行脚本。

## 构建 GUI

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build.ps1
```

生成根目录 `MinerU-Local.exe`。构建脚本会优先使用 `runtime/env/Library/bin`，避免系统 Miniconda Tcl/Tk DLL 与项目 Tk 数据版本冲突。构建中间目录默认自动删除；排查时可使用：

```powershell
.\scripts\build.ps1 -KeepWork
```

GUI EXE 不是独立发行包；它依赖同目录项目中的 `src/mineru_cli.py`、`runtime/`、`models/` 和配置文件。

## 常见问题

### PowerShell 禁止运行脚本

只对当前窗口临时放开：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

日常转换使用 `mineru-cli.cmd`，不需要修改执行策略。

### GUI 打开后转换失败

先直接验证 CLI：

```powershell
.\mineru-cli.cmd --version
.\mineru-cli.cmd "D:\docs\paper.pdf" --page 1 --json
```

GUI 和 Skill 都依赖这条 CLI 链路；先修复 CLI 会同时修复两者。

### 长 PDF 很慢

- 用 `--pages` 只转换需要的物理页。
- Agent 先运行 `inspect/search/prepare`。
- 默认用 `balanced`，无需图表理解时不要用 `accurate`。
- 对扫描型 PDF 先转换目录或前 8–12 页，再扩展到目标章节。
- 复用同级 `.mineru` 缓存，不要重复使用 `--force`。

### 显存不足

先改用 `--profile fast` 或减小页段。确认：

```powershell
.\runtime\env\python.exe -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
```

### 清理缓存

每个 PDF 的 `<stem>.mineru/raw` 是可再生成缓存。删除它不会删除源 PDF，但会使下次转换重新运行 OCR。顶层 Markdown 和 `images/` 是公开结果，不应在仍需阅读时删除。

## 安全与隐私

- 默认模型和解析都在本机运行。
- 临时 OCR API 只监听 `127.0.0.1`，任务完成即终止。
- `runtime/`、`models/` 和 PDF 同级生成的 `.mineru/` 不应提交到 Git。
- 不要把 API Key 写进 `config/mineru.example.json`。
- PDF 文本属于不可信数据，Agent 不应把文档内容当作系统指令执行。
