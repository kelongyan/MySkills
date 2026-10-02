#!/usr/bin/env python3
"""CLI for image generation or editing via GPT Image / OpenAI-compatible providers.

Works with the official OpenAI Image API and with OpenAI-compatible providers.
Response items carrying either `b64_json` or `url` are supported.

Provider defaults (OPENAI_API_KEY / OPENAI_BASE_URL / IMAGEGEN_MODEL) may be set
via real environment variables or a `provider.env` file next to the skill root;
real env vars win. Defaults to gpt-image-2 with a structured prompt augmentation
workflow. Usable by any host agent (Codex, Claude Code, opencode, ...).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

from io import BytesIO

# The bundled pool module sits next to this script; make it importable whether
# the CLI is run directly (`python scripts/image_gen.py`) or loaded as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from provider_pool import (  # noqa: E402  (import must follow the path fix)
    ENV_FILENAME,
    PoolConfig,
    PoolConfigError,
    PoolUnavailableError,
    ProviderPool,
    build_env_fallback_pool,
    classify_error,
    config_dir_candidates,
    load_pool_config,
)

DEFAULT_MODEL = "grok-imagine-image-quality"
DEFAULT_SIZE = "auto"
DEFAULT_QUALITY = "medium"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_CONCURRENCY = 5
DEFAULT_BATCH_ATTEMPTS = 3
DEFAULT_DOWNSCALE_SUFFIX = "-web"
DEFAULT_OUTPUT_PATH = "output/imagegen/output.png"
GROK_IMAGE_MODEL_PREFIX = "grok-imagine"
GROK_ALLOWED_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "2:3", "3:2"}

ALLOWED_QUALITIES = {"low", "medium", "high", "auto"}
ALLOWED_BACKGROUNDS = {"transparent", "opaque", "auto", None}
ALLOWED_INPUT_FIDELITIES = {"low", "high", None}

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_BATCH_JOBS = 500


def _die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def _dependency_hint(package: str, *, upgrade: bool = False) -> str:
    command = f"uv pip install {'-U ' if upgrade else ''}{package}"
    return (
        "Activate the repo-selected environment first, then install it with "
        f"`{command}`. If this repo uses a local virtualenv, start with "
        "`source .venv/bin/activate`; otherwise use this repo's configured shared fallback "
        "environment. If your project declares dependencies, prefer that project's normal "
        "`uv sync` flow."
    )


def _ensure_api_key(dry_run: bool, *, has_config_key: bool = False) -> None:
    if os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is set.", file=sys.stderr)
        return
    if has_config_key:
        print("Using credentials from the provider pool config.", file=sys.stderr)
        return
    if dry_run:
        _warn("OPENAI_API_KEY is not set; dry-run only.")
        return
    _die("OPENAI_API_KEY is not set. Export it before running.")


def _read_prompt(prompt: Optional[str], prompt_file: Optional[str]) -> str:
    if prompt and prompt_file:
        _die("Use --prompt or --prompt-file, not both.")
    if prompt_file:
        path = Path(prompt_file)
        if not path.exists():
            _die(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8").strip()
    if prompt:
        return prompt.strip()
    _die("Missing prompt. Use --prompt or --prompt-file.")
    return ""  # unreachable


def _check_image_paths(paths: Iterable[str]) -> List[Path]:
    resolved: List[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            _die(f"Image file not found: {path}")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            _warn(f"Image exceeds 50MB limit: {path}")
        resolved.append(path)
    return resolved


def _normalize_output_format(fmt: Optional[str]) -> str:
    if not fmt:
        return DEFAULT_OUTPUT_FORMAT
    fmt = fmt.lower()
    if fmt not in {"png", "jpeg", "jpg", "webp"}:
        _die("output-format must be png, jpeg, jpg, or webp.")
    return "jpeg" if fmt == "jpg" else fmt


def _parse_size(size: str) -> Optional[Tuple[int, int]]:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", size)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _validate_size(size: str, model: str) -> None:
    if size == "auto":
        return
    if size in GROK_ALLOWED_ASPECT_RATIOS:
        return
    if _parse_size(size) is not None:
        return
    allowed = ", ".join(sorted(GROK_ALLOWED_ASPECT_RATIOS))
    _die(f"size must be auto, an aspect ratio ({allowed}), or WIDTHxHEIGHT (e.g. 1024x1024).")


def _validate_quality(quality: str) -> None:
    if quality not in ALLOWED_QUALITIES:
        _die("quality must be one of low, medium, high, or auto.")


def _normalize_quality_for_model(quality: Optional[str], model: str) -> Optional[str]:
    if not quality:
        return quality
    if "grok" in model.lower() and quality in {"high", "auto"}:
        return "medium"
    return quality


def _validate_background(background: Optional[str]) -> None:
    if background not in ALLOWED_BACKGROUNDS:
        _die("background must be one of transparent, opaque, or auto.")


def _validate_input_fidelity(input_fidelity: Optional[str]) -> None:
    if input_fidelity not in ALLOWED_INPUT_FIDELITIES:
        _die("input-fidelity must be one of low or high.")


def _validate_model(model: str) -> None:
    if not model.startswith(GROK_IMAGE_MODEL_PREFIX):
        _warn(
            f"model '{model}' is not a standard Grok Imagine model name; proceeding with custom model."
        )


def _validate_transparency(background: Optional[str], output_format: str) -> None:
    if background == "transparent" and output_format not in {"png", "webp"}:
        _die("transparent background requires output-format png or webp.")


def _validate_model_specific_options(
    *,
    model: str,
    background: Optional[str],
    input_fidelity: Optional[str] = None,
) -> None:
    if background == "transparent":
        _warn(
            "Grok models output RGB images without native alpha channels. "
            "For transparent outputs, prompt for a solid chroma-key background and extract "
            "alpha with scripts/remove_chroma_key.py --auto-key corners --despill."
        )


def _validate_generate_payload(payload: Dict[str, Any]) -> None:
    model = str(payload.get("model", DEFAULT_MODEL))
    _validate_model(model)
    n = int(payload.get("n", 1))
    if n < 1 or n > 10:
        _die("n must be between 1 and 10")
    size = str(payload.get("size", DEFAULT_SIZE))
    quality = str(payload.get("quality", DEFAULT_QUALITY))
    background = payload.get("background")
    _validate_size(size, model)
    _validate_quality(quality)
    _validate_background(background)
    _validate_model_specific_options(model=model, background=background)
    oc = payload.get("output_compression")
    if oc is not None and not (0 <= int(oc) <= 100):
        _die("output_compression must be between 0 and 100")


def _build_output_paths(
    out: str,
    output_format: str,
    count: int,
    out_dir: Optional[str],
) -> List[Path]:
    ext = "." + output_format

    if out_dir:
        out_base = Path(out_dir)
        out_base.mkdir(parents=True, exist_ok=True)
        return [out_base / f"image_{i}{ext}" for i in range(1, count + 1)]

    out_path = Path(out)
    if out_path.exists() and out_path.is_dir():
        out_path.mkdir(parents=True, exist_ok=True)
        return [out_path / f"image_{i}{ext}" for i in range(1, count + 1)]

    if out_path.suffix == "":
        out_path = out_path.with_suffix(ext)
    elif output_format and out_path.suffix.lstrip(".").lower() != output_format:
        _warn(
            f"Output extension {out_path.suffix} does not match output-format {output_format}."
        )

    if count == 1:
        return [out_path]

    return [
        out_path.with_name(f"{out_path.stem}-{i}{out_path.suffix}")
        for i in range(1, count + 1)
    ]


def _augment_prompt(args: argparse.Namespace, prompt: str) -> str:
    fields = _fields_from_args(args)
    return _augment_prompt_fields(args.augment, prompt, fields)


def _augment_prompt_fields(
    augment: bool, prompt: str, fields: Dict[str, Optional[str]]
) -> str:
    if not augment:
        return prompt

    sections: List[str] = []
    if fields.get("use_case"):
        sections.append(f"Use case: {fields['use_case']}")
    sections.append(f"Primary request: {prompt}")
    if fields.get("scene"):
        sections.append(f"Scene/background: {fields['scene']}")
    if fields.get("subject"):
        sections.append(f"Subject: {fields['subject']}")
    if fields.get("style"):
        sections.append(f"Style/medium: {fields['style']}")
    if fields.get("composition"):
        sections.append(f"Composition/framing: {fields['composition']}")
    if fields.get("lighting"):
        sections.append(f"Lighting/mood: {fields['lighting']}")
    if fields.get("palette"):
        sections.append(f"Color palette: {fields['palette']}")
    if fields.get("materials"):
        sections.append(f"Materials/textures: {fields['materials']}")
    if fields.get("text"):
        sections.append(f'Text (verbatim): "{fields["text"]}"')
    if fields.get("constraints"):
        sections.append(f"Constraints: {fields['constraints']}")
    if fields.get("negative"):
        sections.append(f"Avoid: {fields['negative']}")

    return "\n".join(sections)


def _fields_from_args(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    return {
        "use_case": getattr(args, "use_case", None),
        "scene": getattr(args, "scene", None),
        "subject": getattr(args, "subject", None),
        "style": getattr(args, "style", None),
        "composition": getattr(args, "composition", None),
        "lighting": getattr(args, "lighting", None),
        "palette": getattr(args, "palette", None),
        "materials": getattr(args, "materials", None),
        "text": getattr(args, "text", None),
        "constraints": getattr(args, "constraints", None),
        "negative": getattr(args, "negative", None),
    }


def _print_request(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _load_provider_env() -> None:
    """Load single-provider defaults from a `provider.env` file.

    Searched in this environment's own config directory first (see
    `config_dir_candidates`), so each agent environment can keep its own
    credentials while the skill package itself stays free of keys; a
    `provider.env` next to the skill root still works for backward
    compatibility. The first file found wins, and real environment variables
    always take precedence over it.
    """
    candidates = [directory / ENV_FILENAME for directory in config_dir_candidates()]
    candidates.append(Path(__file__).resolve().parent.parent / ENV_FILENAME)

    for env_path in candidates:
        if not env_path.exists():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value
        return


def _download_url(url: str) -> bytes:
    # Some CDNs (e.g. Cloudflare in front of provider storage) reject the
    # default Python-urllib User-Agent with 403, so send a browser-like UA.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
        return resp.read()


def _item_to_bytes(item: Any) -> bytes:
    """Return raw image bytes from a response item.

    Standard OpenAI GPT Image responses carry `b64_json`; some
    OpenAI-compatible providers return a `url` instead. Support both.
    """
    b64 = getattr(item, "b64_json", None)
    if b64:
        return base64.b64decode(b64)
    url = getattr(item, "url", None)
    if url:
        return _download_url(url)
    _die("Image API response item had neither b64_json nor url.")
    return b""  # unreachable


def _derive_downscale_path(path: Path, suffix: str) -> Path:
    if suffix and not suffix.startswith("-") and not suffix.startswith("_"):
        suffix = "-" + suffix
    return path.with_name(f"{path.stem}{suffix}{path.suffix}")


def _downscale_image_bytes(
    image_bytes: bytes, *, max_dim: int, output_format: str
) -> bytes:
    try:
        from PIL import Image
    except Exception:
        _die(f"Downscaling requires Pillow. {_dependency_hint('pillow')}")

    if max_dim < 1:
        _die("--downscale-max-dim must be >= 1")

    with Image.open(BytesIO(image_bytes)) as img:
        img.load()
        w, h = img.size
        scale = min(1.0, float(max_dim) / float(max(w, h)))
        target = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))

        resized = (
            img if target == (w, h) else img.resize(target, Image.Resampling.LANCZOS)
        )

        fmt = output_format.lower()
        if fmt == "jpg":
            fmt = "jpeg"

        if fmt == "jpeg":
            if resized.mode in ("RGBA", "LA") or (
                "transparency" in getattr(resized, "info", {})
            ):
                bg = Image.new("RGB", resized.size, (255, 255, 255))
                bg.paste(
                    resized.convert("RGBA"), mask=resized.convert("RGBA").split()[-1]
                )
                resized = bg
            else:
                resized = resized.convert("RGB")

        out = BytesIO()
        resized.save(out, format=fmt.upper())
        return out.getvalue()


def _decode_write_and_downscale(
    items: List[Any],
    outputs: List[Path],
    *,
    force: bool,
    downscale_max_dim: Optional[int],
    downscale_suffix: str,
    output_format: str,
) -> None:
    for idx, item in enumerate(items):
        if idx >= len(outputs):
            break
        out_path = outputs[idx]
        if out_path.exists() and not force:
            _die(f"Output already exists: {out_path} (use --force to overwrite)")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        raw = _item_to_bytes(item)
        out_path.write_bytes(raw)
        print(f"Wrote {out_path}")

        if downscale_max_dim is None:
            continue

        derived = _derive_downscale_path(out_path, downscale_suffix)
        if derived.exists() and not force:
            _die(f"Output already exists: {derived} (use --force to overwrite)")
        derived.parent.mkdir(parents=True, exist_ok=True)
        resized = _downscale_image_bytes(
            raw, max_dim=downscale_max_dim, output_format=output_format
        )
        derived.write_bytes(resized)
        print(f"Wrote {derived}")


def _create_client():
    try:
        from openai import OpenAI
    except ImportError:
        _die(
            f"openai SDK not installed in the active environment. {_dependency_hint('openai')}"
        )
    kwargs: Dict[str, Any] = {"timeout": 120.0}
    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        kwargs["api_key"] = api_key
    return OpenAI(**kwargs)


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:60] if value else "job"


def _normalize_job(job: Any, idx: int) -> Dict[str, Any]:
    if isinstance(job, str):
        prompt = job.strip()
        if not prompt:
            _die(f"Empty prompt at job {idx}")
        return {"prompt": prompt}
    if isinstance(job, dict):
        if "prompt" not in job or not str(job["prompt"]).strip():
            _die(f"Missing prompt for job {idx}")
        return job
    _die(f"Invalid job at index {idx}: expected string or object.")
    return {}  # unreachable


def _read_jobs_jsonl(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        _die(f"Input file not found: {p}")
    jobs: List[Dict[str, Any]] = []
    for line_no, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item: Any
            if line.startswith("{"):
                item = json.loads(line)
            else:
                item = line
            jobs.append(_normalize_job(item, idx=line_no))
        except json.JSONDecodeError as exc:
            _die(f"Invalid JSON on line {line_no}: {exc}")
    if not jobs:
        _die("No jobs found in input file.")
    if len(jobs) > MAX_BATCH_JOBS:
        _die(f"Too many jobs ({len(jobs)}). Max is {MAX_BATCH_JOBS}.")
    return jobs


def _merge_non_null(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(dst)
    for k, v in src.items():
        if v is not None:
            merged[k] = v
    return merged


def _job_output_paths(
    *,
    out_dir: Path,
    output_format: str,
    idx: int,
    prompt: str,
    n: int,
    explicit_out: Optional[str],
) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = "." + output_format

    if explicit_out:
        base = Path(explicit_out)
        if base.suffix == "":
            base = base.with_suffix(ext)
        elif base.suffix.lstrip(".").lower() != output_format:
            _warn(
                f"Job {idx}: output extension {base.suffix} does not match output-format {output_format}."
            )
        base = out_dir / base.name
    else:
        slug = _slugify(prompt[:80])
        base = out_dir / f"{idx:03d}-{slug}{ext}"

    if n == 1:
        return [base]
    return [base.with_name(f"{base.stem}-{i}{base.suffix}") for i in range(1, n + 1)]


def _with_slot_model(
    payload: Dict[str, Any], pool: ProviderPool, slot: Any
) -> Dict[str, Any]:
    """Copy a request payload and bind it to the slot's model."""
    call_payload = dict(payload)
    call_payload["model"] = pool.model_for(slot)
    return call_payload


