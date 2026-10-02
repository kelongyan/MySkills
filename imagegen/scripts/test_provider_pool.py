#!/usr/bin/env python3
"""Self-checks for the provider pool. No network, no API calls, no keys.

Run after touching ``provider_pool.py`` or the pool wiring in
``image_gen.py``:

    python scripts/test_provider_pool.py

Covers: error classification, weighted round-robin distribution, per-slot
concurrency caps, cooldown + recovery, auth vs fatal handling, cross-slot
retry, and providers.toml parsing. Exit code is non-zero on the first failure.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provider_pool as pp  # noqa: E402


def test_classify() -> None:
    class RateLimitError(Exception):
        pass

    class AuthenticationError(Exception):
        pass

    class BadRequestError(Exception):
        pass

    assert pp.classify_error(RateLimitError("429 too many requests")) == "retryable"
    assert pp.classify_error(AuthenticationError("401 invalid api key")) == "auth"
    assert pp.classify_error(BadRequestError("400 bad size")) == "fatal"
    assert pp.classify_error(Exception("503 upstream_unavailable")) == "retryable"
    assert pp.classify_error(
        Exception("insufficient_quota: you exceeded your current quota")
    ) == "auth"
    assert pp.classify_error(Exception("something odd")) == "fatal"
    print("  classify: OK")


def test_weighted_round_robin() -> None:
    cfg = pp.PoolConfig(slots=[
        pp.ProviderSlot(name="a", api_key="x", weight=3),
        pp.ProviderSlot(name="b", api_key="x", weight=2),
        pp.ProviderSlot(name="c", api_key="x", weight=1),
    ])
    pool = pp.ProviderPool(cfg)

    picks = []
    for _ in range(600):
        slot = pool._select()
        assert slot is not None
        picks.append(slot.name)
        pool.release(slot, "ok")

    counts = Counter(picks)
    ratio = counts["a"] / counts["c"]
    assert counts["a"] > counts["b"] > counts["c"], counts
    assert 2.4 < ratio < 3.6, ratio

    run = 1
    for i in range(1, len(picks)):
        run = run + 1 if picks[i] == picks[i - 1] else 1
        assert run <= 3, f"burst too long near index {i}"
    print(f"  weighted round-robin: OK {dict(counts)} a/c={ratio:.2f}")


def test_concurrency_and_cooldown() -> None:
    cfg = pp.PoolConfig(
        fail_threshold=2,
        cooldown_base_seconds=0.4,
        cooldown_max_seconds=1.2,
        acquire_timeout_seconds=0.3,
        slots=[pp.ProviderSlot(name="a", api_key="x", max_concurrency=1)],
    )
    pool = pp.ProviderPool(cfg)

    async def scenario() -> None:
        first = await pool.acquire()
        assert first.inflight == 1

        async def release_soon() -> None:
            await asyncio.sleep(0.1)
            pool.release(first, "ok")

        task = asyncio.create_task(release_soon())
        second = await pool.acquire(timeout=2)
        assert second is first, "second acquire must wait for the only slot"
        await task
        pool.release(second, "ok")
        assert pool.total_capacity() == 1

        slot = await pool.acquire()
        pool.release(slot, "retryable", error=Exception("503 unavailable"))
        slot = await pool.acquire()
        pool.release(slot, "retryable", error=Exception("503 unavailable"))
        assert slot.cooldown_remaining() > 0, "slot should be cooling down"

        try:
            await pool.acquire()
            raise AssertionError("acquire should fail while cooling down")
        except pp.PoolUnavailableError:
            pass

        await asyncio.sleep(0.5)
        slot = await pool.acquire(timeout=2)
        assert slot.cooldown_remaining() == 0
        pool.release(slot, "ok")
        assert slot.consecutive_failures == 0, "success must reset the counter"

    asyncio.run(scenario())
    print("  concurrency + cooldown: OK")


def test_auth_cooldown_and_fatal() -> None:
    cfg = pp.PoolConfig(
        cooldown_base_seconds=0.2,
        cooldown_max_seconds=0.4,
        acquire_timeout_seconds=1.0,
        slots=[pp.ProviderSlot(name="a", api_key="x")],
    )
    pool = pp.ProviderPool(cfg)

    async def scenario() -> None:
        slot = await pool.acquire()
        pool.release(slot, "auth", error=Exception("401 invalid api key"))
        assert slot.auth_failures == 1
        assert slot.cooldown_remaining() > 0

        await asyncio.sleep(0.45)
        slot = await pool.acquire(timeout=1)
        pool.release(slot, "fatal", error=Exception("400 bad request"))
        assert slot.fatal_count == 1
        assert slot.consecutive_failures == 1, "fatal must leave the counter untouched"
        assert slot.cooldown_remaining() == 0, "fatal must not trigger cooldown"

    asyncio.run(scenario())
    print("  auth cooldown + fatal no-penalty: OK")


def test_cross_slot_retry() -> None:
    import image_gen as ig

    cfg = pp.PoolConfig(
        max_attempts=4,
        fail_threshold=1,
        cooldown_base_seconds=0.2,
        cooldown_max_seconds=1.0,
        slots=[
            pp.ProviderSlot(name="bad", api_key="x", weight=1),
            pp.ProviderSlot(name="good", api_key="x", weight=1),
        ],
    )
    pool = pp.ProviderPool(cfg)
    pool.model_for = lambda slot: "bad-model" if slot.name == "bad" else "good-model"

    seen: list = []

    class FakeImages:
        async def generate(self, **kwargs):
            seen.append(kwargs["model"])
            if kwargs["model"] == "bad-model":
                raise Exception("503 upstream_unavailable")
            return "ok-result"

    pool.async_client = lambda slot: type("C", (), {"images": FakeImages()})()

    async def scenario():
        return await ig._invoke_with_pool(
            pool=pool,
            job_label="[test]",
            build_payload=lambda slot: ig._with_slot_model(
                {"prompt": "p", "model": "ignored"}, pool, slot
            ),
            call=lambda client, request: client.images.generate(**request),
        )

    assert asyncio.run(scenario()) == "ok-result"
    assert seen == ["bad-model", "good-model"], seen
    assert pool.slots[0].fail_count == 1
    assert pool.slots[1].ok_count == 1
    print(f"  cross-slot retry: OK {seen}")


def test_fatal_is_not_retried() -> None:
    import image_gen as ig

    cfg = pp.PoolConfig(
        max_attempts=4,
        slots=[
            pp.ProviderSlot(name="a", api_key="x"),
            pp.ProviderSlot(name="b", api_key="x"),
        ],
    )
    pool = pp.ProviderPool(cfg)
    calls: list = []

    class FakeImages:
        async def generate(self, **kwargs):
            calls.append(1)
            raise Exception("400 invalid size parameter")

    pool.async_client = lambda slot: type("C", (), {"images": FakeImages()})()

    async def scenario():
        return await ig._invoke_with_pool(
            pool=pool,
            job_label="[test]",
            build_payload=lambda slot: ig._with_slot_model({"prompt": "p"}, pool, slot),
            call=lambda client, request: client.images.generate(**request),
        )

    try:
        asyncio.run(scenario())
        raise AssertionError("expected failure")
    except RuntimeError as exc:
        assert "fatal" in str(exc), exc
    assert len(calls) == 1, f"fatal errors must not be retried: {calls}"
    print("  fatal is not retried: OK")


def test_sticky_mode() -> None:
    cfg = pp.PoolConfig(
        slots=[
            pp.ProviderSlot(name="a", api_key="x", weight=5),
            pp.ProviderSlot(name="b", api_key="x", weight=1),
            pp.ProviderSlot(name="c", api_key="x", weight=1),
        ]
    )
    pool = pp.ProviderPool(cfg)
    assert pool.pinned_name is None

    # Explicit pin: every pick must land on the pinned slot.
    pinned = pool.pin("c")
    assert pinned.name == "c"
    assert pool.pinned_name == "c"
    picks = set()
    for _ in range(50):
        slot = pool._select()
        assert slot is not None
        picks.add(slot.name)
        pool.release(slot, "ok")
    assert picks == {"c"}, picks
    assert pool.total_capacity() == cfg.slots[2].max_concurrency

    # Unknown slot name is an error, not a silent fallback.
    try:
        pool.pin("nope")
        raise AssertionError("expected PoolConfigError")
    except pp.PoolConfigError as exc:
        assert "nope" in str(exc), exc

    # Auto pin picks by weight, then stays put.
    pool.unpin()
    auto = pool.pin()
    assert auto.name == "a", auto.name
    picks = set()
    for _ in range(50):
        slot = pool._select()
        assert slot is not None
        picks.add(slot.name)
        pool.release(slot, "ok")
    assert picks == {"a"}, picks

    # While pinned, a cooling slot is waited for, never swapped for another.
    slot = pool.slots[0]
    slot.cooldown_until = time.monotonic() + 5
    assert pool._select() is None, "pinned cooling slot must not be swapped out"
    slot.cooldown_until = 0.0
    print("  sticky mode: OK")


def test_config_parsing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "providers.toml"
        target.write_text(
            """
