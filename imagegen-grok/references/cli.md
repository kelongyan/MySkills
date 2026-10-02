# CLI reference (`scripts/image_gen.py`)

This file is for the fallback CLI mode only. Read it when the user explicitly asks to use `scripts/image_gen.py` / CLI / API / model controls, or after the user explicitly confirms that a transparent-output request should use the `gpt-image-1.5` true-transparency fallback path.

`generate-batch` is a CLI subcommand in this fallback path. It is not a top-level mode of the skill.
The word `batch` in a user request is not CLI opt-in by itself.

## What this CLI does
- `generate`: generate a new image from a prompt
- `edit`: edit one or more existing images
- `generate-batch`: run many generation jobs from a JSONL file after the user explicitly chooses CLI/API/model controls

Real API calls require **network access** plus credentials: a `providers.toml` pool, or `OPENAI_API_KEY` on the single-provider path. `--dry-run` needs neither.

## Quick start (works from any repo)
Set a stable path to the skill CLI (default `CODEX_HOME` is `~/.codex`):

```
export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
export IMAGE_GEN="$CODEX_HOME/skills/.system/imagegen/scripts/image_gen.py"
```

Install dependencies into that environment with its package manager. In uv-managed environments, `uv pip install ...` remains the preferred path.

## Quick start

Dry-run (no API call; no network required; does not require the `openai` package):

```bash
python "$IMAGE_GEN" generate \
  --prompt "Test" \
  --out output/imagegen/test.png \
  --dry-run
```

Notes:
- One-off dry-runs print the API payload and the computed output path(s).
- Repo-local finals should live under `output/imagegen/`.

Generate (requires credentials + network):

```bash
python "$IMAGE_GEN" generate \
  --prompt "A cozy alpine cabin at dawn" \
  --size 1024x1024 \
  --out output/imagegen/alpine-cabin.png
```

Edit:

```bash
python "$IMAGE_GEN" edit \
  --image input.png \
  --prompt "Replace only the background with a warm sunset" \
  --out output/imagegen/sunset-edit.png
```

## Guardrails
- Use the bundled CLI directly (`python "$IMAGE_GEN" ...`) after activating the correct environment.
- Do **not** create one-off runners (for example `gen_images.py`) unless the user explicitly asks for a custom wrapper.
- Extending the CLI itself (`scripts/image_gen.py`, `scripts/provider_pool.py`) for new providers or hosts is allowed — keep the changes backward compatible and note what changed. Prefer that over forking a wrapper.
- Do not silently downgrade from CLI `gpt-image-2` or built-in `image_gen` to CLI `gpt-image-1.5`; ask first unless the user explicitly requested `gpt-image-1.5`.

## Multi-provider / multi-key pool

High-volume batches can spread across several keys and providers. This is
opt-in: without a `providers.toml` the CLI behaves exactly as before, using the
single provider from `provider.env` / real env vars (`provider.env` is resolved
with the same lookup order described below).

Config lookup order: `--providers PATH` → `IMAGEGEN_PROVIDERS` →
`IMAGEGEN_CONFIG_DIR` → this environment's own directory →
the shared config directory → the skill root (legacy). Start from
`providers.toml.example`, and keep the real file — it holds keys — outside the
skill tree.

**One config per agent environment.** A copy installed at
`<host>/.claude/skills/imagegen` resolves to `<config dir>/claude/providers.toml`,
so Claude Code, opencode, WorkBuddy, ... can each run a different provider set
from the same skill package. `<config dir>` is `%APPDATA%/imagegen` on Windows,
`$XDG_CONFIG_HOME/imagegen` or `~/.config/imagegen` elsewhere. A bare source
checkout has no host tree above it and uses `<config dir>` directly.

```toml
[pool]
strategy = "weighted-round-robin"   # or "least-inflight"
max_attempts = 6                    # per-job attempts across the whole pool
cooldown_base_seconds = 5
cooldown_max_seconds = 300
fail_threshold = 2
acquire_timeout_seconds = 600

[defaults]
model = "gpt-image-2"
max_concurrency = 4

[[provider]]
name = "key-a"
base_url = "https://host/v1"
api_key = "sk-..."
weight = 3                 # higher weight = more traffic
max_concurrency = 6        # in-flight cap for this key

[[provider]]
name = "key-b"
base_url_env = "OPENAI_BASE_URL"
api_key_env = "MY_KEY_VAR_B"
weight = 1
enabled = false            # keep it in the file, out of rotation
```