async def _invoke_with_pool(
    *,
    pool: ProviderPool,
    job_label: str,
    build_payload: Any,
    call: Any,
) -> Any:
    """Run one API call, retrying on other slots until it succeeds.

    ``build_payload(slot)`` returns the kwargs for a single attempt and
    ``call(client, payload)`` performs the awaitable request. Retries prefer a
    slot this job has not tried yet, so a throttled key is skipped instead of
    being hit again while other keys sit idle.
    """

    attempts = max(1, pool.config.max_attempts)
    total_slots = len(pool.slots)
    tried: set[str] = set()
    last_exc: Optional[Exception] = None
    attempt = 0

    for attempt in range(1, attempts + 1):
        exclude = tried if len(tried) < total_slots else None
        try:
            slot = await pool.acquire(exclude=exclude)
        except PoolUnavailableError as exc:
            raise RuntimeError(f"{job_label} {exc}") from exc

        tried.add(slot.name)
        call_payload = build_payload(slot)
        started = time.time()
        try:
            client = pool.async_client(slot)
            result = await call(client, call_payload)
        except Exception as exc:  # noqa: BLE001 - classified just below
            elapsed = time.time() - started
            outcome = classify_error(exc)
            pool.release(slot, outcome, error=exc, latency=elapsed)
            last_exc = exc
            if outcome == "fatal":
                raise RuntimeError(
                    f"{job_label} fatal error on slot {slot.name}: {exc}"
                ) from exc
            print(
                f"{job_label} attempt {attempt}/{attempts} on slot {slot.name} "
                f"failed ({outcome}: {exc.__class__.__name__}); switching slot",
                file=sys.stderr,
            )
            continue
        pool.release(slot, "ok", latency=time.time() - started)
        return result

    raise RuntimeError(
        f"{job_label} failed after {attempt} attempt(s): {last_exc}"
    )