[pool]
strategy = "least-inflight"
max_attempts = 5
cooldown_base_seconds = 2
cooldown_max_seconds = 60

[defaults]
model = "gpt-image-2"
max_concurrency = 3

[[provider]]
name = "a"
base_url = "https://x.test/v1"
api_key = "k1"
weight = 2

[[provider]]
name = "b"
base_url_env = "IMAGEGEN_TEST_URL"
api_key_env = "IMAGEGEN_TEST_UNSET"
enabled = false
""",
            encoding="utf-8",
        )
        cfg = pp.load_pool_config(
            str(target), env={"IMAGEGEN_TEST_URL": "https://env.test/v1"}
        )
        assert cfg is not None
        assert cfg.strategy == "least-inflight"
        assert cfg.max_attempts == 5
        assert cfg.cooldown_max_seconds == 60
        assert len(cfg.slots) == 2
        assert cfg.slots[0].weight == 2
        assert cfg.slots[0].base_url == "https://x.test/v1"
        assert cfg.slots[1].base_url == "https://env.test/v1"
        assert cfg.slots[1].enabled is False
        assert cfg.slots[1].max_concurrency == 3
        assert cfg.slots[1].api_key == ""

        pool = pp.ProviderPool(cfg)
        assert len(pool.slots) == 1, "disabled slots must not be scheduled"

        bad = root / "bad.toml"
        bad.write_text("[[provider]]\nname = 'x'\n", encoding="utf-8")
        try:
            pp.load_pool_config(str(bad))
            raise AssertionError("expected PoolConfigError")
        except pp.PoolConfigError as exc:
            assert "api_key" in str(exc), exc
    print("  config parsing: OK")


def test_config_lookup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conf_home = root / "confhome"
        conf_home.mkdir()

        # XDG_CONFIG_HOME keeps this check platform-independent.
        env = {"XDG_CONFIG_HOME": str(conf_home)}
        shared = conf_home / "imagegen"

        # A plain checkout (no agent tree) has no profile -> shared dir only.
        checkout = root / "Desktop" / "imagegen"
        checkout.mkdir(parents=True)
        assert pp.host_profile(checkout) is None
        assert pp.config_dir_candidates(env, checkout) == [shared]

        # A copy installed under an agent's tree gets its own profile dir.
        agent_root = root / "home" / ".claude" / "skills" / "imagegen"
        agent_root.mkdir(parents=True)
        assert pp.host_profile(agent_root) == "claude"
        assert pp.config_dir_candidates(env, agent_root) == [shared / "claude", shared]

        # IMAGEGEN_CONFIG_DIR takes precedence over both.
        env_explicit = dict(env, IMAGEGEN_CONFIG_DIR=str(root / "explicit-dir"))
        dirs = pp.config_dir_candidates(env_explicit, agent_root)
        assert dirs[0] == root / "explicit-dir"

        # Nothing configured yet.
        assert pp.find_providers_path(None, skill_root=checkout, env=env) is None

        # Each agent environment resolves to its own file; the checkout falls
        # back to the shared one.
        agent_dir = shared / "claude"
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_file = agent_dir / "providers.toml"
        agent_file.write_text("[[provider]]\nname = 'c'\napi_key = 'k'\n", encoding="utf-8")
        shared_file = shared / "providers.toml"
        shared_file.write_text("[[provider]]\nname = 's'\napi_key = 'k'\n", encoding="utf-8")

        assert pp.find_providers_path(None, skill_root=agent_root, env=env) == agent_file
        assert pp.find_providers_path(None, skill_root=checkout, env=env) == shared_file

        # Explicit path still wins.
        explicit = root / "explicit.toml"
        explicit.write_text("[[provider]]\nname = 'e'\napi_key = 'k'\n", encoding="utf-8")
        assert (
            pp.find_providers_path(str(explicit), skill_root=agent_root, env=env)
            == explicit
        )

        # A named-but-missing path is an error, not a silent fallback.
        try:
            pp.find_providers_path(str(root / "nope.toml"), skill_root=checkout, env=env)
            raise AssertionError("expected PoolConfigError")
        except pp.PoolConfigError:
            pass
    print("  config lookup + per-agent isolation: OK")


def main() -> int:
    print("provider pool self-checks:")
    test_classify()
    test_weighted_round_robin()
    test_concurrency_and_cooldown()
    test_auth_cooldown_and_fatal()
    test_cross_slot_retry()
    test_fatal_is_not_retried()
    test_sticky_mode()
    test_config_parsing()
    test_config_lookup()
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
