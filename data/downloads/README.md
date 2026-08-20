# Corpus 下载区

这个目录保存 `data/corpus.json` 管理的官方/公开测试资料。路径按语言、文档类型和领域分层；每个 PDF 的解析结果必须与源文件同级，命名为 `<stem>.pdf2md/`。

下载、校验和来源固定统一使用 `scripts/manage-corpus.py`。不要手工把同一文件复制到新的测试目录；需要承担不同回归角色时，应在相应 README 记录 SHA 重复关系，而不是用不可移植的硬链接替代清单。

PDF、`.part`、`.pdf2md/` 与 `local-state.json` 均被 Git 忽略。仓库版本化的是 `data/corpus.json` 中的 URL、许可判断、预期区域、大小和 SHA-256；只有 `training_eligible=true` 且许可重新核验通过的条目才能进入训练。