def _resolve_batch_pool(
    args: argparse.Namespace, *, required: bool = True
) -> Optional[ProviderPool]:
    """Pick the pool for batch work: configured pool, else an env fallback.

    With no ``providers.toml`` this builds a single-slot pool from
    ``OPENAI_API_KEY``, which keeps the original single-provider behavior.
    """

    pool = getattr(args, "pool", None)
    if pool is not None:
        return pool

    concurrency = (
        args.concurrency if args.concurrency is not None else DEFAULT_CONCURRENCY
    )
    attempts = (
        args.max_attempts if args.max_attempts is not None else DEFAULT_BATCH_ATTEMPTS
    )
    pool = build_env_fallback_pool(
        model=args.model,
        max_concurrency=concurrency,
        max_attempts=attempts,
    )
    if pool is None and required:
        _die(
            "No credentials for batch mode: set OPENAI_API_KEY or add providers.toml."
        )
    return pool


def _add_pool_preview(
    preview: Dict[str, Any], pool: Optional[ProviderPool]
) -> None:
    """Annotate a dry-run payload with the slot it would be routed to."""

    if pool is None:
        return
    slot = pool.pick_preview()
    if slot is not None:
        preview["provider_slot"] = slot.name
        preview["model"] = pool.model_for(slot)


def _print_pool_summary(pool: Optional[ProviderPool]) -> None:
    if pool is None:
        return
    for line in pool.summary_lines():
        print(line, file=sys.stderr)


