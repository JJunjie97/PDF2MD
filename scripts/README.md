# 脚本索引

这里的脚本都是当前维护工具，不是废弃代码。

## 安装、运行与构建

| 脚本 | 用途 |
|---|---|
| `runtime.ps1` | 统一项目路径、环境变量和本地运行时发现 |
| `install.ps1` | 创建或修复 `runtime/env` prefix 环境 |
| `download-models.ps1` | 下载 MinerU 模型并写入本地配置 |
| `build-icon.py` | 从源图生成程序图标 |
| `build.ps1` | 构建 GUI 外壳 `PDF2MD.exe` |

## 质量审计

| 脚本 | 用途 |
|---|---|
| `audit-navigation.py` | 检查目录锚点、正反链、长行和字节级幂等性 |
| `build-front-eval-plan.py` | 从固定语料与 inspect 缓存生成最小前置页评测计划 |
| `build-front-review-queue.py` | 汇总拒识、低 margin、OOD 与结构异常页的元数据复核队列 |
| `evaluate-front-regions.py` | 只读现有 content-list 缓存，对人工金标评测前置区域分类 |

## 语料与离线训练

| 脚本 | 用途 |
|---|---|
| `manage-corpus.py` | 校验、下载和核验固定 SHA-256 的回归语料 |
| `manage-front-training.py` | 校验页级主类型与导航存在性标注 |
| `generate-front-region-synthetic.py` | 生成确定性的中英文 CC0 合成前置页 |
| `train-front-region-model.py` | 从已验证缓存训练实验性布局/文本分类头 |

生产转换的唯一 Python 入口仍是 `src/pdf2md_cli.py`；这些脚本不得绕过核心发布与安全校验。

