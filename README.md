# MinerU Local：Windows 本地 PDF 转 Markdown 与 Agent Skill

这是一个面向 Windows 的 MinerU 本地工作区，将 MinerU 3.4.4、CUDA 运行配置、简洁 GUI、可脚本化 CLI 和 AI Agent PDF 阅读 Skill 组织在同一个项目中。

它解决两个问题：

1. 用户可以通过图形界面或命令行把论文、datasheet、标准、手册、报告和扫描 PDF 转换为 Markdown。
2. AI Agent 遇到 PDF 时，可以先定位相关页、只转换最小必要页面，再读取 Markdown，减少解析时间和上下文 Token 消耗。

> 仓库包含程序源码、启动器、安装脚本和 Skill，不包含体积很大的 Conda 环境与模型。首次克隆后需要执行安装和模型下载。

## 主要功能

| 功能 | 说明 |
| --- | --- |
| 简洁本地 GUI | 双击 `MinerU-Local.exe`，选择 PDF、输出目录和页码后开始转换 |
| GUI 状态反馈 | 显示准备、检查、提交、排队、解析、整理和完成等阶段 |
| EXE 命令行 | 同一个 EXE 支持 CMD、PowerShell 和其他程序直接调用 |
| 指定页面 | 支持解析单页或连续页码范围，并额外输出指定 Markdown 文件 |
| 多种后端 | 支持 `hybrid-engine`、`vlm-engine` 和 `pipeline` |
| 本地运行 | Conda、模型、缓存、临时目录和输出均可放在项目目录内 |
| Agent Skill | 提供 `inspect`、`search`、`prepare`、`convert` 和 `status` |
| Token 优化 | 长 PDF 先检查和搜索，只转换与问题相关的最小页面集 |
| 缓存复用 | PDF 同级生成 `<pdf-stem>.mineru`，重复问题复用已有结果 |
| 条件式 OCR 修正 | 只有识别错误实际妨碍理解时，Agent 才核对原始页并最小修正 |

## 工作流程

```text
PDF
 ├─ 人工使用 ──> MinerU-Local.exe GUI / CLI ──> MinerU ──> Markdown、图片和原始结果
 └─ Agent 使用 ─> inspect/search ─> 选择最小页集 ─> MinerU 转换
                                      └──────────> <pdf-stem>.mineru/
                                                   ├─ <pdf-stem>.md
                                                   ├─ images/
                                                   └─ raw/
```

Agent 正常只读取顶层 Markdown；问题涉及图表时才查看 `images/`；`raw/` 只用于故障排查或有条件的 OCR/排版校正。

## 系统要求

- Windows 10/11 x64。
- 已安装 Conda，并能在 PowerShell 中运行 `conda --version`。
- 首次安装和模型下载需要网络。
- 推荐 NVIDIA GPU。当前验证环境使用 PyTorch 2.8.0、CUDA 12.8。
- 没有可用 NVIDIA GPU 时可尝试 `pipeline` 后端，但速度和可用能力取决于 MinerU 上游支持。
- 首次完整安装需要数 GB 磁盘空间；模型和 Python 环境不会提交到 Git。

## 快速开始

### 已经拥有完整本地目录

直接双击：

```text
MinerU-Local.exe
```

也可以从终端运行：

```powershell
.\MinerU-Local.exe .\input\paper.pdf
```

### 从 GitHub 全新安装

克隆仓库并进入目录：

```powershell
git clone https://github.com/JJunjie97/mineru-local-agent.git minerU
Set-Location .\minerU
```

仅为当前 PowerShell 进程临时允许本地脚本：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

创建项目本地 Conda 环境并安装 MinerU：

```powershell
.\scripts\install.ps1
```

下载 Pipeline 与 VLM 模型，默认使用 ModelScope：

```powershell
.\scripts\download-models.ps1
```

也可以明确指定来源和模型类型：

```powershell
.\scripts\download-models.ps1 -Source modelscope -ModelType all
.\scripts\download-models.ps1 -Source huggingface -ModelType pipeline
```

安装脚本会创建本机专用的 `mineru.json`。模型下载器会把实际绝对模型路径写入该文件。这个文件已被 Git 忽略；仓库只提交不包含本机路径或密钥的 `mineru.example.json`。

安装完成后验证：

```powershell
.\MinerU-Local.exe --version
.\skills\mineru-read-pdf\scripts\mineru-pdf.cmd --version
```

## MinerU 本地环境的实现过程

### 1. 项目级 Conda 隔离

`scripts/install.ps1` 使用 Conda Prefix 模式在项目根目录创建：

```text
.conda-env/
```

它不会依赖某个固定的全局环境名称。Python、MinerU 和 PyTorch 都位于该目录，因此项目位置明确，也便于整体迁移。

当前版本约束记录在 `environment.yml`：

