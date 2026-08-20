# 前置区域训练数据

这里仅版本化小型、可审计的来源清单和物理页级标签，不提交 PDF、页面图像、OCR 文本或模型特征。

## 文件

- `sources.json`：候选公开数据集的官方入口、许可边界和预期用途。全部是 `manual-only`，管理脚本不会下载这些大型数据集。
- `annotations.jsonl`：每行一个 `pdf2md.front-page-label.v1` 标签，仅含 `schema`、`document_id`、`source_sha256`、1 基物理页 `page`、`kind`、`status` 和 `reviewer`。
- `navigation-annotations.jsonl`：独立于页面主类别的导航多标签层。每行一个 `pdf2md.front-navigation-label.v1` 标签，在上述字段外只增加 `presence`；`kind` 仅允许 `contents`、`list_of_figures`、`list_of_tables`，`presence` 仅允许 `present` 或 `absent`。
- `front-region-evaluation.json`：由固定 SHA 的人工标签和本地 `content-list-v2` 缓存生成的确定性评测报告，仅保存哈希、页码、标签与决策元数据，不保存页面文本或图像。
- `local/`：被 Git 忽略的候选和人工复核队列。

标签不保存文本或图像。训练或复核时，以 `source_sha256` 将标签连接到 PDF 同级 `.pdf2md/raw/manifest.json` 以及 `raw/cache/content-lists/` 中的本地特征；来源哈希不一致时必须停止，不能把旧标签套到新版 PDF。这样 OCR 文本、版面框和页面图像始终留在本机缓存中。

导航层记录的是人工确认的“该物理页是否存在某类导航块”，不记录文本、边界框、图片或区域数量。每个 `(document_id, source_sha256, page, kind)` 只能出现一次；同页不同 `kind` 可以并存。`absent` 必须显式标注，任何未出现的页/类别组合一律是 `unknown`，不得当作负样本。当前 9 个完整复核页含 27 条导航金标：`contents` 为 4 个正例/5 个负例，图目录为 3 个正例/6 个负例，表目录为 2 个正例/7 个负例。三类都同时具备正负样本；同一页可以继续保留主类别 `abstract`，同时拥有导航类别 `contents=present`。主标签 schema 保持不变，trainer 另行读取导航文件并训练三个相互独立、缺标掩码的 sigmoid 辅助头。

## 状态边界

- `verified`：已人工确认，可以进入训练/评估拆分；`reviewer` 不得以 `auto:` 开头。
- `needs_review`：只能作为候选。规则、PDF outline 和 `inspect.json` 产生的任何结果都保持此状态，不能自动升级为金标。

当前种子覆盖 19 个固定 SHA 文档、37 个物理页标签；其中 11 页来自两份经视觉核验的 CC BY 4.0 冷原子博士论文，5 页来自仅作本地回归的 Harvard 中性原子量子计算博士论文，10 页来自 3 份英文和 2 份中文 ADI datasheet，4 页来自 CC BY 4.0 数学物理论文与量子物理讲义，其余 7 页分别来自 Attention 论文、ESP32 中文技术手册、生态环境部指南、NIST AI RMF、RP2040、TI ADS1115 与 WCH CH32V307。标签覆盖封面/法律页、摘要、致谢、目录、图目录、表目录与正文起点。它仍只是管线与早期校准种子，不足以批准生产模型。

## 命令

```powershell
./runtime/env/python.exe ./scripts/manage-front-training.py validate
./runtime/env/python.exe ./scripts/manage-front-training.py list
./runtime/env/python.exe ./scripts/manage-front-training.py bootstrap
./runtime/env/python.exe ./scripts/manage-front-training.py export-review
./runtime/env/python.exe ./scripts/evaluate-front-regions.py --output ./data/training/front-region-evaluation.json
./runtime/env/python.exe ./scripts/evaluate-front-regions.py --output ./data/training/front-region-evaluation.json --check
```