Rules:
- One `[[provider]]` entry is one scheduling slot. Several entries sharing a
  `base_url` are several keys on one provider; different `base_url` values are
  different providers. Both mix freely.
- Selection is smooth weighted round-robin, so `weight = 3` gets roughly three
  times the traffic, spread evenly rather than in bursts.
- `api_key_env` / `base_url_env` read from the environment, so a shared URL or
  secret need not be duplicated per slot.
- A slot with `enabled = false` is not scheduled and may omit credentials.
- A batch normally spreads across slots. When the outputs must look consistent,
  `--sticky` (or `--sticky-slot NAME`) pins the whole run to one slot; while
  pinned, a cooling slot is waited for rather than swapped out, so consistency
  is bought at the cost of throughput.

Failure semantics:
- `429` / `5xx` / timeout / upstream errors → cool that slot down
  (exponential, capped at `cooldown_max_seconds`) and retry the job on a
  **different** slot instead of sleeping on the same key.
- auth / quota errors (`401`, `403`, insufficient quota) → cool the key down
  hard and move on.
- request errors (`400`, `404`, `422`) → fail the job immediately; another key
  cannot fix a malformed request.

Related flags:
- `--providers PATH` — use a specific pool config.
- `--no-pool` — ignore any pool config and use the single-provider path.
- `--sticky` — pin the whole run to one slot (chosen by weight) so a batch
  stays stylistically consistent.
- `--sticky-slot NAME` — pin to a specific slot by name; errors out if the
  name is unknown. Implies `--sticky`.
- `--dry-run` — prints the `provider_slot` each job would be routed to.
- `--concurrency` — only applies to the single-provider fallback; when
  `providers.toml` defines per-slot limits it is ignored (with a warning).
- `--max-attempts` — overrides the pool's `max_attempts` when passed explicitly.

Batch mode prints a per-slot summary at the end (ok / fail / auth / fatal
counts and average latency).

Self-checks without network access: `python scripts/test_provider_pool.py`.

## Defaults
- Model: `grok-imagine-image-quality`
- Supported model family for this CLI: Grok Imagine models (`grok-imagine-*`) and OpenAI-compatible providers
- Size: `auto` (or aspect ratios `16:9`, `1:1`, `9:16`, `4:3`, `3:4`, `2:3`, `3:2`)
- Quality: `medium`
- Output format: `png`
- Default one-off output path: `output/imagegen/output.png`
- Background: unspecified unless `--background` is set

## grok-imagine size and model guidance

`grok-imagine-image-quality` is the default model for image generation.

- Use `--quality low` for fast drafts and quick iterations.
- Use `--quality medium`, `--quality high`, or `--quality auto` for final assets.
- Aspect ratios are natively supported via `--size` (`16:9`, `1:1`, `9:16`, `4:3`, `3:4`, `2:3`, `3:2`, `auto`).
- Explicit pixel sizes such as `--size 1024x1024` are also supported.
- Grok outputs RGB images (JPEG). For transparent cutouts, prompt on a solid chroma-key background and extract alpha with `scripts/remove_chroma_key.py`.

Popular Grok sizes:
- `16:9` (1280x720)
- `1:1` (960x960)
- `9:16` (720x1280)
- `4:3` (1152x864)
- `3:4` (864x1152)
- `2:3` (768x1152)
- `auto`

Fast draft:

```bash
python "$IMAGE_GEN" generate \
  --prompt "A product thumbnail of a matte ceramic mug on a stone surface" \
  --quality low \
  --size 1:1 \
  --out output/imagegen/mug-draft.png
```

Widescreen 16:9 landscape:

```bash
python "$IMAGE_GEN" generate \
  --prompt "A polished landing-page hero image of a futuristic neon city" \
  --quality high \
  --size 16:9 \
  --out output/imagegen/city-hero.png
```

Transparent output workflow (Chroma-Key):

```bash
# 1. Generate subject on a solid chroma background (e.g. green or white)
python "$IMAGE_GEN" generate \
  --prompt "A clean product cutout of a ceramic mug on a solid bright green chroma-key background" \
  --size 1:1 \
  --out output/imagegen/mug-raw.png

# 2. Extract alpha channel
python scripts/remove_chroma_key.py \
  --input output/imagegen/mug-raw.png \
  --out output/imagegen/mug-transparent.png \
  --auto-key corners \
  --soft-matte \
  --despill
```

## Quality, input fidelity, and masks (CLI fallback only)
These are explicit CLI controls. They are not built-in `image_gen` tool arguments.

