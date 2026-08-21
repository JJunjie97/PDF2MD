# PDF2MD 识别模型、匹配代码与执行流程

本文是 PDF2MD 的实现级技术参考，回答四个问题：

1. 一份 PDF 从输入到 Markdown，模型和 Python 代码按什么顺序执行；
2. MinerU 内部使用了哪些模型，各模型负责什么；
3. 哪些判断不是模型，而是正则、几何、序列对齐和置信门控；
4. 当前工作目录里哪些能力已经用于生产，哪些仍只是离线候选。

面向普通使用的流程、CLI、缓存和目录结构见 [根 README](../README.md#架构与端到端-ocr-流程)；前置页二级分类的训练、校准和发布门禁见 [前置区域级联](front-region-cascade.md)。

> 版本边界：本文按项目固定的 MinerU `3.4.4` 和 2026-08-21 本机环境核对。模型缓存不会随 Git 仓库提交，其他机器是否已经下载相同权重，应以其运行配置和模型目录为准。

## 1. 先区分三类“识别”

整个系统并不是一个大模型从 PDF 一步生成最终 Markdown，而是三层协作：

| 层 | 主要实现 | 是否神经模型 | 负责什么 | 不负责什么 |
|---|---|---:|---|---|
| MinerU 感知层 | PP-DocLayoutV2、PaddleOCR、UniMERNet、表格模型、MinerU VLM | 是 | 版面框、阅读顺序、文字、公式、表格、图像语义和中间块 | 不决定最终目录链接是否安全 |
| PDF2MD 适配与页面理解 | `pdf2md_engine.py`、`pdf2md_front_regions.py`、`pdf2md_region_*.py` | 混合 | 保留布局元数据、修复字符；用规则和可选小模型判断前置页类型 | 不凭页级类型创建正文标题 |
| PDF2MD 确定性发布 | `pdf2md_core.py`、`pdf2md_frontmatter.py`、`pdf2md_toc.py`、`pdf2md_markdown.py` | 否 | 缓存、验证、原生文本恢复、目录范围所有权、正文反向匹配、锚点和原子写出 | 不在证据不足时猜链接或删除正文 |

这里的关键原则是：

- 模型负责产生文字、布局和候选结构；
- Python 代码负责验证候选、融合多路证据和决定是否自动修改 Markdown；
- “拒识”是正常结果。证据不足时保留原文，比强行分类或链接更安全。

## 2. 总体执行拓扑

~~~mermaid
flowchart TD
    subgraph Entry["入口与生命周期"]
        E1["CLI / GUI / Agent Skill"]
        E2["pdf2md_cli.py<br/>参数与命令分派"]
        E3["pdf2md_core.py<br/>页段、manifest、缓存、服务生命周期"]
        E1 --> E2 --> E3
    end

    subgraph MinerU["MinerU 模型推理"]
        M0{"selection 缓存命中？"}
        M1["OCRService<br/>本地 FastAPI"]
        M2{"profile"}
        M3["Pipeline<br/>PP-DocLayoutV2 + OCR + 公式 + 表格"]
        M4["Hybrid medium / high<br/>PP-DocLayoutV2 + MinerU VLM"]
        M5["middle_json finalize"]
        M6["Markdown + images + content-list-v2"]
        M0 -->|未命中| M1 --> M2
        M2 -->|fast| M3 --> M5
        M2 -->|balanced / accurate| M4 --> M5
        M5 --> M6
    end

    subgraph PythonDecision["PDF2MD Python 决策"]
        P1["ZIP / UTF-8 / U+FFFD / 路径校验"]
        P2["selection 缓存"]
        P3["HTML 表格 → GFM<br/>引用图片重建"]
        P4["前置页级联<br/>规则 → 可选布局头 → 可选文本头 → 拒识"]
        P5["pypdf 原生前置文本<br/>断行、双栏、页码列"]
        P6["目录证据融合<br/>Markdown + native + structured"]
        P7["正文反向匹配<br/>编号、类型、方向、唯一性、margin"]
        P8["锚点、反链和 owned range 重渲染"]
        P9["临时 Markdown 原子替换"]
        P1 --> P2 --> P3 --> P4 --> P5 --> P6 --> P7 --> P8 --> P9
    end

    E3 --> M0
    M0 -->|命中| P3
    M6 --> P1
~~~

这张图中的 `content-list-v2` 很重要：它是同一次 MinerU 推理留下的轻量结构证据，不是第二次 OCR。PDF2MD 从中读取物理页、块类型、边界框、阅读顺序、布局标签与分数、INDEX 条目；最终公开阅读入口仍只有顶层 Markdown 和实际引用的图片。

## 3. 后端选择与调用条件

`src/pdf2md_core.py` 中的 profile 映射为：

| profile | MinerU backend | effort | 图像/图表语义分析 | 典型用途 |
|---|---|---:|---:|---|
| `fast` | `pipeline` | medium | 关闭 | 文本层较好、快速检查 |
| `balanced` | `hybrid-engine` | medium | 关闭 | 默认；论文、手册、datasheet |
| `accurate` | `hybrid-engine` | high | 开启 | 复杂版面或图表语义重要 |

公式和表格识别请求始终开启。“关闭图像分析”只表示不额外生成图像/图表语义描述，不表示跳过表格抽取。

### `auto`、`txt`、`ocr` 不是三个模型

它们控制文字来源：

- `auto`：由 MinerU 的 Python 规则抽样最多 10 页，检查文字量、页面长宽比、Unicode/ToUnicode/CID 字体、控制字符、私用区字符、异常标点、图片覆盖率等，决定是否启用 OCR。它不是神经网络分类器。
- `txt`：强制 `ocr_enable=False`。OCR detector 仍可能用于文字行几何，但正文主要来自 PDFium 原生文本。
- `ocr`：强制 `ocr_enable=True`。
  - Pipeline 中，PaddleOCR detector + recognizer 产生正文文字；
  - Hybrid 中，主要正文和公式由 MinerU VLM 产生，PaddleOCR detector 主要提供行框与段落合并几何提示，不再覆盖 VLM 正文。

## 4. MinerU 内部模型清单

本机固定环境为 MinerU 3.4.4、PyTorch 2.8.0+cu128、LMDeploy 0.11.1、Transformers 4.57.6 和 ONNX Runtime 1.28.0。以下是当前可由运行路径选择的核心模型。

### 4.1 PP-DocLayoutV2：布局检测与阅读顺序

相对于本机 PDF-Extract-Kit bundle 根目录，本模型位于 `models/Layout/PP-DocLayoutV2`；当前 bundle 根是 `models/modelscope/models/OpenDataLab--PDF-Extract-Kit-1.0/snapshots/master/`。权重配置使用 HGNetV2-L 主干、RT-DETR 风格目标检测器，并带 reading-order head。这些模型缓存被 Git 忽略，不应把该相对路径理解成仓库根目录下的同名文件。

它输出 25 类区域，包括：

- 文档标题、段落标题、正文；
- 目录内容块；
- 表格、图像、图表及其题注、脚注；
- 行内/行间公式与公式编号；
- 页眉、页脚、页码；
- 其他版面类别。

需要特别注意：PP-DocLayoutV2 的 `content` 标签表示“一个目录状布局块”，不是“整页一定是目录”的最终页面分类。PDF2MD 仍会结合标题、条目密度、页码形态、相邻页和正文边界做二次判断。

### 4.2 PaddleOCR：检测与文字识别

MinerU 包装器为 `PytorchPaddleOCR`。当前默认语言 `ch` 使用：

| 组件 | 当前权重 |
|---|---|
| 文字检测 | `ch_PP-OCRv6_small_det_infer.safetensors` |
| 文字识别 | `ch_PP-OCRv6_small_rec_infer.safetensors` |
| 字典 | `ppocrv6_dict.txt` |

`ch_server` 才会改用 medium 中文识别权重。当前 MinerU 的公开入口会把 `en` 归一化到 `ch` 模型配置；language 参数主要选择或归一化 OCR 权重与字典，不会切换 PP-DocLayoutV2 或 MinerU VLM。

### 4.3 公式模型

当前默认公式识别器是：

- UniMERNet small；
- 相对于同一 PDF-Extract-Kit bundle 根目录的 `models/MFR/unimernet_hf_small_2503`。

本机也下载了 PP-FormulaNet_plus-M，但只有设置 `MINERU_FORMULA_CH_SUPPORT=true` 时才切换过去。模型“已经下载”不等于当前分支“正在执行”。

### 4.4 Pipeline 表格模型

`fast` 的 Pipeline 表格链包括：

1. 表格方向判断：不是独立方向模型。代码先用 OCR 框几何筛选，再比较 0°、90°、270° OCR 识别置信度；
2. PP-LCNet `PP-LCNet_x1_0_table_cls.onnx`：有线/无线表格分类；
3. SLANet Plus `slanet-plus.onnx`：先处理无线表格结构；
4. U-Net Structure `unet.onnx`：处理有线表格，以及无线分类置信度低于 0.9 的表格。

Hybrid 不运行这套 SLANet/U-Net 内容识别链；Hybrid 的表格内容由 VLM 识别。`balanced` 的 medium 路径仍会借用 OCR 评分规则判断表格方向。

### 4.5 MinerU VLM

当前本地模型为 `OpenDataLab/MinerU2.5-Pro-2605-1.2B`。其配置声明架构为 `Qwen2VLForConditionalGeneration`，模型类型为 `qwen2_vl`。

它按区域使用不同任务提示：

- `Layout Detection`；
- `Text Recognition`；
- `Table Recognition`；
- `Formula Recognition`；
- `Image Analysis`。

Windows + CUDA 下，当前自动引擎解析链为 `hybrid-engine → engine → lmdeploy-engine → TurboMind`。

项目配置模板中还有一个标题辅助 LLM 配置，但 `title_aided.enable=false`，所以当前生产流程不会调用其中配置的 Qwen 服务。

## 5. MinerU Pipeline 的内部流程

`fast` 走 Pipeline。默认把 PDF 页渲染为约 200 DPI，长边限制在 3500 像素内。

~~~mermaid
flowchart TD
    A["PDF 页渲染"]
    B["PP-DocLayoutV2<br/>区域检测 + reading order"]
    C["UniMERNet<br/>公式识别"]
    D["切分正文、表格、公式区域"]
    E["表格方向规则<br/>比较 OCR 评分"]
    F["PP-LCNet<br/>有线 / 无线分类"]
    G["表内 PaddleOCR"]
    H["SLANet Plus<br/>无线结构"]
    T{"wired<br/>或 wireless score &lt; 0.9？"}
    I["U-Net Structure<br/>有线 / 低置信无线结构"]
    J["正文 PaddleOCR detector"]
    K{"ocr_enable"}
    L["PaddleOCR recognizer"]
    M["PDFium 原生文本"]
    N["MagicModel<br/>span 与 block 几何匹配"]
    O["题注归属、段落合并、跨页表格、标题分级"]
    P["finalized middle_json"]
    Q["Markdown / content-list-v2"]

    A --> B --> C --> D
    D --> E --> F --> G --> H --> T
    T -->|是| I --> N
    T -->|否| N
    D --> J --> K
    K -->|true| L --> N
    K -->|false| M --> N
    N --> O --> P --> Q
~~~

MinerU 的 span 与布局块不是靠标题字符串匹配。`SpanBlockMatcher` 计算文字 span 落入布局框的面积比例，默认超过 0.5 才归入该块。图、表、图表与其题注/脚注之间的归属，继续由 Python 几何关系和 reading order 决定。

## 6. MinerU Hybrid 的内部流程

### 6.1 `balanced`：Hybrid medium

medium 先运行 PP-DocLayoutV2，再把检测结果映射为 VLM 的外部 blocks；VLM 不再自行做整页布局检测，而是按这些块裁图识别。

~~~mermaid
flowchart TD
    A["PDF 页图"]
    F["先解析 auto / txt / ocr"]
    B["PP-DocLayoutV2"]
    C["映射成 VLM 外部 blocks"]
    D["表格方向 OCR 评分"]
    G["VLM：正文 / 标题 / 表格 / 公式"]
    H["PaddleOCR det：行框 sidecar"]
    I["VLM 跳过文字类 block"]
    J["UniMERNet：行内公式"]
    K["PaddleOCR det：文字行框"]
    L["PDFium：原生文字"]
    M["Hybrid MagicModel 融合"]
    N["段落、跨页表格、标题分级"]
    O["finalized middle_json"]

    A --> F --> B --> C --> D
    D -->|已解析为 OCR| G --> H --> M
    D -->|已解析为 txt| I --> J --> K --> L --> M
    M --> N --> O
~~~

medium 强制关闭额外 image analysis。这里关闭的是图片语义描述，布局、文字、公式和表格仍会处理。

### 6.2 `accurate`：Hybrid high

high 仍先运行 PP-DocLayoutV2，随后还让 VLM 自己做完整 `Layout Detection`，再逐区域识别：

1. PP-DocLayoutV2 提供行内公式框、OCR 几何 sidecar、`doc_title` 拆分等辅助信息；
2. VLM 对整页做布局检测；
3. VLM 按自己的布局逐块执行文字、表格、公式和可选图像分析；
4. `txt` 分支再融合 UniMERNet、PaddleOCR detector 和 PDFium 原生文字；
5. `ocr` 分支的主要文字/公式来自 VLM，PaddleOCR detector 提供行级几何；
6. Hybrid MagicModel 统一块结构，再生成 `middle_json`。

因此，“accurate/high 完全不用 PP-DocLayoutV2”是不正确的；当前源码仍会执行它。

## 7. PDF2MD 对 MinerU 的适配补丁

`src/pdf2md_engine.py` 在启动 MinerU FastAPI 之前依次安装：

1. `install_hybrid_index_patch()`；
2. `install_span_repair_patch()`；
3. `install_preload_route()`；
4. 再进入 MinerU `fast_api.main()`。

这些是 PDF2MD 的兼容和证据保真层，不是新的 OCR 模型。

### 7.1 INDEX 与布局元数据保真

MinerU 的中间转换会重建字典，Hybrid 还可能把布局 `INDEX` 降为普通 `TEXT/paragraph`。适配层保存并恢复：

- 私有 `_pdf2md.layout` 中的原始 layout label、detector score、index/order；
- content-list 标准公开字段中的 bbox；INDEX 转换时将原 bbox 复制到新结果；
- INDEX 的逐行 `list_items`。

因此 bbox 不属于私有布局字典；evidence 层只是在需要时把标准 bbox 作为布局 bbox fallback。Hybrid medium 回配私有布局元数据时使用“映射后的块类型 + 归一化 bbox 精确相等”，防止跳过无效检测后把分数错配给相邻块。

### 7.2 U+FFFD 字符修复

当 PDFium 得到 `�` 时，不立即整页二次 OCR，而是：

1. 用 pypdf 按字体重新提取文字；
2. 清理字体子集前缀及 `Identity-H/Identity-V` 后缀；
3. 对单字符执行 NFKC；
4. 用 Python `difflib.SequenceMatcher` 对齐同字体字符序列；
5. 只接受等长 replacement 区间；
6. 区间中原字符必须是 `U+FFFD` 或与候选完全相同；
7. 只填仍为 `U+FFFD` 的槽位；
8. 未修复 span 继续交给 MinerU 原有的局部 post-OCR；
9. 最终 Markdown 仍含 `U+FFFD` 时，Core 拒绝把该 selection 写入可复用缓存。

这条路径是“字体映射修复优先，残留 span 局部 OCR”，不是反复加载模型重跑整页。

## 8. 服务、缓存与模型加载时序

~~~mermaid
sequenceDiagram
    participant U as CLI / GUI / Agent
    participant C as pdf2md_core
    participant S as OCRService
    participant E as pdf2md_engine
    participant M as MinerU
    participant P as Publish

    U->>C: PDF + pages + profile + method + language
    C->>C: 校验路径、页数、SHA、manifest、selection key
    alt 普通单文件且全部 cache hit
        C->>P: 直接重跑最新后处理
    else 存在 cache miss
        C->>S: 启动本地 API
        S->>E: 安装适配补丁
        E->>M: 懒加载所需模型
        loop 每个未命中页段
            C->>S: 上传 PDF 和物理页段
            S->>M: 执行 Pipeline / Hybrid
            M-->>S: Markdown / images / content-list-v2
            S-->>C: 返回结果 ZIP
            C->>C: 安全解压、UTF-8、U+FFFD、引用图片校验
            C->>C: 写 selection 缓存
        end
        opt 当前调用拥有 OCRService
            C->>S: 停止服务并保存本次日志
        end
        C->>P: 合并全部页段并发布
    end
    P-->>U: 顶层 Markdown + images + raw
~~~

生命周期差异：

- 普通单文件：首个 miss 才启动服务；同一次命令的页段共用模型；结束后关闭；
- `batch`：整个队列共用一个固定 profile/method/language 会话；默认懒加载，`--load-model` 可先预热；
- `preload/session`：会话开始即预热，在多次转换间保持 GPU 模型驻留，`exit`、EOF 或 `Ctrl+C` 后清理；
- GUI 当前每个任务启动一个 CLI 子进程，不跨任务保持模型；大量反复测试应使用 `batch` 或 `preload/session`。

## 9. 前置页的二级识别

MinerU 布局模型只给块，PDF2MD 还要回答“这一物理页是封面、摘要、目录、图目录、表目录、正文起点，还是无法确定”。入口为：

- `pdf2md_front_regions.classify_content_list_v2()`：规则证据；
- `pdf2md_region_evidence.py`：布局和文本特征；
- `pdf2md_region_models.py`：严格线性模型加载；
- `pdf2md_region_cascade.classify_front_regions_v2()`：级联；
- `project_front_regions_v1()`：只投影允许下游使用的结果。

~~~mermaid
flowchart TD
    A["content-list-v2 页面"]
    B["提取 text / block / bbox / label / score / order"]
    C["高置信 Python 规则"]
    D{"规则锁定？"}
    E["接受主类型和页内导航证据"]
    F{"有批准的布局模型<br/>且布局证据有效？"}
    G["布局线性头"]
    H{"概率、类别阈值、margin、OOD 均通过？"}
    I["字符 2–5 gram 文本线性头"]
    J{"通过且不与布局 top-1 冲突？"}
    K["拒识"]
    L["相邻页序列约束与正文边界"]
    M["V1 安全投影"]

    A --> B --> C --> D
    D -->|是| E
    D -->|否| F
    F -->|是| G --> H
    F -->|否| I
    H -->|是| E
    H -->|否| I
    I --> J
    J -->|是| E
    J -->|否| K
    E --> L --> M
    K --> M
~~~

### 9.1 高置信 Python 规则

规则优先于小模型，主要证据包括：

- title/paragraph block 的完整中英文标题；
- Chapter/Part/“第 N 章”与相邻标题的严格组合；
- INDEX 密度、带页码条目比例、leader 长度；
- 紧邻上一导航页的 continuation；
- 正文起点与 datasheet 的特殊前置窗口；
- 超长纯 leader debris、脚注/旁注和普通 prose 的否决条件；
- 同页 `Abstract + Contents` 等混合页面的独立导航块。

强 INDEX 需要至少 6 个结构条目、至少 4 条有效导航行，且有效比例至少 60%。规则分数是“证据强度”，不是经过校准的概率。

芯片手册的特殊回看只从物理第 1 页开始：首个 datasheet-like body 候选必须位于前 3 页，明确导航必须在前 8 页，导航后最多再找 12 页确定真正正文边界。

### 9.2 可选布局线性头

输入不再跑新的视觉骨干，而是复用 PP-DocLayoutV2 已经产生的框：

- 物理页号、块数、文字长度的 `log1p`；
- 有效布局框比例；
- score 的均值、最小值、最大值；
- 按 label 聚合的 count、score、面积、中心点；
- 4×4 页面网格位置。

模型为小型线性 softmax 头：

~~~text
logit[class] = bias[class] + sum(weight[class, feature] * feature)
probability = softmax(logit / temperature)
~~~

它还会检查已知特征比例、L1 范围、维度和有限数；异常、缺布局证据或 OOD 不会用 bias 猜测，而是转入文本阶段。

### 9.3 可选文本线性头

只有规则和布局阶段没有接受页面时才调用。文本特征为：

- 每页最多 32,768 字符；
- 英文/数字 token 和单个汉字；
- 最多 8,192 token；
- 字符 2–5 gram；
- BLAKE2b 稳定散列到 512 维；
- signed hashing trick 与 L2 归一化；
- 字符数和 token 数的 `log1p`。

它不是 Transformer 或 LLM。文本 top-1 若与前一阶段未被接受的布局 top-1 冲突，最终拒识。

源码在没有传入 policy 时的 fallback 阶段阈值为 layout 0.86、text 0.82，margin 分别为 0.20、0.15；fallback 类别表对目录、图目录、表目录和正文边界还使用 0.92–0.93 的更高阈值。批准后的生产 `v1/policy.json` 可以覆盖这些 fallback。当前未批准 candidate policy 的 layout/text 和类别阈值均为 0.98，但它不会进入生产推理。

### 9.4 当前生产状态

当前工作目录只有 `models/front-region/candidate/`，其 policy 为：

- `experimental=true`；
- `approved_for_auto_action=false`。

生产目录 `models/front-region/v1/` 不存在，所以当前正式前置页级联实际为：

~~~mermaid
flowchart LR
    R["高置信规则"] --> A{"能证明？"}
    A -->|是| K["接受"]
    A -->|否| X["拒识"]
~~~

这只表示“PDF2MD 自有前置页小模型尚未上线”，不表示 MinerU、PP-DocLayoutV2、OCR、公式或 VLM 没有运行。

要让 layout/text JSON 进入生产，`v1/policy.json` 和两个 artifact 必须同时通过 schema、批准状态、experimental/experiment_only、SHA-256、training eligibility 和 redistribution 门禁。训练器生成的 `navigation-layout.npz` / `navigation-text.npz` 仍是离线评测产物，生产 loader 不读取。

## 10. 原生 PDF 文本如何辅助目录

`src/pdf2md_frontmatter.py` 使用 pypdf，从选中的连续物理前置页读取原生文字层。它不是 OCR 模型。

主要步骤：

1. 优先 `extraction_mode="layout"`，失败时退回普通 `extract_text()`；
2. 识别 Contents、List of Figures、List of Tables 及严格中英文别名；
3. NFKC 并清理 Markdown/HTML 行内标记；
4. 解析点线、破折号或空白后的数字/罗马页码；
5. 合并被拆开的页码和跨行标题；
6. 只在左行以连字符结尾、右行以小写开头时修复英文断词；
7. 用列宽一致性和完整条目边界恢复多栏阅读顺序；
8. 仅在编号严格递增、页码单调且差值受限时拆开被布局文本提取合并的图表项；
9. 将 ISO/IEC/IEEE/ASTM/DIN/EN 后的数字视为标准号，不误当页码；
10. 按类型、规范化标题和页码去重。

目录延续页至少要解析 3 项，且至少 60% 带页码。原生结果只有在至少 50% 条目带页码、条目数不少于 Markdown 结果时，才可以整体替换当前条目；否则直接保留 Markdown 解析结果，未达门槛的 native 条目不再参与后续融合。独立的 structured evidence 仍可按自身门槛融合。

原生缓存绑定源 PDF 解析路径、文件大小、`mtime_ns`、`max_pages` 和精确连续物理页集合；它目前不计算源 SHA。非连续选择、未选中的导航页或缓存 provenance 不一致都会 fail closed。

## 11. 目录、图目录和表目录如何从正文反向匹配

主入口是 `src/pdf2md_toc.py::enhance_document_navigation()`。

它不是盲目枚举全文所有标题，也不是让原目录文字决定最终目标。三路来源的角色是：

| 来源 | 标记 | 作用 |
|---|---|---|
| MinerU Markdown | 普通 entry | 当前可见目录和局部说明 |
| pypdf 原生文本 | `native=True` | 修复 OCR 漏行、断行、双栏、页码列 |
| MinerU INDEX/content-list | `structured=True` | 提供物理页和条目块边界 |

三路条目用 LCS/SCS 风格的保序归并处理内部缺项，不用集合相加打乱顺序。冲突时证据优先级为 `native > structured > Markdown`。

### 11.1 导航增强的严格执行顺序

| 顺序 | 函数 | 作用 |
|---:|---|---|
| 1 | `_strip_generated_navigation` | 清理 owned 锚点、紧邻 target 锚点且明确标为 generated 的题注 heading，以及与这些标记相邻的反链；不删除普通旧标题 |
| 2 | `_structured_navigation_entries` | 验证并读取 V1 结构化目录块 |
| 3 | `_section_ranges` | 在代码块以外定位现有目录类 Markdown 标题与初始范围 |
| 4 | `_populate_entries` | 解析 Markdown，并融合 native/structured 候选；内部第一次调用 `_structured_navigation_runs` 做分区 |
| 5 | `_extend_native_section_ranges` | 用有序原生证据扩展被截断的旧目录尾 |
| 6 | `_extend_body_backed_section_ranges` | 用块级结构和正文存在性确认尾部所有权 |
| 7 | `_refresh_section_entries` | 在最终 owned range 上重新解析非原生条目 |
| 8 | `_structured_navigation_runs`（第二次） | 再按物理页和明确标题切分双语/多段结构 run，供重复 section 折叠 |
| 9 | `_collapse_repeated_structured_sections` | 折叠同一目录跨页重复页眉 |
| 10 | `_collect_caption_targets` | 收集严格 Figure/Table 题注 |
| 11 | `_rebuild_corrupt_caption_lists` | 仅在严格残骸条件下从题注重建图表目录 |
| 12 | `_assign_section_anchors` | 分配目录 section 锚点 |
| 13 | `_collect_heading_targets` | 收集显式 `#..######` 正文标题 |
| 14 | `_collect_navigation_targets` | 组织可匹配目标 |
| 15 | `_match_contents` | 正文目录按方向和游标匹配标题 |
| 16 | `_match_captions` | 图/表目录按类型与编号匹配题注 |
| 17 | `_assign_target_anchors` | 分配正文目标锚点 |
| 18 | `_render_document` | 在内存文本中替换 owned 目录范围，插入链接和反链；文件级 `.md.tmp → replace` 由 Core 随后执行 |

### 11.2 目录范围所有权与条目匹配是两件事

先证明“这段原始文本属于目录”，再决定“其中每一条能否链接”。无法链接的条目仍可作为纯文本 bullet 保留，不会因为目标歧义而把原 OCR 尾遗留在新目录后。

可疑尾部的一般所有权门槛是：

- 至少 3 条结构记录；
- 4 条以内必须全部得到来源或正文支持；
- 5 条以上至少 3 条有支持，支持率至少 75%；
- 不支持项只能夹在两个支持项之间；
- 代码围栏、缩进代码、普通 Markdown 正文标题和无法闭合的 prose 是硬边界。

另有更窄的点线模式：每一行都必须是强 leader + 纯数字页码，页码单调，并完整结束在下一个导航标题之前。

这些门槛只用于扩展一个已经识别出的导航段，不是把任意“像目录”的正文自动删掉。

### 11.3 正文标题匹配

`_heading_score()` 的核心逻辑可概括为：

~~~python
def conceptual_heading_score(entry, target):
    entry_key = canonical_identifier(entry)
    target_key = canonical_identifier(target)

    # 实现还会把可确认的单前缀 entry 映射到 target_key。
    if entry_key and target_key and entry_key != target_key:
        return -1.0

    if normalized(entry.title) == normalized(target.title):
        if entry_key != target_key and normalized(entry.title) not in UNNUMBERED_EXACT:
            return -1.0
        return 1.00

    if bool(entry_key) != bool(target_key):
        return -1.0

    if normalized_body(entry.title) == normalized_body(target.title):
        if normalized_prefix(entry.title) and normalized_prefix(target.title):
            return 0.99
        return 0.95

    if (
        entry_key
        and entry_key == target_key
        and not normalized_body(entry.title)
        and normalized_body(target.title)
        and CONTAINER_ONLY_RE.fullmatch(strip_inline_markdown(entry.title))
    ):
        return 0.98

    similarity = SequenceMatcher(
        None,
        normalized_body(entry.title),
        normalized_body(target.title),
    ).ratio()
    minimum = 0.84 if same_reliable_prefix(entry, target) else 0.94
    return similarity if similarity >= minimum else -1.0
~~~

实际选择还同时要求：

- 有编号的条目与标题必须同号；
- 普通图目录/表目录的题注 entry 与 target 必须同类型，combined 图表目录明确允许 Figure 和 Table 两类；Contents 使用标题/navigation target 的另一套匹配；
- 先判断目录位于正文标题之前还是之后，再按该方向使用单调 cursor；
- 同一正文目标只能使用一次；
- 第一候选和第二候选的分差至少 0.08；
- “唯一”只在当前方向、cursor 范围和尚未使用的候选集合内成立，不要求全文绝对唯一。

目录打印页码不直接决定 Markdown 中的目标位置。页码主要帮助解析、去重和判断目录尾是否可信；真正链接依靠编号、标题文本、方向和正文顺序。

### 11.4 图表题注匹配

图目录/表目录采用更严格的类型隔离：

1. Figure 只能匹配 Figure，Table 只能匹配 Table；
2. 规范化编号必须完全一致；
3. 标题存在前缀包含关系时得分 1.0，否则使用 `SequenceMatcher`；
4. 同编号有多个候选时，第一名至少 0.90 且领先第二名至少 0.08；
5. 同标题并列只有存在明确 `continued` 证据才接受。

损坏的图/表目录只有在已经识别到相应导航 section、原段是超长 leader debris、且没有可信 native/structured 条目时，才会从正文严格题注反建。没有图/表导航标题时不会凭空创建整段列表。

## 12. 其他确定性 Python 处理

### 12.1 HTML 表格转 GFM

`src/pdf2md_markdown.py` 使用标准库 `HTMLParser`，不是模型：

- `rowspan/colspan` 最大 100；
- 展开合并单元格；
- 优先选择含 `th` 的行作表头；
- 否则选第一个至少两个非空单元格、且无 colspan 的行；
- 空表头命名为 `Column N`；
- 单列表格或解析异常时保留原 HTML。

图片筛选、编号和发布属于 `pdf2md_core.py`，不是 markdown 模块的职责。

### 12.2 安全接收与发布

Core 在模型结果进入正式输出前还会执行：

- 流式 ZIP 下载；
- 解压路径 containment；
- 严格 UTF-8；
- `U+FFFD` 拒绝；
- Markdown 图片引用与缓存图片存在性；
- selection provenance 与任务键校验；
- 物理页段排序、合并与边界标记；
- 只复制 Markdown 实际引用图片；
- 顶层 Markdown 先写 `.md.tmp` 再原子替换。

公开 `images/` 会在发布时由缓存重建，它与 Markdown 替换不是跨目录事务；强杀中断后可重跑，命中的 OCR 缓存会重新完成发布。

## 13. 不同 profile 下“可能运行”的模型

下表表示后端核心与按内容触发的组件，不表示每页都会命中每类区域。

| 组件 | fast / Pipeline | balanced / Hybrid medium | accurate / Hybrid high |
|---|:---:|:---:|:---:|
| PP-DocLayoutV2 | 是 | 是 | 是 |
| PaddleOCR detector | 是 | 是，主要作几何 sidecar | 是，主要作几何 sidecar |
| PaddleOCR recognizer | OCR 模式正文；表内文字/方向、印章和残留 span 局部 post-OCR 按内容调用 | medium 表格方向评分；txt 或 auto→txt 时可做残留 span 局部 post-OCR，不覆盖 VLM 正文 | txt 或 auto→txt 时可做残留 span 局部 post-OCR；OCR 分支正文由 VLM 负责，只保留 detector sidecar |
| UniMERNet | 公式区域 | txt 分支公式补充 | txt 分支公式补充 |
| PP-LCNet + SLANet + U-Net | 表格区域 | 否 | 否 |
| MinerU VLM | 否 | 按 PP 外部 block 识别 | 自做布局后逐块识别 |
| VLM Image Analysis | 否 | 关闭 | 开启 |
| PDF2MD 前置 layout/text 小模型 | 仅批准后 | 仅批准后 | 仅批准后 |
| PDF2MD 高置信规则与目录匹配 | 是 | 是 | 是 |

## 14. 当前能力边界

1. 当前前置页生产是 rules-only；candidate 的 layout/text JSON 尚未批准，而 navigation NPZ 是生产 loader 根本不读取的离线训练/评测产物。
2. 前置区域分类不等于删除页面。封面、版权、摘要等类型主要用于边界和评测；下游只消费已接受的导航结构证据。
3. 正文反演用于纠正、补齐和链接已有导航段，不会在导航标题完全缺失时从所有正文标题凭空生成 Contents。
4. 多个不连续 selection 不跨缺页推断前置区域，也不启用连续页原生目录恢复。
5. MinerU 的 `content` 块、PDF outline、原目录 OCR、pypdf 文本和正文标题都是不同证据，任何单一路径都不是无条件真值。
6. 图表目录的条目顺序可来自前置目录，但最终链接目标必须是正文显式题注或受严格约束生成的题注 heading。
7. 低置信、低 margin、模型冲突、OOD、非有限数、重复目标或正文方向不一致都会拒绝自动链接。
8. 模型已经缓存不等于每次运行都会加载；缓存命中、profile、method、内容类型和会话模式共同决定实际调用。

## 15. 源码入口索引

| 主题 | 源码 |
|---|---|
| CLI 参数与命令分派 | [`src/pdf2md_cli.py`](../src/pdf2md_cli.py) |
| 缓存、OCR 服务、发布与生产模型门禁 | [`src/pdf2md_core.py`](../src/pdf2md_core.py) |
| MinerU 补丁、元数据保真与预热 | [`src/pdf2md_engine.py`](../src/pdf2md_engine.py) |
| HTML 表格转 GFM | [`src/pdf2md_markdown.py`](../src/pdf2md_markdown.py) |
| 原生 PDF 前置文本 | [`src/pdf2md_frontmatter.py`](../src/pdf2md_frontmatter.py) |
| 前置页高置信规则 | [`src/pdf2md_front_regions.py`](../src/pdf2md_front_regions.py) |
| 布局和文本特征 | [`src/pdf2md_region_evidence.py`](../src/pdf2md_region_evidence.py) |
| 线性模型加载和 OOD 检查 | [`src/pdf2md_region_models.py`](../src/pdf2md_region_models.py) |
| 规则→布局→文本→拒识 | [`src/pdf2md_region_cascade.py`](../src/pdf2md_region_cascade.py) |
| 目录融合、正文反演、锚点和反链 | [`src/pdf2md_toc.py`](../src/pdf2md_toc.py) |
| 生产配置字段示例 | [`config/pdf2md.example.json`](../config/pdf2md.example.json) |
| 前置级联训练与门禁细节 | [`docs/front-region-cascade.md`](front-region-cascade.md) |
| 下载、评测和训练工具索引 | [`scripts/README.md`](../scripts/README.md) |

实现细节发生变化时，本文应与相应源码和无 GPU 回归测试一起更新；本地模型目录只用于确认当前机器状态，不能替代可提交的配置、策略和哈希门禁。
