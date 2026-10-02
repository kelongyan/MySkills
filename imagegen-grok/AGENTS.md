# AGENTS.md

本文件给在此仓库工作的 ZCode agent 提供项目背景。更详细的行为规范见 `SKILL.md`。

## 这是什么

一个 **host-agnostic 的 agent Skill 包**（skill 名 `imagegen`），不是可运行的应用。它告诉 AI agent 如何为项目生成和编辑位图，`SKILL.md` 是权威行为规范，agent 运行时加载并遵循。`scripts/` 下的 Python 脚本是 CLI 路径，宿主没有内置图像工具时使用。

没有构建步骤、没有包清单，当前也不是 git 仓库。改动主要是文档编辑加几个独立脚本。`scripts/test_provider_pool.py` 是唯一的自测，纯离线（不联网、不需要密钥）。

## 目录结构

| 路径 | 作用 |
| --- | --- |
| `SKILL.md` | 决策树、工作流、提示词增强规则、用例 slug 分类、模型/尺寸指引。**改任何 agent 可见行为前先读** |
| `scripts/image_gen.py` | CLI：`generate` / `edit` / `generate-batch` |
| `scripts/provider_pool.py` | 多提供商/多 Key 调度（加权轮询、并发上限、冷却、跨 Key 重试）。被 `image_gen.py` 导入 |
| `scripts/test_provider_pool.py` | 池子逻辑的离线自测；改 `provider_pool.py` 后跑它 |
| `scripts/remove_chroma_key.py` | 色键转 alpha（硬键、软遮罩、dominance alpha、溢色清理、边缘羽化/收缩） |
| `providers.toml.example` | 多提供商池配置模板；真实 `providers.toml` 放用户配置目录（Windows `%APPDATA%/imagegen`），**不放 skill 目录** |
| `provider.env.example` | 单 provider 回退配置模板；真实 `provider.env` 同样放用户配置目录 |
| `agents/openai.yaml` | skill 展示名、图标、默认提示词 |
| `assets/` | 图标资源 |
| `references/cli.md`、`image-api.md`、`codex-network.md` | **仅 CLI 路径**的参考文档 |
| `references/prompting.md`、`sample-prompts.md` | 两条路径共用 |
| `output/imagegen/` | 最终产物目录 |

## 两条执行路径（核心心智模型）

1. **宿主内置图像工具**（如 Codex 的 `image_gen`）——宿主提供时优先，通常不需要 API key。
2. **CLI 路径** `scripts/image_gen.py`——宿主没有内置工具时（Claude Code、opencode、ZCode 等）的正常路径，用户想要 CLI/API/model/provider 控制时也用它。需要 `OPENAI_API_KEY`，非 OpenAI provider 还需 `OPENAI_BASE_URL`。大量生图时可配 `providers.toml` 启用多提供商/多 Key 池（`scripts/provider_pool.py`），`--providers` 指定配置、`--no-pool` 关闭、`--sticky`/`--sticky-slot` 锁定单个 slot 求风格一致；不配则行为与改造前完全一致。

CLI 参数（`--quality`、`--input-fidelity`、mask、输出路径、`background=transparent`）只属于 CLI 路径，不是内置工具的入参。`references/` 里的 CLI 文档同理。

## 环境与命令

Python 3.12.10、uv 0.12.5 已可用。依赖（优先用 uv）：

```bash
uv pip install openai                 # 必需
uv pip install pillow                 # 可选，仅 --downscale-max-dim 和 remove_chroma_key.py 需要
```

```bash
# 生成
python scripts/image_gen.py generate --prompt "..." --out output/imagegen/hero.png

# 编辑（可多个 --image；图片需已在磁盘上）
python scripts/image_gen.py edit --image in.png --prompt "change only X; keep Y"

# 批量（JSONL，每行一个 job，必须给 --out-dir）
python scripts/image_gen.py generate-batch --input jobs.jsonl --out-dir output/imagegen/

# 不调用 API 预览任一命令
python scripts/image_gen.py generate --prompt "..." --dry-run
```

默认值：`--model` 依次取 `providers.toml` 的 `defaults.model` → `IMAGEGEN_MODEL` → `grok-imagine-image-quality`；另有 `--size auto`（支持 `16:9`、`1:1` 等比例）、`--quality medium`、`--output-format png`、`--out output/imagegen/output.png`、`--n 1`（上限 10）。

