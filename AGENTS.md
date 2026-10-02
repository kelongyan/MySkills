# AGENTS.md

本文件给在 `F:\MySkills` 工作的 agent 提供工作区级背景。这里**没有根级构建、测试、lint 配置，也不是 git 仓库**——每个子目录是一个独立分发的 agent Skill 包，各自的约定在包内维护。

## 工作区结构

| 目录 | 是什么 | 权威规范 |
| --- | --- | --- |
| `imagegen/` | 位图生成/编辑 skill（OpenAI 兼容，模型锁定 `gpt-image-2`） | 包内 `SKILL.md` |
| `imagegen-grok/` | 同一 skill 的分支，改用 `grok-imagine-image-quality`，原生支持比例字符串 `--size` | 包内 `SKILL.md` |
| `storage-analyzer/` | 只读磁盘存储分析 skill，产出交互式 HTML 报告 + 本地一键删除服务 | 包内 `SKILL.md` |

**进任何一个包做事前，先读该包的 `AGENTS.md`（或 `CLAUDE.md`，两者内容相同）和 `RULE.md`**——真实凭据位置、模型锁定、安全模型等关键约定都在包内，本文件不重复。

## 包之间没有同步义务

`imagegen/` 与 `imagegen-grok/` 是近亲分支：`provider_pool.py`、`remove_chroma_key.py`、`test_provider_pool.py` 当前逐字节相同，只有 `image_gen.py` 和 `SKILL.md` 的模型相关部分不同。但两者**独立维护，不要求双向同步**——在一个包里修共享脚本的 bug，不需要镜像到另一个。

## 环境与命令（Windows）

- 终端是 PowerShell；Windows 上 Python 命令多为 `python` 或 `py -3`，不是 `python3`（`storage-analyzer` 的文档示例写 `python3`，在 Windows 上要替换）。
- `imagegen` / `imagegen-grok` 需要 `openai`（必需）和 `pillow`（仅 `--downscale-max-dim` 和 `remove_chroma_key.py` 需要）；全局 Python 3.12 已装好，无需再安装。
- `storage-analyzer` 是纯 Python 3 标准库，零依赖。
- 这个工作区没有统一测试入口。唯一的自测在两个 imagegen 包里，纯离线（不联网、不要密钥）：

```bash
python imagegen/scripts/test_provider_pool.py
```

改 `provider_pool.py` 后必须跑它（两个包各自跑自己的副本）。`storage-analyzer` 没有测试。

## 红线（跨包通用）

- **任何真实 API key、凭据不得写进工作区**。包内只允许 `*.example` 模板；真实 `providers.toml` / `provider.env` 放用户配置目录（Windows `%APPDATA%/imagegen`）。
- `storage-analyzer` **全程只读**：只跑扫描/统计命令，删除命令只展示给用户自己执行，不代跑。
- 文档统一用简体中文写，与现有文件保持一致；路径、命令保留原文不翻译。
