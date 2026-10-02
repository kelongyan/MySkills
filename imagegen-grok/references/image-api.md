# Image API quick reference

This file describes the Image API parameters for Grok Imagine models and the bundled CLI (`scripts/image_gen.py`).

## Scope
- This CLI is dedicated to Grok Imagine models (`grok-imagine-image-quality`, `grok-imagine-image`).
- The built-in `image_gen` tool and the CLI do not expose the same controls.

## Model summary

| Model | Quality | Output Formats | Aspect Ratios & Resolutions | Recommended use |
| --- | --- | --- | --- | --- |
| `grok-imagine-image-quality` | `low`, `medium`, `high`, `auto` | `png`, `jpeg`, `webp` | `16:9`, `1:1`, `9:16`, `4:3`, `3:4`, `2:3`, `3:2`, `auto`, `WIDTHxHEIGHT` | Default high-quality generation model with rich texture and strong prompt adherence |
| `grok-imagine-image` | `low`, `medium`, `high`, `auto` | `png`, `jpeg`, `webp` | `16:9`, `1:1`, `9:16`, `4:3`, `3:4`, `2:3`, `3:2`, `auto`, `WIDTHxHEIGHT` | Standard speed-optimized Grok generation model |

## Grok sizes and aspect ratios

Grok accepts `auto`, standard aspect ratio strings, or explicit `WIDTHxHEIGHT`:

| Aspect Ratio / Size | Typical Resolution | Notes |
| --- | --- | --- |
| `16:9` | `1280x720` | Widescreen landscape (hero banners, wallpaper) |
| `1:1` | `960x960` | Square (avatars, icons, product thumbnails) |
| `9:16` | `720x1280` | Vertical portrait (mobile wallpapers, stories) |
| `4:3` | `1152x864` | Standard landscape |
| `3:4` | `864x1152` | Standard portrait |
| `2:3` | `768x1152` | Photographic portrait |
| `auto` | Adaptive | Size determined automatically from prompt |

## Endpoints
- Generate: `POST /v1/images/generations` (`client.images.generate(...)`)
- Edit: `POST /v1/images/edits` (`client.images.edit(...)`)

## Core parameters for Grok Imagine
- `prompt`: text prompt
- `model`: image model (`grok-imagine-image-quality`)
- `n`: number of images (1-10)
- `size`: `auto`, aspect ratio string (`16:9`, `1:1`, etc.), or `WIDTHxHEIGHT`
- `quality`: `low`, `medium`, `high`, or `auto`
- `output_format`: `png` (default), `jpeg`, `webp`

## Transparent backgrounds

Grok models output RGB JPEG images without native alpha channels. To generate transparent assets:
1. Specify a solid flat background color in the prompt (e.g. `on a solid bright green chroma-key background`).
2. Run `scripts/remove_chroma_key.py` on the output to isolate the subject and create a clean PNG/WEBP with transparency.

```bash
python scripts/remove_chroma_key.py --input in.png --out out.png --auto-key corners --soft-matte --despill
```

## Output
- `data[]` list with `b64_json` per image
- The bundled `scripts/image_gen.py` CLI decodes `b64_json` and writes output files for you.

## Limits and notes
- Input images and masks must be under 50MB.
- Use the edits endpoint when the user requests changes to an existing image.
- Masking is prompt-guided; exact shapes are not guaranteed.
- Large sizes and high quality increase latency and cost.
- Use `quality=low` for fast drafts, thumbnails, and quick iterations. Use `medium` or `high` for final assets, dense text, diagrams, identity-sensitive edits, or high-resolution outputs.
- High `input_fidelity` can materially increase input token usage on models that support it.
- If a request fails because a specific option is unsupported by the selected GPT Image model, retry manually without that option only when the option is not required by the user. If true transparent CLI output is required, ask before switching to `gpt-image-1.5` instead of dropping `background=transparent`, unless the user already explicitly chose that fallback.

## Important boundary
- `quality`, `input_fidelity`, explicit masks, `background`, `output_format`, and related parameters are fallback-only execution controls.
- Do not assume they are built-in `image_gen` tool arguments.