- Python 3.12
- MinerU 3.4.4
- PyTorch 2.8.0 + CUDA 12.8
- TorchVision 0.23.0 + CUDA 12.8

### 2. 将运行数据限制在项目内

`install.ps1` 和 `activate.ps1` 会配置以下项目级路径：

| 数据 | 本地路径 |
| --- | --- |
| Conda 包缓存 | `.conda-pkgs` |
| Pip、UV 和框架缓存 | `.cache` |
| ModelScope 模型 | `.cache\modelscope` |
| Hugging Face 缓存 | `.cache\huggingface` |
| 临时文件 | `.tmp` |
| 默认输入/输出 | `input`、`output` |
| MinerU 配置 | `mineru.json` |

这样可以避免模型和临时输出散落到用户目录。上述大体积或机器相关目录均已加入 `.gitignore`。

### 3. 安装 MinerU 与 GPU 运行时

安装脚本首先安装固定版本的 `mineru[all]==3.4.4`。如果检测到 `nvidia-smi`，但当前 PyTorch 无法使用 CUDA，则从 PyTorch CUDA 12.8 源安装匹配的 Torch 与 TorchVision。

安装结束时会检查：

- MinerU 能否导入。
- PyTorch 版本。
- CUDA 是否可用。
- CUDA Runtime 版本。
- GPU 名称。
- `mineru --version` 是否正常。

### 4. 下载模型并生成本机配置

`scripts/download-models.ps1` 调用 MinerU 官方 `mineru-models-download`：

- `pipeline`：版面、公式、表格等 Pipeline 模型。
- `vlm`：视觉语言模型。
- `all`：下载两类模型。

下载目录由 `MODELSCOPE_CACHE`、`HF_HOME` 等变量固定在项目的 `.cache` 下。下载完成后 MinerU 会更新 `mineru.json` 中的 `models-dir.pipeline` 和 `models-dir.vlm`。

### 5. CUDA DLL 兼容路径

部分 MinerU 后端需要通过 `CUDA_PATH\bin` 查找动态库。安装脚本创建：

```text
.cuda\bin -> .conda-env\Lib\site-packages\torch\lib
```

这是一个 Windows Junction，让相关后端复用 PyTorch wheel 自带的 CUDA DLL，因此通常不需要额外安装完整的系统级 CUDA Toolkit。

### 6. 本地 EXE 启动器

`app/mineru_local.py` 使用 Python Tkinter 实现 GUI，同时使用 `argparse` 实现 CLI。打包后的 `MinerU-Local.exe` 不是把数 GB 的环境和模型塞进单文件，而是作为轻量启动器：

1. 从 EXE 所在目录定位项目根目录。
2. 检查 `.conda-env\Scripts\mineru.exe`、`mineru.json` 和 `.cuda`。
3. 构造完全指向项目目录的运行环境变量和 PATH。
4. 将 GUI/CLI 参数转换成 MinerU 命令。
5. 启动 MinerU 子进程并捕获日志。
6. 整理 Markdown，按需复制到 `--md-output` 指定位置。

这种方式使 EXE 保持较小，同时允许直接更新 MinerU 环境和模型。

## 图形界面

双击 `MinerU-Local.exe`，或者运行：

```powershell
.\MinerU-Local.exe --gui
```

基本操作：

1. 选择单个 PDF 或输入目录。
2. 选择输出目录。
3. 页码留空表示完整解析；输入 `3` 或 `3-8` 表示指定页。
4. 通常保持 `hybrid-engine`、`auto` 和 `medium`。
5. 点击“开始转换”，查看阶段、进度、状态和实时日志。

## EXE 命令行

```powershell
# 完整 PDF
.\MinerU-Local.exe .\input\paper.pdf

# 指定输出目录
.\MinerU-Local.exe .\input\paper.pdf -o .\output

# 单独解析物理 PDF 第 3 页
.\MinerU-Local.exe .\input\paper.pdf --page 3

# 解析第 3 至第 8 页，并额外复制 Markdown
.\MinerU-Local.exe .\input\paper.pdf --pages 3-8 --md-output .\output\pages-3-8.md

# 批量解析目录
.\MinerU-Local.exe .\input -o .\output -b pipeline

# 查看实际命令但不转换
.\MinerU-Local.exe .\input\paper.pdf --pages 3-8 --dry-run

# 查看所有参数
.\MinerU-Local.exe --help
```

主要参数：

