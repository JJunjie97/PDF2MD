# 本地真实回归集

`data/regression/` 保存用于端到端质量验证、但不一定具有训练许可的真实 PDF 与解析结果。每个源 PDF 必须与同名 `<stem>.pdf2md/` 同级，顶层 Markdown 是公开结果，`raw/` 是复现和审计所需缓存。

## cold-atom-theses

`cold-atom-theses/` 含 21 份冷原子、原子干涉仪相关博士论文及 21 套完整解析结果。它们由原来的 `paper/干涉仪/干涉仪/` 无损迁移而来；迁移后已逐份核对 PDF 大小、SHA-256、manifest、公开 Markdown 和同级输出，缓存 JSON 中的绝对来源路径也已更新到新位置。

这些样本主要用于：

- 目录、图目录、表目录的召回和正文反向匹配；
- 扫描/原生文本、双栏、续页、页码列、OCR 丢章号等版式回归；
- 锚点、反链、无悬空链接和二次发布幂等性。

```powershell
.\runtime\env\python.exe .\scripts\audit-navigation.py `
 .\data\regression\cold-atom-theses --idempotent
```

`sugarbakerThesis-augmented.pdf` 与 `data/downloads/en/theses/atom-interferometry/stanford-sugarbaker-2014-10m-fountain.pdf` 字节相同。前者保留完整端到端解析结果，后者是 `corpus.json` 管理的 canonical 下载项；暂不使用硬链接或删除任一角色，避免破坏跨机器可移植性和既有缓存。

这里的 PDF、图片和 `.pdf2md/` 均被 Git 忽略；README 只记录结构和使用边界。除非某份文档另行出现在 `data/corpus.json` 且明确标记 `training_eligible=true`，否则不得用于训练或发布权重。