async def _run_generate_batch(args: argparse.Namespace) -> int:
    jobs = _read_jobs_jsonl(args.input)
    out_dir = Path(args.out_dir)

    base_fields = _fields_from_args(args)
    base_payload = {
        "model": args.model,
        "n": args.n,
        "size": args.size,
        "quality": args.quality,
        "background": args.background,
        "output_format": args.output_format,
        "output_compression": args.output_compression,
        "moderation": args.moderation,
    }

    pool = _resolve_batch_pool(args, required=not args.dry_run)

    if args.dry_run:
        for i, job in enumerate(jobs, start=1):
            prompt = str(job["prompt"]).strip()
            fields = _merge_non_null(base_fields, job.get("fields", {}))
            # Allow flat job keys as well (use_case, scene, etc.)
            fields = _merge_non_null(
                fields, {k: job.get(k) for k in base_fields.keys()}
            )
            augmented = _augment_prompt_fields(args.augment, prompt, fields)

            job_payload = dict(base_payload)
            job_payload["prompt"] = augmented
            job_payload = _merge_non_null(
                job_payload, {k: job.get(k) for k in base_payload.keys()}
            )
            job_payload = {k: v for k, v in job_payload.items() if v is not None}

            _validate_generate_payload(job_payload)
            effective_output_format = _normalize_output_format(
                job_payload.get("output_format")
            )
            _validate_transparency(
                job_payload.get("background"), effective_output_format
            )
            job_payload["output_format"] = effective_output_format

            n = int(job_payload.get("n", 1))
            outputs = _job_output_paths(
                out_dir=out_dir,
                output_format=effective_output_format,
                idx=i,
                prompt=prompt,
                n=n,
                explicit_out=job.get("out"),
            )
            downscaled = None
            if args.downscale_max_dim is not None:
                downscaled = [
                    str(_derive_downscale_path(p, args.downscale_suffix))
                    for p in outputs
                ]
            preview = {
                "endpoint": "/v1/images/generations",
                "job": i,
                "outputs": [str(p) for p in outputs],
                "outputs_downscaled": downscaled,
                **job_payload,
            }
            _add_pool_preview(preview, pool)
            _print_request(preview)
        return 0

    gate = asyncio.Semaphore(max(1, pool.total_capacity()))

    any_failed = False

    async def run_job(i: int, job: Dict[str, Any]) -> Tuple[int, Optional[str]]:
        nonlocal any_failed
        prompt = str(job["prompt"]).strip()
        job_label = f"[job {i}/{len(jobs)}]"

        fields = _merge_non_null(base_fields, job.get("fields", {}))
        fields = _merge_non_null(fields, {k: job.get(k) for k in base_fields.keys()})
        augmented = _augment_prompt_fields(args.augment, prompt, fields)

        payload = dict(base_payload)
        payload["prompt"] = augmented
        payload = _merge_non_null(payload, {k: job.get(k) for k in base_payload.keys()})
        payload = {k: v for k, v in payload.items() if v is not None}

        n = int(payload.get("n", 1))
        _validate_generate_payload(payload)
        effective_output_format = _normalize_output_format(payload.get("output_format"))
        _validate_transparency(payload.get("background"), effective_output_format)
        payload["output_format"] = effective_output_format
        outputs = _job_output_paths(
            out_dir=out_dir,
            output_format=effective_output_format,
            idx=i,
            prompt=prompt,
            n=n,
            explicit_out=job.get("out"),
        )
        try:
            async with gate:
                print(f"{job_label} starting", file=sys.stderr)
                started = time.time()
                result = await _invoke_with_pool(
                    pool=pool,
                    job_label=job_label,
                    build_payload=lambda slot: _with_slot_model(payload, pool, slot),
                    call=lambda client, request: client.images.generate(**request),
                )
                elapsed = time.time() - started
                print(f"{job_label} completed in {elapsed:.1f}s", file=sys.stderr)
            _decode_write_and_downscale(
                list(result.data),
                outputs,
                force=args.force,
                downscale_max_dim=args.downscale_max_dim,
                downscale_suffix=args.downscale_suffix,
                output_format=effective_output_format,
            )
            return i, None
        except Exception as exc:
            any_failed = True
            print(f"{job_label} failed: {exc}", file=sys.stderr)
            if args.fail_fast:
                raise
            return i, str(exc)

    tasks = [
        asyncio.create_task(run_job(i, job)) for i, job in enumerate(jobs, start=1)
    ]

    try:
        await asyncio.gather(*tasks)
    except Exception:
        for t in tasks:
            if not t.done():
                t.cancel()
        _print_pool_summary(pool)
        raise

    _print_pool_summary(pool)
    return 1 if any_failed else 0