批量细节：JSONL 每行是字符串或含 `prompt` 的对象，最多 500 个 job，空行和 `#` 开头的行会跳过。`--concurrency` 与 `--max-attempts` 默认都不传入：无 `providers.toml` 时分别退化为 5（上限 25）和 3；有 `providers.toml` 时并发由各 slot 的 `max_concurrency` 决定（此时 `--concurrency` 被忽略并警告），而显式传入的 `--max-attempts` 会覆盖池子的 `max_attempts`。

路径约定：中间文件放 `tmp/imagegen/`（用完删掉），最终产物放 `output/imagegen/`。已存在的输出不加 `--force` 不会覆盖。

## Provider 配置（CLI）

CLI 从真实环境变量或 `provider.env` 读取 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`IMAGEGEN_MODEL`，**真实环境变量优先**。

**skill 本体不得携带真实密钥**：仓库内只允许 `*.example` 模板，真实凭据一律放用户配置目录（Windows `%APPDATA%/imagegen`，其他平台 `$XDG_CONFIG_HOME/imagegen` 或 `~/.config/imagegen`）。

`provider.env` 与 `providers.toml` 的查找顺序：`--providers PATH`（仅 providers.toml）→ `IMAGEGEN_PROVIDERS`（仅 providers.toml）→ `IMAGEGEN_CONFIG_DIR` → 本环境专属目录 `<用户配置目录>/<宿主名>` → 共享用户配置目录 → skill 根目录（仅兜底）。

**一份包、多环境各配一份**：副本装在 `<宿主>/.xxx/skills/imagegen` 时自动使用 `<用户配置目录>/xxx/`（workbuddy / claude / opencode …），互不干扰；本体（无宿主树）用共享目录。

响应同时兼容 `b64_json` 和 `url`，所以 OpenAI 兼容的第三方 provider 也能用。`--model` 非 `gpt-image-` 前缀只警告不报错。

## 不要改坏的东西

- **专门适配 `grok-imagine-image-quality`。** 移除 GPT 专有尺寸倍数限制与切换逻辑，原生支持比例字符串传参（`16:9`、`1:1`、`3:4`、`2:3`、`auto` 等）。
- `n` 只产生**同一个 prompt** 的变体。不同素材要不同调用或不同 batch job，不能拿 `n` 顶替。
- CLI 允许修改和扩展（比如支持新 provider/host），这是有意放开的，但保持向后兼容。
- 池子只决定"用哪个 Key"，服务于 `grok-imagine-image-quality`。无 `providers.toml` 时必须保持单 provider 行为。
- **不要把真实密钥写进 skill 目录**（包括 `providers.toml`、`provider.env`，以及任何脚本内的硬编码）。凭据只放用户配置目录，包内只留 `*.example` 模板。
- `SKILL.md` 里的 taxonomy slug（如 `product-mockup`、`precise-object-edit`、`background-extraction`）在所有出现处必须一致。
- 编辑 `SKILL.md` 或 `references/` 时，不要把 built-in 与 CLI 的边界写模糊——涉及保存路径、换模型、透明背景、批量的表述都容易在"顺手简化"时被破坏。

## 透明背景

- 内置工具路径：直接向 `image_gen` 要真正的透明背景并保留 alpha。
- CLI 路径：在提示词中指定纯色背景（如纯绿/纯白），配合色键流程提取透明通道：

```bash
python scripts/remove_chroma_key.py --input in.png --out out.png --auto-key corners --soft-matte --despill
```

`remove_chroma_key.py` 输出必须是 `.png` 或 `.webp`；`--soft-matte` 要求 `--transparent-threshold` 小于 `--opaque-threshold`。

## 改前先读

改 agent 行为读 `SKILL.md`；改 CLI 参数或 API 细节读 `references/cli.md` 和 `references/image-api.md`；网络/沙箱问题读 `references/codex-network.md`。

`CLAUDE.md` 是同一份项目说明的 Claude Code 版本，与本文件内容重叠。改动仓库约定时两边都要同步，避免其中一份过期。

## 术语上的已知不一致

`SKILL.md` 把 CLI 定位为"first-class，不是最后手段"，但 `references/` 下的文档统一称之为 "fallback CLI mode"。这是仓库现存的历史措辞，不是笔误。改文档时沿用所在文件的既有说法，不要单方面统一——`references/` 的 "fallback-only" 措辞承担着"这些参数不属于内置工具"的区分作用。
