# 本地示例

这个目录保存用户原有、已由 `data/corpus.json` 固定大小和 SHA-256 的本地 PDF。它们不是自动下载语料，也不因位于 `data/` 而获得再分发或训练许可。

当前三份样本分别覆盖：

- RP2350 超长 datasheet：多页目录、深层章节、寄存器表和长文档性能；
- RP2350 硬件设计指南：短手册与单页目录快速回归；
- 中文 LabVIEW/FPGA 水印教材：超长、低文本密度、坏书签和复杂图文负例。

这三份 PDF 当前没有完整同级 `.pdf2md/` 结果。后续转换时必须把结果留在本目录的同名 `<stem>.pdf2md/`，不要把源文件或解析结果重新散落到项目根目录。

许可边界以 `data/corpus.json` 为准：三份资料都只作本地回归，`redistributable=false`、`training_eligible=false`。