def _generate_batch(args: argparse.Namespace) -> None:
    exit_code = asyncio.run(_run_generate_batch(args))
    if exit_code:
        raise SystemExit(exit_code)


def _generate(args: argparse.Namespace) -> None:
    prompt = _read_prompt(args.prompt, args.prompt_file)
    prompt = _augment_prompt(args, prompt)

    payload = {
        "model": args.model,
        "prompt": prompt,
        "n": args.n,
        "size": args.size,
        "quality": _normalize_quality_for_model(args.quality, args.model),
        "background": args.background,
        "output_format": args.output_format,
        "output_compression": args.output_compression,
        "moderation": args.moderation,
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    output_format = _normalize_output_format(args.output_format)
    _validate_transparency(args.background, output_format)
    payload["output_format"] = output_format
    output_paths = _build_output_paths(args.out, output_format, args.n, args.out_dir)
    downscaled = None
    if args.downscale_max_dim is not None:
        downscaled = [
            str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths
        ]

    if args.dry_run:
        preview = {
            "endpoint": "/v1/images/generations",
            "outputs": [str(p) for p in output_paths],
            "outputs_downscaled": downscaled,
            **payload,
        }
        _add_pool_preview(preview, getattr(args, "pool", None))
        _print_request(preview)
        return

    pool = getattr(args, "pool", None)
    if pool is not None:
        asyncio.run(_run_generate_via_pool(args, pool, payload, output_paths))
        _print_pool_summary(pool)
        return

    print(
        "Calling Image API (generation). This can take up to a couple of minutes.",
        file=sys.stderr,
    )
    started = time.time()
    client = _create_client()
    result = client.images.generate(**payload)
    elapsed = time.time() - started
    print(f"Generation completed in {elapsed:.1f}s.", file=sys.stderr)

    _decode_write_and_downscale(
        list(result.data),
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        output_format=output_format,
    )


def _edit(args: argparse.Namespace) -> None:
    prompt = _read_prompt(args.prompt, args.prompt_file)
    prompt = _augment_prompt(args, prompt)

    image_paths = _check_image_paths(args.image)
    mask_path = Path(args.mask) if args.mask else None
    if mask_path:
        if not mask_path.exists():
            _die(f"Mask file not found: {mask_path}")
        if mask_path.suffix.lower() != ".png":
            _warn(f"Mask should be a PNG with an alpha channel: {mask_path}")
        if mask_path.stat().st_size > MAX_IMAGE_BYTES:
            _warn(f"Mask exceeds 50MB limit: {mask_path}")

    payload = {
        "model": args.model,
        "prompt": prompt,
        "n": args.n,
        "size": args.size,
        "quality": args.quality,
        "background": args.background,
        "output_format": args.output_format,
        "output_compression": args.output_compression,
        "input_fidelity": args.input_fidelity,
        "moderation": args.moderation,
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    output_format = _normalize_output_format(args.output_format)
    _validate_transparency(args.background, output_format)
    payload["output_format"] = output_format
    _validate_input_fidelity(args.input_fidelity)
    output_paths = _build_output_paths(args.out, output_format, args.n, args.out_dir)
    downscaled = None
    if args.downscale_max_dim is not None:
        downscaled = [
            str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths
        ]

    if args.dry_run:
        payload_preview = dict(payload)
        payload_preview["image"] = [str(p) for p in image_paths]
        if mask_path:
            payload_preview["mask"] = str(mask_path)
        preview = {
            "endpoint": "/v1/images/edits",
            "outputs": [str(p) for p in output_paths],
            "outputs_downscaled": downscaled,
            **payload_preview,
        }
        _add_pool_preview(preview, getattr(args, "pool", None))
        _print_request(preview)
        return

    pool = getattr(args, "pool", None)
    if pool is not None:
        asyncio.run(
            _run_edit_via_pool(
                args, pool, payload, image_paths, mask_path, output_paths
            )
        )
        _print_pool_summary(pool)
        return

    print(
        f"Calling Image API (edit) with {len(image_paths)} image(s).",
        file=sys.stderr,
    )
    started = time.time()
    client = _create_client()

    with _open_files(image_paths) as image_files, _open_mask(mask_path) as mask_file:
        request = dict(payload)
        request["image"] = image_files if len(image_files) > 1 else image_files[0]
        if mask_file is not None:
            request["mask"] = mask_file
        result = client.images.edit(**request)

    elapsed = time.time() - started
    print(f"Edit completed in {elapsed:.1f}s.", file=sys.stderr)
    _decode_write_and_downscale(
        list(result.data),
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        output_format=output_format,
    )


async def _run_generate_via_pool(
    args: argparse.Namespace,
    pool: ProviderPool,
    payload: Dict[str, Any],
    output_paths: List[Path],
) -> None:
    """Single generation routed through the provider pool (one acquired slot)."""

    print(
        "Calling Image API (generation) through the provider pool.",
        file=sys.stderr,
    )
    started = time.time()
    result = await _invoke_with_pool(
        pool=pool,
        job_label="[generate]",
        build_payload=lambda slot: _with_slot_model(payload, pool, slot),
        call=lambda client, request: client.images.generate(**request),
    )
    print(f"Generation completed in {time.time() - started:.1f}s.", file=sys.stderr)

    _decode_write_and_downscale(
        list(result.data),
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        output_format=payload["output_format"],
    )


async def _run_edit_via_pool(
    args: argparse.Namespace,
    pool: ProviderPool,
    payload: Dict[str, Any],
    image_paths: List[Path],
    mask_path: Optional[Path],
    output_paths: List[Path],
) -> None:
    """Single edit routed through the provider pool (one acquired slot)."""

    print(
        f"Calling Image API (edit) through the provider pool with "
        f"{len(image_paths)} image(s).",
        file=sys.stderr,
    )

    async def call(client: Any, request_payload: Dict[str, Any]) -> Any:
        # Re-open the files for every attempt: a retry runs on another slot.
        with _open_files(image_paths) as image_files, _open_mask(mask_path) as mask_file:
            request = dict(request_payload)
            request["image"] = (
                image_files if len(image_files) > 1 else image_files[0]
            )
            if mask_file is not None:
                request["mask"] = mask_file
            return await client.images.edit(**request)

    started = time.time()
    result = await _invoke_with_pool(
        pool=pool,
        job_label="[edit]",
        build_payload=lambda slot: _with_slot_model(payload, pool, slot),
        call=call,
    )
    print(f"Edit completed in {time.time() - started:.1f}s.", file=sys.stderr)

    _decode_write_and_downscale(
        list(result.data),
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        output_format=payload["output_format"],
    )


def _open_files(paths: List[Path]):
    return _FileBundle(paths)


def _open_mask(mask_path: Optional[Path]):
    if mask_path is None:
        return _NullContext()
    return _SingleFile(mask_path)


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


class _SingleFile:
    def __init__(self, path: Path):
        self._path = path
        self._handle = None

    def __enter__(self):
        self._handle = self._path.open("rb")
        return self._handle

    def __exit__(self, exc_type, exc, tb):
        if self._handle:
            try:
                self._handle.close()
            except Exception:
                pass
        return False


class _FileBundle:
    def __init__(self, paths: List[Path]):
        self._paths = paths
        self._handles: List[object] = []

    def __enter__(self):
        self._handles = [p.open("rb") for p in self._paths]
        return self._handles

    def __exit__(self, exc_type, exc, tb):
        for handle in self._handles:
            try:
                handle.close()
            except Exception:
                pass
        return False


def _add_shared_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=None)
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--size", default=DEFAULT_SIZE)
    parser.add_argument("--quality", default=DEFAULT_QUALITY)
    parser.add_argument("--background")
    parser.add_argument("--output-format")
    parser.add_argument("--output-compression", type=int)
    parser.add_argument("--moderation")
    parser.add_argument("--out", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--out-dir")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--providers", help="Path to a providers.toml pool config."
    )
    parser.add_argument(
        "--no-pool",
        action="store_true",
        help="Ignore providers.toml and use a single provider.",
    )
    parser.add_argument(
        "--sticky",
        action="store_true",
        help="Pin the whole run to one provider slot (consistent style).",
    )
    parser.add_argument(
        "--sticky-slot",
        help="Pin the run to this slot by name (implies --sticky).",
    )
    parser.add_argument("--augment", dest="augment", action="store_true")
    parser.add_argument("--no-augment", dest="augment", action="store_false")
    parser.set_defaults(augment=True)

    # Prompt augmentation hints
    parser.add_argument("--use-case")
    parser.add_argument("--scene")
    parser.add_argument("--subject")
    parser.add_argument("--style")
    parser.add_argument("--composition")
    parser.add_argument("--lighting")
    parser.add_argument("--palette")
    parser.add_argument("--materials")
    parser.add_argument("--text")
    parser.add_argument("--constraints")
    parser.add_argument("--negative")

    # Post-processing (optional): generate an additional downscaled copy for fast web loading.
    parser.add_argument("--downscale-max-dim", type=int)
    parser.add_argument("--downscale-suffix", default=DEFAULT_DOWNSCALE_SUFFIX)


def main() -> int:
    _load_provider_env()
    parser = argparse.ArgumentParser(
        description="CLI for image generation or editing via Grok Imagine / OpenAI-compatible providers"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen_parser = subparsers.add_parser("generate", help="Create a new image")
    _add_shared_args(gen_parser)
    gen_parser.set_defaults(func=_generate)

    batch_parser = subparsers.add_parser(
        "generate-batch",
        help="Generate multiple prompts concurrently (JSONL input)",
    )
    _add_shared_args(batch_parser)
    batch_parser.add_argument(
        "--input", required=True, help="Path to JSONL file (one job per line)"
    )
    batch_parser.add_argument("--concurrency", type=int, default=None)
    batch_parser.add_argument("--max-attempts", type=int, default=None)
    batch_parser.add_argument("--fail-fast", action="store_true")
    batch_parser.set_defaults(func=_generate_batch)

    edit_parser = subparsers.add_parser("edit", help="Edit an existing image")
    _add_shared_args(edit_parser)
    edit_parser.add_argument("--image", action="append", required=True)
    edit_parser.add_argument("--mask")
    edit_parser.add_argument("--input-fidelity")
    edit_parser.set_defaults(func=_edit)

    args = parser.parse_args()

    # The multi-provider / multi-key pool is opt-in: it activates only when a
    # providers.toml is found (or --providers is given). --no-pool forces the
    # original single-provider path.
    pool_config: Optional[PoolConfig] = None
    if not args.no_pool:
        try:
            pool_config = load_pool_config(args.providers)
        except PoolConfigError as exc:
            _die(str(exc))

    # Resolve the model after loading the config so `defaults.model` can supply
    # it when --model is not passed.
    if args.model is None:
        if pool_config is not None:
            args.model = pool_config.default_model
        else:
            args.model = os.getenv("IMAGEGEN_MODEL") or DEFAULT_MODEL

    if pool_config is not None:
        if getattr(args, "max_attempts", None) is not None:
            pool_config.max_attempts = args.max_attempts
        if getattr(args, "concurrency", None) is not None:
            _warn(
                "--concurrency is ignored when providers.toml defines per-slot "
                "max_concurrency."
            )

    args.pool_config = pool_config
    args.pool = None
    if pool_config is not None:
        try:
            args.pool = ProviderPool(pool_config)
        except PoolConfigError as exc:
            _die(str(exc))

    # --sticky keeps the whole run on one slot so a batch stays stylistically
    # consistent, at the cost of not spreading load across keys.
    if args.sticky or args.sticky_slot:
        if args.pool is None:
            _warn(
                "--sticky has no effect without a providers.toml pool; "
                "a single provider already serves everything."
            )
        else:
            try:
                pinned = args.pool.pin(args.sticky_slot)
            except PoolConfigError as exc:
                _die(str(exc))
            print(
                f"Sticky mode: all work pinned to provider slot {pinned.name!r}.",
                file=sys.stderr,
            )

    if args.n < 1 or args.n > 10:
        _die("--n must be between 1 and 10")

    concurrency_arg = getattr(args, "concurrency", None)
    if concurrency_arg is not None and not (1 <= concurrency_arg <= 25):
        _die("--concurrency must be between 1 and 25")

    attempts_arg = getattr(args, "max_attempts", None)
    if attempts_arg is not None and not (1 <= attempts_arg <= 10):
        _die("--max-attempts must be between 1 and 10")

    if args.output_compression is not None and not (
        0 <= args.output_compression <= 100
    ):
        _die("--output-compression must be between 0 and 100")
    if args.command == "generate-batch" and not args.out_dir:
        _die("generate-batch requires --out-dir")
    if (
        getattr(args, "downscale_max_dim", None) is not None
        and args.downscale_max_dim < 1
    ):
        _die("--downscale-max-dim must be >= 1")

    _validate_model(args.model)
    _validate_size(args.size, args.model)
    _validate_quality(args.quality)
    _validate_background(args.background)
    _validate_model_specific_options(
        model=args.model,
        background=args.background,
        input_fidelity=getattr(args, "input_fidelity", None),
    )

    has_config_key = args.pool is not None and any(
        slot.api_key for slot in args.pool.slots
    )
    _ensure_api_key(args.dry_run, has_config_key=has_config_key)

    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