- `--quality` works for `generate`, `edit`, and `generate-batch`: `low|medium|high|auto`
- `--input-fidelity` is **edit-only** and validated as `low|high`; it is not supported for `gpt-image-2`
- `--mask` is **edit-only**

Example:

```bash
python "$IMAGE_GEN" edit \
  --model gpt-image-1.5 \
  --image input.png \
  --prompt "Change only the background" \
  --quality high \
  --input-fidelity high \
  --out output/imagegen/background-edit.png
```

Mask notes:
- For multi-image edits, pass repeated `--image` flags. Their order is meaningful, so describe each image by index and role in the prompt.
- The CLI accepts a single `--mask`.
- Image and mask must be the same size and format and each under 50MB.
- Masks must include an alpha channel.
- If multiple input images are provided, the mask applies to the first image.
- Masking is prompt-guided; do not promise exact pixel-perfect mask boundaries.
- Use a PNG mask when possible; the script treats mask handling as best-effort and does not perform full preflight validation beyond file checks/warnings.
- In the edit prompt, repeat invariants (`change only the background; keep the subject unchanged`) to reduce drift.

## Output handling
- Use `tmp/imagegen/` for temporary JSONL inputs or scratch files.
- Use `output/imagegen/` for final outputs.
- Reruns fail if a target file already exists unless you pass `--force`.
- `--out-dir` changes one-off naming to `image_1.<ext>`, `image_2.<ext>`, and so on.
- Downscaled copies use the default suffix `-web` unless you override it.

## Common recipes

Generate with augmentation fields:

```bash
python "$IMAGE_GEN" generate \
  --prompt "A minimal hero image of a ceramic coffee mug" \
  --use-case "product-mockup" \
  --style "clean product photography" \
  --composition "wide product shot with usable negative space for page copy" \
  --constraints "no logos, no text" \
  --out output/imagegen/mug-hero.png
```

Generate + also write a downscaled copy for fast web loading:

```bash
python "$IMAGE_GEN" generate \
  --prompt "A cozy alpine cabin at dawn" \
  --size 1024x1024 \
  --downscale-max-dim 1024 \
  --out output/imagegen/alpine-cabin.png
```

Generate multiple prompts concurrently (async batch):

```bash
mkdir -p tmp/imagegen output/imagegen/batch
cat > tmp/imagegen/prompts.jsonl << 'EOF'
{"prompt":"Cavernous hangar interior with a compact shuttle parked near the center","use_case":"stylized-concept","composition":"wide-angle, low-angle","lighting":"volumetric light rays through drifting fog","constraints":"no logos or trademarks; no watermark","size":"1536x1024"}
{"prompt":"Gray wolf in profile in a snowy forest","use_case":"photorealistic-natural","composition":"eye-level","constraints":"no logos or trademarks; no watermark","size":"1024x1024"}
EOF

python "$IMAGE_GEN" generate-batch \
  --input tmp/imagegen/prompts.jsonl \
  --out-dir output/imagegen/batch \
  --concurrency 5

rm -f tmp/imagegen/prompts.jsonl
```

Notes:
- `generate-batch` requires `--out-dir`.
- `--concurrency` caps parallelism on the single-provider path (default `5`). When `providers.toml` is in use, each slot's own `max_concurrency` governs instead and `--concurrency` is ignored with a warning.
- Per-job overrides are supported in JSONL (for example `size`, `quality`, `background`, `output_format`, `output_compression`, `moderation`, `n`, `model`, `out`, and prompt-augmentation fields).
- `--n` generates multiple variants for a single prompt; `generate-batch` is for many different prompts.
- In batch mode, per-job `out` is treated as a filename under `--out-dir`.
- For many requested deliverable assets, provide one prompt/job per distinct asset and use semantic filenames when possible.

## CLI notes
- Supported sizes depend on the model. `gpt-image-2` supports flexible constrained sizes; older GPT Image models support `1024x1024`, `1536x1024`, `1024x1536`, or `auto`.
- True transparent CLI outputs require `output_format` to be `png` or `webp` and are not supported by `gpt-image-2`.
- `--prompt-file`, `--output-compression`, `--moderation`, `--max-attempts`, `--fail-fast`, `--force`, and `--no-augment` are supported.
- This CLI is intended for GPT Image models. Do not assume older non-GPT image-model behavior applies here.

## See also
- API parameter quick reference for fallback CLI mode: `references/image-api.md`
- Prompt examples shared across both top-level modes: `references/sample-prompts.md`
- Network/sandbox notes for fallback CLI mode: `references/codex-network.md`
- Built-in-first transparent image workflow: `SKILL.md`