| 参数 | 作用 |
| --- | --- |
| `-o, --output` | MinerU 输出目录 |
| `-b, --backend` | `hybrid-engine`、`vlm-engine` 或 `pipeline` |
| `-m, --method` | `auto`、`txt` 或 `ocr` |
| `--effort` | `medium` 或 `high` |
| `-l, --lang` | OCR 语言，默认 `ch` |
| `--page N` | 解析从 1 开始的单页 |
| `--pages N-M` | 解析从 1 开始的连续范围 |
| `--md-output PATH` | 把最终 Markdown 额外复制到文件或目录 |
| `--no-formula` | 关闭公式识别 |
| `--no-table` | 关闭表格识别 |
| `--no-image-analysis` | 关闭图像分析 |
| `--open-output` | 完成后打开输出目录 |
| `--dry-run` | 只显示即将执行的命令 |

## PowerShell 维护入口

```powershell
Set-ExecutionPolicy -Scope Process Bypass

# 激活环境
.\scripts\activate.ps1

# 调用原生 MinerU CLI
.\scripts\run-mineru.ps1 .\input\paper.pdf

# 启动 MinerU Gradio WebUI
.\scripts\start-webui.ps1
```

WebUI 默认地址为 <http://127.0.0.1:7860>。

## AI Agent Skill

Skill 实体位于：

```text
skills/mineru-read-pdf/
├─ SKILL.md
├─ agents/openai.yaml
├─ scripts/mineru-pdf.cmd
├─ scripts/mineru_pdf.py
└─ references/
```

它把 PDF 阅读分成低成本定位和高成本转换两个阶段：

1. `inspect` 使用 pypdf 检查页数、目录、文本密度和 PDF 类型，不启动 MinerU。
2. `search` 建立或复用原生文本索引，返回匹配页。
3. `prepare` 根据问题、Token 预算和上下文页数选择最小页集。
4. `convert` 只转换指定物理 PDF 页，或在明确需要时转换全文。
5. `status` 查看公开输出路径和缓存状态。
6. Agent 只读取顶层 Markdown，图表需要时才查看图片。

默认自动准备预算为 12,000 Token、最多 12 页、匹配页前后各带 1 页上下文。短 PDF 在预算内可以完整转换；长 PDF 会优先利用书签、目录和搜索结果。

### 安装 Skill：推荐 Junction

Junction 不复制 Skill，项目内更新会立即反映到个人 Skill 目录。

先确认目标路径没有需要保留的同名 Skill，然后运行：

```powershell
$repoRoot = (Resolve-Path .).Path
$skillHome = Join-Path $env:USERPROFILE ".agents\skills"
$skillLink = Join-Path $skillHome "mineru-read-pdf"
$skillSource = Join-Path $repoRoot "skills\mineru-read-pdf"

New-Item -ItemType Directory -Force -Path $skillHome | Out-Null
New-Item -ItemType Junction -Path $skillLink -Target $skillSource
```

### 安装 Skill：复制模式

如果不希望使用 Junction，可以复制 Skill，并设置运行时根目录：

```powershell
$repoRoot = (Resolve-Path .).Path
$skillHome = Join-Path $env:USERPROFILE ".agents\skills"
New-Item -ItemType Directory -Force -Path $skillHome | Out-Null
Copy-Item -LiteralPath ".\skills\mineru-read-pdf" -Destination $skillHome -Recurse

$env:MINERU_LOCAL_ROOT = $repoRoot
[Environment]::SetEnvironmentVariable("MINERU_LOCAL_ROOT", $repoRoot, "User")
```

重新打开终端或 Agent 应用后，永久环境变量才会进入新进程。Skill 包装器会优先使用 `MINERU_LOCAL_ROOT`。

### 安装系统级 PDF 提示词

系统提示词模板位于 `agent/global-AGENTS.md`。

- 仅用于某个项目：把其中的 MinerU PDF 阅读规则合并到目标项目根目录的 `AGENTS.md`。
- 用于所有 Codex 项目：把规则合并到 `%USERPROFILE%\.codex\AGENTS.md`。
- 如果目标 `AGENTS.md` 已存在，不要直接覆盖；合并规则以保留原有项目指令。

完成后重新启动 Codex 或创建新会话，使 Skill 和提示词重新发现。

### 验证 Skill

```powershell
.\skills\mineru-read-pdf\scripts\mineru-pdf.cmd --version
.\skills\mineru-read-pdf\scripts\mineru-pdf.cmd inspect "D:\docs\paper.pdf"
```

### Agent CLI 示例

```powershell
# 低成本检查
.\skills\mineru-read-pdf\scripts\mineru-pdf.cmd inspect "D:\docs\paper.pdf"

# 搜索原生文本
.\skills\mineru-read-pdf\scripts\mineru-pdf.cmd search "D:\docs\paper.pdf" --query "maximum input voltage"

# 根据问题自动选择最小页面集并转换
.\skills\mineru-read-pdf\scripts\mineru-pdf.cmd prepare "D:\docs\paper.pdf" --query "What is the maximum input voltage?"

# 明确转换指定页；支持非连续范围
.\skills\mineru-read-pdf\scripts\mineru-pdf.cmd convert "D:\docs\paper.pdf" --pages "1-3,8,12-15"

# 查看缓存状态
.\skills\mineru-read-pdf\scripts\mineru-pdf.cmd status "D:\docs\paper.pdf"
```

