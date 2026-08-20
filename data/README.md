# PDF2MD 测试语料

这个目录统一保存所有测试/回归/训练 PDF 及其解析结果、清单和标注，不再在项目根保留独立 `paper/` 树。

| 路径 | 内容 |
|---|---|
| [`local-examples/`](local-examples/README.md) | 用户原有的 3 份本地示例 PDF；由 `corpus.json` 固定 SHA/大小，尚无完整解析结果 |
| [`downloads/`](downloads/README.md) | 由 `corpus.json` 管理的跨领域官方/公开资料；PDF 与同名 `.pdf2md/` 同级 |
| [`regression/`](regression/README.md) | 本地真实回归集；当前含 21 份冷原子、原子干涉仪博士论文及完整解析结果 |
| [`training/`](training/README.md) | 主类型/页内导航标注、评测报告、本地复核数据和合成训练 PDF/解析缓存 |
| [`corpus.json`](corpus.json) | 可下载/可核验语料的固定清单 |
| [`front-eval-plan.json`](front-eval-plan.json) | 由 manifest 与 inspect 缓存生成的确定性前置页评测计划 |
| [`front-review-queue.json`](front-review-queue.json) | 由 V2 分类报告生成、仅含元数据的低置信/结构异常复核队列 |
| `local-state.json` | 被 Git 忽略的本机下载状态，不作为跨机器信任根 |

主清单当前含 109 份资料，101 份本地文件已由 manifest 大小和 SHA-256 固定；PDF 文件不会提交到 Git，本机下载信息保存在被忽略的 `local-state.json`。只有带 `expected_size` 与 `expected_sha256` 的条目具备跨机器内容固定能力。`local-examples/` 与 `regression/` 中的历史本地资料默认只用于测试，不因被移动到 `data/` 就自动获得训练许可。

## 快速使用

```powershell
./runtime/env/python.exe ./scripts/manage-corpus.py list
./runtime/env/python.exe ./scripts/manage-corpus.py download --suite smoke --max-total-mb 250
./runtime/env/python.exe ./scripts/manage-corpus.py verify --suite smoke
```

`download` 在没有 `--suite` 或 `--id` 时只下载较小的 `smoke` 集。选项可重复，例如：

```powershell
./runtime/env/python.exe ./scripts/manage-corpus.py download --id nist-ai-rmf-100-1 --id arxiv-attention-1706-03762
./runtime/env/python.exe ./scripts/manage-corpus.py download --suite core --max-total-mb 500
./runtime/env/python.exe ./scripts/manage-corpus.py download --id some-unpinned-id --accept-unpinned
```

自动下载默认要求条目同时提供 `expected_size` 与 `expected_sha256`；只有明确使用 `--accept-unpinned` 才允许下载未固定内容。下载器只接受无 userinfo、默认 443 端口的 HTTPS，初始请求和每次重定向都会在发送前解析 DNS，并拒绝任一非公网地址。它限制单文件和本次总大小、验证 PDF 文件头及 manifest pin，先写入 `.part` 再原子落盘；若固定 `.part` 已存在则安全失败，不会删除或覆盖它。

已存在的 PDF 不会盲目跳过：程序会检查普通文件、PDF magic、大小和 SHA-256。带 manifest pin 的文件即使缺少 state 也可独立验证，`download` 会据此重建 state。任何未固定条目——即使本地已经有文件和 TOFU state——默认都拒绝下载或认可，必须显式使用 `--accept-unpinned`；`verify --accept-unpinned` 才会用严格校验过的本地 state 做 TOFU 验证。manifest pin 始终优先，state 不能绕过错误 pin。自定义 `--manifest` 未指定 `--state` 时，state 默认位于该 manifest 同目录的 `local-state.json`。

Windows 路径会额外拒绝 NTFS ADS、驱动器相对路径、设备名和尾随点/空格；下载目标、`.part`、manifest、state 及其祖先目录不得碰撞。并发下载必须拥有自己的临时文件，下载过程中若目标突然出现则安全失败，不会覆盖其他进程或用户文件。

DNS 会在请求前检查，但解析检查与实际连接之间仍存在时间窗口；不要把未经审核的不可信 manifest 当作通用 URL 下载清单，也不要通过关闭 TLS 验证来绕过来源站点的证书错误。

## 清单约定

- `smoke`：少量中英文、多版式快速回归样本。
- `core`：芯片资料、技术手册、论文、政府手册和书籍的主回归集。
- `extended`：更广领域或较大文件，按需下载。
- `local-existing`：用户已经放入 `data/` 的文件，不提供远程下载地址。
- 顶层 `front_region_schema` 必须是 `pdf2md.front-regions.v1`。
- `expected_front_regions` 是待检测区域提示，不是固定页码真值；管理器会拒绝未知或重复标签。
- `expected_size` 与 `expected_sha256` 必须成对出现；smoke 下载样本应全部固定。
- 没有稳定 PDF 直链但保留官方来源页的条目标为 `manual-only`，`download` 会安全跳过。

`redistributable` 与 `training_eligible` 是保守的工程标签，不替代法律意见。厂商文档、许可不明确的 arXiv 文件、用户提供文件和明确限制生成式 AI 训练的材料默认不能用于训练；政府资料若可能包含第三方图片也保守标为不可训练。只有明确开放许可且已核验训练用途的条目才可标记为可训练，模型微调前仍应重新核验来源页面的最新许可。

请勿提交下载后的 PDF、`.part` 文件或 `local-state.json`。新增样本时应优先选择作者、厂商、标准组织、政府或开放教材的官方页面，并同时记录来源页面和直接 PDF URL。
