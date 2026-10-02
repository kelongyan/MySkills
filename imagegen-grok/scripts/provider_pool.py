#!/usr/bin/env python3
"""Provider pool for the imagegen CLI: multi-provider / multi-key scheduling.

Spreads a high-volume image-generation workload over many API keys (and/or
many providers) instead of hammering a single key. Imported by
``scripts/image_gen.py``. Uses only the standard library (the ``openai`` SDK is
imported lazily, so dry-runs and unit tests need no extra dependency).

Core ideas
----------
- One config entry (``[[provider]]`` in ``providers.toml``) is one scheduling
  *slot*. Several keys pointing at the same ``base_url`` are simply several
  slots; different ``base_url`` values mean different providers.
- A job acquires a slot, calls the API, then reports the outcome. On a
  retryable failure it releases the slot and retries on a *different* slot,
  so a single throttled key cannot stall the batch.
- Selection uses smooth weighted round-robin (nginx-style): a slot with
  ``weight = 3`` serves roughly three times the traffic, spread evenly rather
  than in a burst.
- Every slot has its own concurrency cap and an exponential cooldown that
  engages after repeated failures and resets on success.

Config file: ``providers.toml``. It is looked up in the per-machine config
directory (``%APPDATA%/imagegen`` on Windows, ``$XDG_CONFIG_HOME/imagegen`` or
``~/.config/imagegen`` elsewhere), so the skill package itself never has to
carry credentials. ``--providers PATH`` and ``IMAGEGEN_PROVIDERS`` override the
lookup; a ``providers.toml`` next to the skill root still works for backward
compatibility, but shipping one with keys is discouraged. When no config file
exists, callers build a single-slot pool from ``OPENAI_API_KEY`` /
``OPENAI_BASE_URL`` via :func:`build_env_fallback_pool`, which reproduces the
original single-provider behavior.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - older interpreters
    tomllib = None  # type: ignore[assignment]


PROVIDERS_FILENAME = "providers.toml"
ENV_FILENAME = "provider.env"
PROVIDERS_ENV_VAR = "IMAGEGEN_PROVIDERS"
CONFIG_DIR_ENV_VAR = "IMAGEGEN_CONFIG_DIR"
CONFIG_DIR_NAME = "imagegen"

DEFAULT_STRATEGY = "weighted-round-robin"
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_COOLDOWN_BASE = 5.0
DEFAULT_COOLDOWN_MAX = 300.0
DEFAULT_FAIL_THRESHOLD = 2
DEFAULT_SLOT_CONCURRENCY = 4
DEFAULT_ACQUIRE_TIMEOUT = 600.0
DEFAULT_MODEL = "grok-imagine-image-quality"

ALLOWED_STRATEGIES = ("weighted-round-robin", "least-inflight")


class PoolConfigError(RuntimeError):
    """Raised when the provider pool configuration is missing or invalid."""


class PoolUnavailableError(RuntimeError):
    """Raised when no provider slot could be acquired within the timeout."""


@dataclass
class ProviderSlot:
    """One scheduling unit: a single API key on a single provider endpoint."""

    name: str
    api_key: str
    base_url: Optional[str] = None
    model: Optional[str] = None
    weight: int = 1
    max_concurrency: int = DEFAULT_SLOT_CONCURRENCY
    enabled: bool = True

    # Runtime state (never read from config).
    inflight: int = 0
    current_weight: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    ok_count: int = 0
    fail_count: int = 0
    fatal_count: int = 0
    auth_failures: int = 0
    total_latency: float = 0.0
    latency_samples: int = 0

    def average_latency(self) -> float:
        if self.latency_samples <= 0:
            return 0.0
        return self.total_latency / self.latency_samples

    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.monotonic())


@dataclass
class PoolConfig:
    """Pool-level knobs plus the configured slots."""

    strategy: str = DEFAULT_STRATEGY
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    cooldown_base_seconds: float = DEFAULT_COOLDOWN_BASE
    cooldown_max_seconds: float = DEFAULT_COOLDOWN_MAX
    fail_threshold: int = DEFAULT_FAIL_THRESHOLD
    default_model: str = DEFAULT_MODEL
    default_max_concurrency: int = DEFAULT_SLOT_CONCURRENCY
    acquire_timeout_seconds: float = DEFAULT_ACQUIRE_TIMEOUT
    slots: List[ProviderSlot] = field(default_factory=list)


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------

_AUTH_PATTERN = re.compile(
    r"\b(?:401|403)\b|unauthoriz|invalid[_ -]?api[_ -]?key|authentication|"
    r"permission denied|forbidden|insufficient[_ -]?quota|quota exceeded|billing",
    re.IGNORECASE,
)
_RETRY_PATTERN = re.compile(
    r"\b(?:408|409|425|429|500|502|503|504|529)\b|rate[_ -]?limit|"
    r"too many requests|overload|timeout|timed out|temporar|connection|"
    r"upstream|unavailable|bad gateway|internal server error",
    re.IGNORECASE,
)
_AUTH_EXC_NAMES = ("authenticationerror", "permissiondeniederror")
_RETRY_EXC_NAMES = (
    "ratelimiterror",
    "apitimeouterror",
    "apiconnectionerror",
    "internalservererror",
    "serviceunavailableerror",
    "apistatuserror",
)
_RETRY_AFTER_PATTERN = re.compile(
    r"retry[- _]?after[:= ]+([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE
)


def classify_error(exc: BaseException) -> str:
    """Map an exception to ``"auth"``, ``"retryable"`` or ``"fatal"``.

    ``auth``      - the key itself is unusable (401/403/quota); cool it down
                    hard and move to another key.
    ``retryable`` - transient (429/5xx/timeout/upstream); cool down and retry
                    on another slot.
    ``fatal``     - the request itself is wrong (400/404/422); retrying any
                    key would produce the same error, so fail the job.

    Matching is deliberately string/class-name based rather than tied to
    specific ``openai`` exception types, so OpenAI-compatible third-party
    providers are classified correctly too.
    """

    name = type(exc).__name__.lower()
    text = f"{type(exc).__name__}: {exc}"

    if name in _AUTH_EXC_NAMES or _AUTH_PATTERN.search(text):
        return "auth"
    if name in _RETRY_EXC_NAMES or _RETRY_PATTERN.search(text):
        return "retryable"
    return "fatal"


def _retry_after_seconds(exc: Optional[BaseException]) -> Optional[float]:
    """Best-effort extraction of a server-provided retry delay, in seconds."""

    if exc is None:
        return None

    for attr in ("retry_after", "retry_after_seconds"):
        value = getattr(exc, attr, None)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is not None and hasattr(headers, "get"):
        raw = headers.get("retry-after")
        if raw:
            try:
                return max(0.0, float(str(raw).strip()))
            except (TypeError, ValueError):
                pass

    match = _RETRY_AFTER_PATTERN.search(str(exc))
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------


def user_config_dir(env: Optional[Dict[str, str]] = None) -> Path:
    """Directory for per-machine config that must NOT ship with the skill.

    The skill package has to stay free of credentials, so the live
    ``providers.toml`` / ``provider.env`` belong here instead:

    - Windows: ``%APPDATA%/imagegen``
    - otherwise: ``$XDG_CONFIG_HOME/imagegen``, else ``~/.config/imagegen``
    """

    environment = os.environ if env is None else env

    xdg = environment.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / CONFIG_DIR_NAME
    if os.name == "nt":
        appdata = environment.get("APPDATA")
        if appdata:
            return Path(appdata) / CONFIG_DIR_NAME
    return Path.home() / ".config" / CONFIG_DIR_NAME


def _default_skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


_GENERIC_HOSTS = {"users", "home", "administrator", "desktop", "documents", "appdata"}


def host_profile(skill_root: Optional[Path] = None) -> Optional[str]:
    """Infer a per-agent profile name from where this copy of the skill lives.

    A copy installed under an agent's own tree — ``<host>/.workbuddy/skills/
    imagegen``, ``<host>/.claude/skills/imagegen``, ... — gets the profile
    ``workbuddy`` / ``claude``, so every agent environment can keep its own
    provider config. A plain checkout (``~/Desktop/imagegen``) has no profile
    and falls back to the shared config directory.
    """

    root = Path(skill_root) if skill_root is not None else _default_skill_root()
    parent = root.parent
    if parent.name.lower() not in ("skills", "skill"):
        return None

    host = parent.parent.name.strip().lstrip(".").lower()
    if not host or host in _GENERIC_HOSTS:
        return None
    return host


def config_dir_candidates(
    env: Optional[Dict[str, str]] = None,
    skill_root: Optional[Path] = None,
) -> List[Path]:
    """Directories searched for ``providers.toml`` / ``provider.env``, in order.

    1. ``IMAGEGEN_CONFIG_DIR`` — explicit per-environment directory
    2. ``<shared config dir>/<host profile>`` — automatic per-environment dir,
       so each agent (workbuddy, claude, opencode, ...) keeps its own providers
    3. the shared config directory itself, used by the source checkout and as a
       fallback for environments that have no file of their own
    """

    environment = os.environ if env is None else env
    directories: List[Path] = []

    explicit = environment.get(CONFIG_DIR_ENV_VAR)
    if explicit:
        directories.append(Path(explicit))

    profile = host_profile(skill_root)
    if profile:
        directories.append(user_config_dir(environment) / profile)

    shared = user_config_dir(environment)
    if shared not in directories:
        directories.append(shared)

    return directories


def find_providers_path(
    explicit: Optional[str] = None,
    *,
    skill_root: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> Optional[Path]:
    """Resolve which ``providers.toml`` to use, or ``None`` if there is none.

    Precedence: explicit ``--providers`` flag, then ``IMAGEGEN_PROVIDERS``,
    then the directories from :func:`config_dir_candidates` (per-environment
    first, shared last), then a ``providers.toml`` next to the skill root for
    backward compatibility. An explicitly requested path that does not exist
    is an error rather than a silent fallback.
    """

    environment = os.environ if env is None else env

    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise PoolConfigError(f"providers config not found: {path}")
        return path

    from_env = environment.get(PROVIDERS_ENV_VAR)
    if from_env:
        path = Path(from_env)
        if not path.exists():
            raise PoolConfigError(
                f"{PROVIDERS_ENV_VAR} points to a missing file: {path}"
            )
        return path

    for directory in config_dir_candidates(environment, skill_root):
        candidate = directory / PROVIDERS_FILENAME
        if candidate.exists():
            return candidate

    # Legacy location. Kept working, but the skill package should not carry
    # credentials, so prefer the per-machine directories above.
    root = (
        Path(skill_root) if skill_root is not None else _default_skill_root()
    )
    candidate = root / PROVIDERS_FILENAME
    if candidate.exists():
        return candidate
    return None


def load_pool_config(
    explicit: Optional[str] = None,
    *,
    skill_root: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> Optional[PoolConfig]:
    """Load and validate the pool config, or return ``None`` when absent."""

    path = find_providers_path(explicit, skill_root=skill_root, env=env)
    if path is None:
        return None

    if tomllib is None:  # pragma: no cover - py<3.11
        raise PoolConfigError(
            "reading providers.toml requires Python 3.11+ (stdlib tomllib)."
        )

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise PoolConfigError(f"could not read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:  # type: ignore[union-attr]
        raise PoolConfigError(f"invalid TOML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise PoolConfigError(f"{path}: top level must be a TOML table.")

    return _config_from_mapping(data, path, env)


def _config_from_mapping(
    data: Dict[str, Any], source: Path, env: Optional[Dict[str, str]]
) -> PoolConfig:
    environment = os.environ if env is None else env

    pool_section = _as_mapping(data.get("pool"), "pool", source)
    defaults = _as_mapping(data.get("defaults"), "defaults", source)

    strategy = str(pool_section.get("strategy", DEFAULT_STRATEGY))
    if strategy not in ALLOWED_STRATEGIES:
        raise PoolConfigError(
            f"{source}: pool.strategy must be one of {list(ALLOWED_STRATEGIES)}, "
            f"got {strategy!r}."
        )

    config = PoolConfig(
        strategy=strategy,
        max_attempts=_positive_int(
            pool_section.get("max_attempts"), DEFAULT_MAX_ATTEMPTS,
            "pool.max_attempts", source,
        ),
        cooldown_base_seconds=_positive_float(
            pool_section.get("cooldown_base_seconds"), DEFAULT_COOLDOWN_BASE,
            "pool.cooldown_base_seconds", source,
        ),
        cooldown_max_seconds=_positive_float(
            pool_section.get("cooldown_max_seconds"), DEFAULT_COOLDOWN_MAX,
            "pool.cooldown_max_seconds", source,
        ),
        fail_threshold=_positive_int(
            pool_section.get("fail_threshold"), DEFAULT_FAIL_THRESHOLD,
            "pool.fail_threshold", source,
        ),
        acquire_timeout_seconds=_positive_float(
            pool_section.get("acquire_timeout_seconds"), DEFAULT_ACQUIRE_TIMEOUT,
            "pool.acquire_timeout_seconds", source,
        ),
        default_model=str(defaults.get("model", DEFAULT_MODEL)),
        default_max_concurrency=_positive_int(
            defaults.get("max_concurrency"), DEFAULT_SLOT_CONCURRENCY,
            "defaults.max_concurrency", source,
        ),
    )

    if config.cooldown_max_seconds < config.cooldown_base_seconds:
        raise PoolConfigError(
            f"{source}: pool.cooldown_max_seconds must be >= "
            "pool.cooldown_base_seconds."
        )

    raw_providers = data.get("provider")
    if not isinstance(raw_providers, list) or not raw_providers:
        raise PoolConfigError(
            f"{source}: at least one [[provider]] entry is required."
        )

    seen: Set[str] = set()
    slots: List[ProviderSlot] = []
    for index, entry in enumerate(raw_providers, start=1):
        slots.append(
            _slot_from_mapping(entry, index, config, source, environment, seen)
        )

    config.slots = slots
    if not any(slot.enabled for slot in slots):
        raise PoolConfigError(
            f"{source}: every provider has enabled = false; nothing to schedule."
        )
    return config


def _slot_from_mapping(
    entry: Any,
    index: int,
    config: PoolConfig,
    source: Path,
    env: Dict[str, str],
    seen: Set[str],
) -> ProviderSlot:
    if not isinstance(entry, dict):
        raise PoolConfigError(f"{source}: provider #{index} must be a table.")

    name = str(entry.get("name") or f"slot{index}").strip() or f"slot{index}"
    if name in seen:
        raise PoolConfigError(f"{source}: duplicate provider name {name!r}.")
    seen.add(name)

    enabled = bool(entry.get("enabled", True))

    api_key = entry.get("api_key")
    api_key_env = entry.get("api_key_env")
    if not api_key and api_key_env:
        api_key = env.get(str(api_key_env))
        if not api_key and enabled:
            raise PoolConfigError(
                f"{source}: provider {name!r} references api_key_env="
                f"{api_key_env!r}, but that environment variable is not set."
            )
    if not api_key:
        if enabled:
            raise PoolConfigError(
                f"{source}: provider {name!r} needs either api_key or api_key_env."
            )
        # Disabled slots stay in the file without carrying a live secret.
        api_key = ""

    base_url = entry.get("base_url")
    base_url_env = entry.get("base_url_env")
    if not base_url and base_url_env:
        base_url = env.get(str(base_url_env))
    base_url = str(base_url).strip() or None if base_url is not None else None

    model = entry.get("model")
    model = str(model).strip() or None if model is not None else None

    return ProviderSlot(
        name=name,
        api_key=str(api_key),
        base_url=base_url,
        model=model,
        weight=_positive_int(
            entry.get("weight"), 1, f"provider {name!r}.weight", source
        ),
        max_concurrency=_positive_int(
            entry.get("max_concurrency"), config.default_max_concurrency,
            f"provider {name!r}.max_concurrency", source,
        ),
        enabled=enabled,
    )


def _as_mapping(value: Any, key: str, source: Path) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PoolConfigError(f"{source}: [{key}] must be a table.")
    return value


def _positive_int(value: Any, default: int, label: str, source: Path) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PoolConfigError(f"{source}: {label} must be an integer.") from exc
    if number < 1:
        raise PoolConfigError(f"{source}: {label} must be >= 1.")
    return number


def _positive_float(value: Any, default: float, label: str, source: Path) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PoolConfigError(f"{source}: {label} must be a number.") from exc
    if number <= 0:
        raise PoolConfigError(f"{source}: {label} must be > 0.")
    return number


def build_env_fallback_pool(
    *,
    model: str,
    max_concurrency: int = DEFAULT_SLOT_CONCURRENCY,
    max_attempts: int = 3,
    env: Optional[Dict[str, str]] = None,
) -> Optional["ProviderPool"]:
    """Build a single-slot pool from ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``.

    Returns ``None`` when no API key is available, so callers can keep their
    existing "missing credentials" error path.
    """

    environment = os.environ if env is None else env
    api_key = environment.get("OPENAI_API_KEY")
    if not api_key:
        return None

    slot = ProviderSlot(
        name="default",
        api_key=api_key,
        base_url=environment.get("OPENAI_BASE_URL") or None,
        model=model,
        weight=1,
        max_concurrency=max(1, int(max_concurrency)),
    )
    config = PoolConfig(
        strategy=DEFAULT_STRATEGY,
        max_attempts=max(1, int(max_attempts)),
        default_model=model,
        default_max_concurrency=max(1, int(max_concurrency)),
        slots=[slot],
    )
    return ProviderPool(config)


# --------------------------------------------------------------------------
# The pool itself
# --------------------------------------------------------------------------


class ProviderPool:
    """Schedules API calls across the enabled slots of a :class:`PoolConfig`."""

    def __init__(self, config: PoolConfig):
        self.config = config
        self._slots = [slot for slot in config.slots if slot.enabled]
        if not self._slots:
            raise PoolConfigError("provider pool has no enabled slots.")
        self._async_clients: Dict[str, Any] = {}
        self._sync_clients: Dict[str, Any] = {}
        self._pinned_name: Optional[str] = None

    # -- introspection ----------------------------------------------------

    @property
    def slots(self) -> List[ProviderSlot]:
        return list(self._slots)

    @property
    def pinned_name(self) -> Optional[str]:
        return self._pinned_name

    def total_capacity(self) -> int:
        slots = self._slots
        if self._pinned_name is not None:
            slots = [slot for slot in slots if slot.name == self._pinned_name]
        return sum(max(1, slot.max_concurrency) for slot in slots)

    def model_for(self, slot: ProviderSlot) -> str:
        return slot.model or self.config.default_model

    # -- sticky mode ------------------------------------------------------

    def pin(self, slot_name: Optional[str] = None) -> ProviderSlot:
        """Restrict all scheduling to a single slot.

        Used by ``--sticky`` to keep a whole batch on one key, so the outputs
        stay consistent in style/parameters. While pinned, a cooling slot is
        waited for rather than silently swapped for another one.

        With no ``slot_name``, one slot is chosen by the normal weighted rule.
        """

        if slot_name:
            for slot in self._slots:
                if slot.name == slot_name:
                    self._pinned_name = slot.name
                    return slot
            available = ", ".join(slot.name for slot in self._slots)
            raise PoolConfigError(
                f"unknown provider slot {slot_name!r}; available slots: {available}"
            )

        chosen = self._select()
        if chosen is None:
            # Everything is busy or cooling down; pin the heaviest slot.
            chosen = max(self._slots, key=lambda slot: slot.weight)
        self._pinned_name = chosen.name
        return chosen

    def unpin(self) -> None:
        self._pinned_name = None

    # -- clients ----------------------------------------------------------

    def _client_kwargs(self, slot: ProviderSlot) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"api_key": slot.api_key, "timeout": 120.0}
        if slot.base_url:
            kwargs["base_url"] = slot.base_url
        return kwargs

    def _new_client(self, slot: ProviderSlot, *, is_async: bool) -> Any:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on env
            raise PoolConfigError(
                "openai SDK is not installed in the active environment; "
                "install it with `uv pip install openai`."
            ) from exc
        factory = openai.AsyncOpenAI if is_async else openai.OpenAI
        return factory(**self._client_kwargs(slot))

    def async_client(self, slot: ProviderSlot) -> Any:
        client = self._async_clients.get(slot.name)
        if client is None:
            client = self._new_client(slot, is_async=True)
            self._async_clients[slot.name] = client
        return client

    def sync_client(self, slot: ProviderSlot) -> Any:
        client = self._sync_clients.get(slot.name)
        if client is None:
            client = self._new_client(slot, is_async=False)
            self._sync_clients[slot.name] = client
        return client

    # -- scheduling -------------------------------------------------------

    def ready_slots(self, exclude: Optional[Set[str]] = None) -> List[ProviderSlot]:
        """Slots that are not cooling down and have a free concurrency slot."""

        slots = self._slots
        if self._pinned_name is not None:
            slots = [slot for slot in slots if slot.name == self._pinned_name]

        now = time.monotonic()
        candidates = [
            slot
            for slot in slots
            if slot.cooldown_until <= now and slot.inflight < slot.max_concurrency
        ]
        if exclude:
            narrowed = [slot for slot in candidates if slot.name not in exclude]
            if narrowed:
                # Only fall back to already-tried slots when nothing else is
                # available, so single-slot pools (and pinned mode) can retry.
                return narrowed
        return candidates

    def _select(self, exclude: Optional[Set[str]] = None) -> Optional[ProviderSlot]:
        candidates = self.ready_slots(exclude)
        if not candidates:
            return None

        if self.config.strategy == "least-inflight":
            return min(candidates, key=lambda slot: (slot.inflight, -slot.weight))

        # Smooth weighted round-robin (nginx-style).
        weights = {id(slot): max(1, slot.weight) for slot in candidates}
        total = sum(weights.values())
        for slot in candidates:
            slot.current_weight += weights[id(slot)]
        chosen = max(candidates, key=lambda slot: slot.current_weight)
        chosen.current_weight -= total
        return chosen

    async def acquire(
        self,
        *,
        exclude: Optional[Set[str]] = None,
        timeout: Optional[float] = None,
    ) -> ProviderSlot:
        """Reserve a ready slot, waiting briefly if every slot is busy.

        Raises :class:`PoolUnavailableError` if nothing frees up in time.
        """

        limit = self.config.acquire_timeout_seconds if timeout is None else timeout
        deadline = time.monotonic() + limit if limit and limit > 0 else None
        delay = 0.02

        while True:
            slot = self._select(exclude)
            if slot is not None:
                slot.inflight += 1
                return slot

            if deadline is None:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.25)
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PoolUnavailableError(self._unavailable_message(exclude))
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.25)

    def release(
        self,
        slot: ProviderSlot,
        outcome: str,
        *,
        error: Optional[BaseException] = None,
        latency: float = 0.0,
    ) -> None:
        """Return a slot to the pool and update its health state.

        ``outcome`` is one of ``"ok"``, ``"retryable"``, ``"auth"`` or
        ``"fatal"``. A ``fatal`` outcome comes from the request itself rather
        than the slot, so it is recorded without penalising the slot *and*
        without resetting its failure streak — a bad request must not launder
        an unhealthy key.
        """

        slot.inflight = max(0, slot.inflight - 1)

        if latency > 0:
            slot.total_latency += latency
            slot.latency_samples += 1

        if outcome == "ok":
            slot.ok_count += 1
            slot.consecutive_failures = 0
            slot.cooldown_until = 0.0
            return

        if outcome == "fatal":
            slot.fatal_count += 1
            return

        slot.fail_count += 1
        slot.consecutive_failures += 1

        if outcome == "auth":
            slot.auth_failures += 1
            cooldown = self.config.cooldown_max_seconds
        else:
            cooldown = 0.0
            if slot.consecutive_failures >= self.config.fail_threshold:
                exponent = slot.consecutive_failures - self.config.fail_threshold
                cooldown = min(
                    self.config.cooldown_base_seconds * (2 ** exponent),
                    self.config.cooldown_max_seconds,
                )

        retry_after = _retry_after_seconds(error)
        if retry_after is not None:
            cooldown = max(cooldown, retry_after)

        if cooldown > 0:
            slot.cooldown_until = time.monotonic() + cooldown

    def pick_preview(self) -> Optional[ProviderSlot]:
        """Choose a slot for a dry-run preview without reserving capacity."""

        return self._select()

    def _unavailable_message(self, exclude: Optional[Set[str]]) -> str:
        now = time.monotonic()
        cooling = [
            f"{slot.name} ({slot.cooldown_remaining():.0f}s)"
            for slot in self._slots
            if slot.cooldown_until > now
        ]
        busy = [
            slot.name
            for slot in self._slots
            if slot.inflight >= slot.max_concurrency
        ]
        parts: List[str] = []
        if cooling:
            parts.append("cooling: " + ", ".join(cooling))
        if busy:
            parts.append("at concurrency cap: " + ", ".join(busy))
        detail = "; ".join(parts) if parts else "all slots busy"
        if self._pinned_name is not None:
            detail += f"; sticky mode is pinned to {self._pinned_name!r}"
        if exclude:
            detail += f"; already tried: {', '.join(sorted(exclude))}"
        return f"no provider slot available ({detail})"

    # -- reporting --------------------------------------------------------

    def summary_lines(self) -> List[str]:
        lines = ["provider pool summary:"]
        for slot in self.config.slots:
            if not slot.enabled:
                lines.append(f"  {slot.name:<14} disabled")
                continue
            state = "ready"
            remaining = slot.cooldown_remaining()
            if remaining > 0:
                state = f"cooldown {remaining:.0f}s"
            if slot.name == self._pinned_name:
                state += " [pinned]"
            lines.append(
                f"  {slot.name:<14} ok {slot.ok_count:<4} fail {slot.fail_count:<4} "
                f"auth {slot.auth_failures:<3} fatal {slot.fatal_count:<3} "
                f"avg {slot.average_latency():6.1f}s  "
                f"w={slot.weight} conc={slot.max_concurrency}  {state}"
            )
        total_ok = sum(slot.ok_count for slot in self.config.slots)
        total_bad = sum(
            slot.fail_count + slot.fatal_count for slot in self.config.slots
        )
        lines.append(
            f"  total: {total_ok} ok / {total_bad} failed "
            f"across {len(self._slots)} active slot(s)"
        )
        return lines
