# Archive

`archive/` 只保存已经退出正式入口、但仍有追溯价值的快照与实验产物。运行时、安装脚本、训练器和测试都不得从这里导入代码或加载模型；恢复某项内容时，应先复制回正式位置并重新通过测试，而不是直接把 `archive/` 当作第二套实现。

## 当前归档

| 归档路径 | 原路径 | 原因 |
|---|---|---|
| `skill-snapshots/pdf2md-read-pdf-skill.zip` | `backups/pdf2md-read-pdf-skill.zip` | 整理前的完整 Skill ZIP 快照；正式、持续更新的来源仍是 `skills/pdf2md-read-pdf/` |
| `legacy-config/conda-environment.yml` | `config/conda-environment.yml` | 已被 `scripts/install.ps1` 的 prefix 环境安装流程取代，不能作为第二安装入口 |
| `experiments/front-region/2026-08-20/candidate-hardening-check/` | `models/front-region/candidate-hardening-check/` | 未批准的前置区域分类实验产物 |
| `experiments/front-region/2026-08-20/candidate-hardening-check-normal/` | `models/front-region/candidate-hardening-check-normal/` | 未批准的对照实验产物 |
| `experiments/front-region/2026-08-20/training-smoke/` | `tmp/front-region-training-smoke/` | 一次性训练烟测产物 |

三个实验目录的策略均未获准自动动作，不参与生产推理。它们被 Git 忽略，只用于本机问题追溯。

## 归档规则

- 只有确认无正式入口、无代码或测试引用、且仍值得保留的内容才能归档。
- 可重新生成的 `__pycache__`、`*.pyc`、临时预览、构建目录和空目录直接清理，不进入归档。
- 测试/训练 PDF 与解析结果统一归入 `data/`，不把它们误收进 `archive/`；当前模型和 OCR 运行时也不作为废弃内容归档。
- `src/`、`scripts/`、`tests/` 中仍被调用或覆盖的文件不归档。