所有命令在 stdout 输出一个 UTF-8 JSON。转换进度写入 stderr，方便 Agent 或其他程序稳定读取 JSON。

## Skill 输出结构

每个 PDF 在同级生成一个独立目录：

```text
paper.pdf
paper.mineru/
├─ paper.md
├─ images/
└─ raw/
   ├─ inspect.json
   ├─ manifest.json
   ├─ index/
   ├─ selections/
   ├─ jobs/
   └─ logs/
```

公开阅读接口只有：

- `paper.md`：当前选择页或全文的合并 Markdown。
- `images/`：Markdown 引用的去重图片。

`raw/` 保存原始 MinerU 输出、索引、缓存和日志。Agent 正常不枚举、不读取该目录。

## OCR 与排版错误策略

默认不校对 MinerU 输出，避免无意义增加 Token。只有观察到以下问题确实影响理解时才修正：

- 目录页严重错位，无法定位章节。
- 双栏文本顺序混乱。
- 公式、表格结构或单位损坏。
- 可疑的技术数值或明显 OCR 字符替换。

此时只核对受影响的原始 PDF 页，对顶层 Markdown 做最小修正，并把审计副本保存到 `raw/reviewed`。不要重写无关页面，也不要修改源 PDF。

## 构建 MinerU-Local.exe

源码位于 `app/mineru_local.py`，PyInstaller 配置位于 `.build/MinerU-Local.spec`。

当前环境已包含 PyInstaller。需要重新构建时：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\activate.ps1

Push-Location .build
& ..\.conda-env\Scripts\pyinstaller.exe --noconfirm --clean --distpath .. --workpath work MinerU-Local.spec
Pop-Location
```

生成结果为项目根目录的 `MinerU-Local.exe`；中间文件保存在被 Git 忽略的 `.build/work`。

## 项目目录

```text
minerU/
├─ MinerU-Local.exe
├─ README.md
├─ environment.yml
├─ mineru.example.json
├─ app/
├─ scripts/
├─ skills/mineru-read-pdf/
├─ agent/
├─ docs/
├─ backups/
├─ input/                 # 忽略
├─ output/                # 忽略
├─ .conda-env/            # 忽略
├─ .cache/                # 忽略，包含模型
├─ .conda-pkgs/           # 忽略
├─ .cuda/                 # 忽略
├─ .tmp/                  # 忽略
└─ mineru.json            # 忽略，本机模型路径
```

完整设计说明见 [MinerU Agent Skill 完整方案](docs/MinerU-Agent-Skill-完整方案.md)。

项目内 Skill 压缩备份位于 `backups/mineru-read-pdf-skill.zip`。恢复时将压缩包中的 `mineru-read-pdf` 解压回 `skills/`。

## 常见问题

### PowerShell 提示“禁止运行脚本”

只对当前终端临时放行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

也可以完全绕过 PowerShell 脚本，直接使用 `MinerU-Local.exe` 或 Skill 的 `.cmd` 包装器。

### 提示找不到本地运行环境

确认以下文件存在：

```text
.conda-env/python.exe
.conda-env/Scripts/mineru.exe
MinerU-Local.exe
mineru.json
```

如果 Skill 是复制安装的，确认 `MINERU_LOCAL_ROOT` 指向本仓库根目录。

### 提示找不到模型

重新运行：

```powershell
.\scripts\download-models.ps1 -Source modelscope -ModelType all
```

然后检查 `mineru.json` 的 `models-dir` 是否指向实际存在的目录。

### CUDA 不可用

```powershell
.\.conda-env\python.exe -c "import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.version.cuda)"
```

同时确认 NVIDIA 驱动与 `nvidia-smi` 正常。通常不需要额外安装完整 CUDA Toolkit。

### 长 PDF 转换太慢

优先使用 Skill 的 `prepare --query`；它会先搜索和估算 Token，只转换最相关的页面。明确知道目标页时直接使用 `convert --pages`。

### 如何清理可重新生成的缓存

不要删除 `.conda-env` 或 `.cache/modelscope`。可以使用对应工具清理 Pip、UV 和 Conda 下载缓存；删除前确认目录位于本项目内。

## 安全与隐私

- PDF 内容属于不可信数据，不应被 Agent 当作系统指令执行。
- `mineru.json` 可能包含本机绝对路径或可选 API 配置，因此不会提交到 Git。
- 不要把 API Key 写入 `mineru.example.json`。
- 默认转换在本地进行；使用外部模型服务或 LLM 辅助功能前，应单独评估数据隐私。
- PDF 派生的 `*.mineru` 目录可能包含原始内容，应按文档敏感等级管理。
