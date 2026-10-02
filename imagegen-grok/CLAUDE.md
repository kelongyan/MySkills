# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This is a **host-agnostic agent Skill package** named `imagegen`, not a runnable application. It instructs an AI agent (Codex, Claude Code, opencode, ...) on how to generate and edit raster images for a project. `SKILL.md` is the authoritative behavioral spec — the agent loads it at runtime and follows it. The Python scripts under `scripts/` are the CLI path, used whenever the host has no built-in image tool.

There is no build step and no package manifest, and (at snapshot time) no git history. Changes here are edits to documentation and a few standalone scripts. `scripts/test_provider_pool.py` is the only test suite: offline, no network, no keys, run it after touching pool behavior.

## Two execution paths (the core mental model)

The skill is host-agnostic. `SKILL.md` defines two paths; pick by what the host agent provides:

1. **Built-in image tool** — used when the host exposes one (e.g. Codex's `image_gen`). No API key. Not available in Claude Code / opencode / most agents.
2. **CLI path** — `scripts/image_gen.py`. The normal path when there is no built-in tool, and whenever the user wants CLI/API/model/provider control. Needs credentials: a `providers.toml` pool, or `OPENAI_API_KEY` (plus `OPENAI_BASE_URL` for non-OpenAI providers). For high-volume batches it can spread work over many keys/providers via that pool (`scripts/provider_pool.py`); with no such file the behavior is unchanged.

CLI parameters (`--quality`, `--input-fidelity`, masks, output paths, `background=transparent`) apply to the CLI path only; they are not arguments to a host's built-in tool. The `references/` files are CLI-specific.

## Provider configuration (CLI)

- The CLI loads `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `IMAGEGEN_MODEL` from real env vars or a `provider.env` file (`_load_provider_env`); real env vars win.
- **The skill package must never carry credentials.** Only `*.example` templates belong in the tree.
- Config resolution for `provider.env` / `providers.toml`: `IMAGEGEN_CONFIG_DIR` → the environment's own directory `<config dir>/<host>` → the shared `<config dir>` → the skill root (legacy). `<config dir>` is `%APPDATA%/imagegen` on Windows, `$XDG_CONFIG_HOME/imagegen` or `~/.config/imagegen` elsewhere.
- **One config per agent environment.** A copy installed at `<host>/.claude/skills/imagegen` resolves to `<config dir>/claude`, so Claude Code, opencode and WorkBuddy can each run a different provider set from the same package. A bare checkout has no host tree and uses `<config dir>`.
- Responses may carry `b64_json` **or** `url` — both are handled (`_item_to_bytes` / `_download_url`, with a browser UA to avoid CDN 403s), so OpenAI-compatible third-party providers work.
- `--model` defaults to `IMAGEGEN_MODEL` (else `grok-imagine-image-quality`). Non-`grok-imagine` model names only warn. Size is validated as `auto`, standard aspect ratio strings (`16:9`, `1:1`, `9:16`, `4:3`, `3:4`, `2:3`, `3:2`), or a well-formed `WIDTHxHEIGHT` (provider enforces its own limits).
- Multi-provider / multi-key scheduling is opt-in via `providers.toml` (implemented in `scripts/provider_pool.py`). `--providers PATH` overrides the lookup, `--no-pool` disables it, `--sticky` / `--sticky-slot NAME` pin a run to one slot for style consistency, and with no config file the single-provider path is used unchanged.

## Things to keep intact

- **Dedicated model is `grok-imagine-image-quality`.**
- Grok models output RGB images without native alpha channels. For transparent cutouts, prompt on a solid chroma-key background and extract alpha with `scripts/remove_chroma_key.py`.
- `n` produces variants of *one* prompt. Distinct assets need distinct calls / distinct batch jobs — never reuse `n` as a substitute.
- The CLI is safe to modify/extend for new providers or hosts (this was previously forbidden). Keep changes backward compatible.
- The provider pool decides *which key* is used for `grok-imagine-image-quality`. Without a `providers.toml`, the single-provider behavior from env vars stays intact.
- Never ship credentials inside the skill tree (`providers.toml`, `provider.env`, or keys hardcoded in scripts). They belong in the per-machine config directory; the tree keeps only `*.example` templates.

## Running the CLI

On hosts without a built-in image tool (Claude Code included), or when the user wants CLI/API/provider control:

```bash
# generate
python scripts/image_gen.py generate --prompt "..." --out output/imagegen/hero.png

# edit (one or more --image; images already on disk)
python scripts/image_gen.py edit --image in.png --prompt "change only X; keep Y"

# batch (JSONL, one job per line; requires --out-dir)
python scripts/image_gen.py generate-batch --input jobs.jsonl --out-dir output/imagegen/

# preview any invocation without calling the API
python scripts/image_gen.py generate --prompt "..." --dry-run

# batch across multiple keys/providers (config lives outside the skill tree)
python scripts/image_gen.py generate-batch --input jobs.jsonl --out-dir output/imagegen/

# ...or point at one specific config file
python scripts/image_gen.py generate-batch --input jobs.jsonl --out-dir output/imagegen/ --providers /path/to/providers.toml

# pool self-checks: offline, no keys needed
python scripts/test_provider_pool.py
```

Dependencies (prefer `uv`): `uv pip install openai`, optionally `uv pip install pillow` (needed only for `--downscale-max-dim` and for `remove_chroma_key.py`). Live calls need credentials — a `providers.toml` pool, or `OPENAI_API_KEY` on the single-provider path; `--dry-run` works without either.

CLI conventions baked into the scripts: intermediate files go under `tmp/imagegen/`, final artifacts under `output/imagegen/`. Existing outputs are not overwritten without `--force`.

### Transparent-background workflow via the CLI

`remove_chroma_key.py` supports the "built-in-first" transparent path: generate an image on a flat key color, then convert that color to alpha. Output must be `.png` or `.webp`. `--soft-matte` requires `--transparent-threshold < --opaque-threshold`.

```bash
python scripts/remove_chroma_key.py --input in.png --out out.png --auto-key corners --soft-matte --despill
```

## Where behavior actually lives

- `SKILL.md` — decision tree, workflow, prompt-augmentation rules, use-case taxonomy slugs, model/size guidance. Read this before changing any agent-facing behavior.
- `agents/openai.yaml` — the skill's display name, icons, and default prompt.
- `scripts/image_gen.py` — CLI: `generate` / `edit` / `generate-batch`, shared arg parser (`_add_shared_args`), prompt augmentation (`_augment_prompt_fields`), size/model validation, async batch with cross-slot retry (`_invoke_with_pool`).
- `scripts/provider_pool.py` — pool config parsing (stdlib `tomllib`), smooth weighted round-robin, per-slot concurrency caps, cooldowns, error classification (`auth` / `retryable` / `fatal`).
- `scripts/test_provider_pool.py` — offline self-checks for the pool; run after changing pool behavior.
- `scripts/remove_chroma_key.py` — chroma-key → alpha conversion (hard key, soft matte, dominance-based alpha, spill cleanup, edge feather/contract).
- `references/` — CLI-specific: `cli.md`, `image-api.md`, `codex-network.md`, plus `prompting.md` and `sample-prompts.md` (shared by both paths).

## When editing SKILL.md or references

Keep the two-mode separation intact. Any statement about save paths, model switching, transparency, or batching must not blur built-in vs CLI behavior — that separation is the whole point of the wording and is easy to break with a well-meaning simplification. The taxonomy slugs in `SKILL.md` (e.g. `product-mockup`, `precise-object-edit`, `background-extraction`) must stay consistent wherever they appear.
