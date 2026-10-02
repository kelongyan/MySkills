# MySkills

个人 agent skill 工作区。每个子目录是一个独立分发的 Agent Skill 包，由 AI agent 宿主（Claude Code、opencode、Codex 等）加载 `SKILL.md` 作为行为规范；不是可运行的应用，也没有根级构建步骤。

## Skills

| 目录 | 作用 | 权威规范 |
| --- | --- | --- |
| [`imagegen/`](imagegen/) | 位图生成与编辑。宿主有内置图像工具时优先用内置；没有时走 CLI（`scripts/image_gen.py`），支持多提供商/多 Key 池调度、批量生成、色键抠透明背景。CLI 路径锁定模型 `gpt-image-2` | [`imagegen/SKILL.md`](imagegen/SKILL.md) |
| [`imagegen-grok/`](imagegen-grok/) | `imagegen` 的分支，改用 `grok-imagine-image-quality`，`--size` 原生支持比例字符串（`16:9`、`1:1`、`auto` 等）。与 `imagegen/` 独立维护，不要求同步 | [`imagegen-grok/SKILL.md`](imagegen-grok/SKILL.md) |
| [`storage-analyzer/`](storage-analyzer/) | 只读磁盘存储分析（macOS / Windows 自动识别）。扫描整机占用，按 🟢可自动清理 / 🟡需人工判断 / 🔴谨慎清理 三级给出处置方案，生成交互式 HTML 报告，并可起本地服务在网页上一键删除（移废纸篓 / 直接删） | [`storage-analyzer/SKILL.md`](storage-analyzer/SKILL.md) |

每个包内还有 `AGENTS.md` / `CLAUDE.md`（项目说明，两者内容相同）与 `RULE.md`（本地开发约定，不入库），进包干活前先读。

## 环境

- Windows，PowerShell；Python 命令多为 `python` 或 `py -3`（不是 `python3`）。
- `imagegen` / `imagegen-grok` 依赖 `openai`（必需）和 `pillow`（仅缩放与色键脚本需要），优先用 `uv pip install`。
- `storage-analyzer` 纯 Python 3 标准库，零依赖。

## 测试

工作区没有统一测试入口。两个 imagegen 包各有一个纯离线自测（不联网、不需要密钥），改 `provider_pool.py` 后必须跑：

```bash
python imagegen/scripts/test_provider_pool.py
python imagegen-grok/scripts/test_provider_pool.py
```

`storage-analyzer` 没有测试。

## 约定

- **任何真实 API key 不得写进仓库。** 包内只允许 `*.example` 模板，真实凭据放用户配置目录（Windows `%APPDATA%/imagegen`）。
- `storage-analyzer` 全程只读：只跑扫描/统计命令，删除命令只展示给用户自己确认后执行。
- 文档用简体中文，路径与命令保留原文。