`validate` 和 `list` 会同时读取页面主标签与导航多标签；需要验证实验文件时可在子命令前传入 `--navigation-annotations <path>`。普通语料要求存在与固定 SHA 一致的本地 `inspect.json`，据此严格校验导航页码上界；`synthetic-front-matter` 在没有 inspect 时，只允许通过受限相对路径、普通文件、固定大小与 SHA-256，并用严格 PDF page tree 读取页数。evaluator 与 trainer 会安全发现语料同级的 `navigation-annotations.jsonl`；显式参数可固定所选文件，同级文件不存在时则保持主标签-only 向后兼容。

`bootstrap` 只读取 `data/corpus.json`、本地 `<pdf>.pdf2md/raw/inspect.json` 的来源哈希、页数、TOC 候选和 outline 标题，并写入 `data/training/local/bootstrap-candidates.jsonl`。`export-review` 将待复核项整理到 `data/training/local/review.json`。两个命令均不联网、不渲染 PDF、不运行 OCR。

`evaluate-front-regions` 只评测 `verified` 物理页，并把页面主类型与页内导航存在性分开报告；两者各自独立选择孤立页和真实连续 selection 上下文，不会拼接不连续缓存。主类型在孤立路径上接受且正确 29/37 页（接受准确率 100%、覆盖率/总体准确率 78.38%），在上下文路径上接受且正确 32/35 页（100%、91.43%）。导航层的 27 条显式判断在孤立路径上正确 24 条、拒识 3 条（总体/平衡准确率 88.89%）；可形成连续上下文的 24 条判断中正确 21 条、拒识 3 条（87.50%）。预测严格取生产投影，主类型未接受时导航也记为拒识；原始候选块不能算成功。当前每类样本尚未满足至少 20 个正例、20 个负例且正负例各覆盖 5 份文档的发布门槛，因此 `release_gate_eligible=false`，这些数据只用于规则回归和离线实验。

公开数据集许可必须在实际下载和训练前重新核对：DocLayNet 适合版面预训练；GROTOAP2 是含噪弱监督；PMC 必须逐篇检查许可并使用官方获取接口；CompHRDoc 的代码/标注许可不能替代其底层 HRDoc-Hard 图像许可。

## 合成基线与训练

项目自带的生成器只产生项目原创 CC0 前置页，用于验证特征、拆分、校准和模型加载链路：

```powershell
.\runtime\env\python.exe .\scripts\generate-front-region-synthetic.py
.\pdf2md.cmd batch .\data\training\generated `
  --profile balanced --method auto --lang ch --force --load-model --fail-fast
.\runtime\env\python.exe .\scripts\manage-front-training.py `
  --corpus .\data\training\generated\corpus.json `
  --annotations .\data\training\generated\annotations.jsonl `
  --navigation-annotations .\data\training\generated\navigation-annotations.jsonl validate
.\runtime\env\python.exe .\scripts\train-front-region-model.py `
  --corpus .\data\training\generated\provenance.json `
  --annotations .\data\training\generated\annotations.jsonl `
  --navigation-annotations .\data\training\generated\navigation-annotations.jsonl `
  --output .\models\front-region\candidate `
  --allow-small --seed 7
```

训练器本身只读取已经存在且与当前 PDF SHA 匹配的 `content-list-v2` 缓存，不会启动 OCR；PDF 重新生成后必须先用上面的常驻 `batch --force` 刷新缓存。主分类候选写为 `layout.json`/`text.json`，有显式导航标签时另写 `navigation-layout.npz`/`navigation-text.npz`。导航 NPZ 只供离线实验，生产 loader 不读取；policy 始终标记为实验性、`approved_for_auto_action=false`，各导航头的 `auto_action_gate=false`。小型合成集只证明训练与推理流水线可运行，不能证明对真实书籍、论文和手册已经达到发布精度。

训练、校准和测试按整份 PDF 分组；主多类头会拒绝空集合以及训练集中完全未见的类别。导航某一头支持不足时不伪造结论，而是保持 `untrained`、阈值 1.0、自动动作门关闭。发布前还必须按来源/模板族去重并拆分，在独立人工测试集上验证破坏性类别的 precision、coverage-risk、ECE/Brier、错误前置内容删除数和正文边界误差。核心只接受人工批准且主模型 SHA-256 与 policy 完全一致的权重，修改模型字节会立即使批准失效。
