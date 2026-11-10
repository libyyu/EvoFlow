"""Cross-process lock tests (fake Redis, no server needed).

Run: cd backend && uv run pytest tests/test_redis_lock.py -v
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from evoflow.core import redis_client as rc


class FakeRedis:
    """Minimal async Redis: SET NX + Lua compare-and-del + ping."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[bytes, bytes] = {}
        self.fail = fail
        self.eval_calls: list = []

    async def set(self, key, value, nx=False, px=None):
        if self.fail:
            raise ConnectionError("fake redis down")
        kb = key if isinstance(key, bytes) else key.encode()
        vb = value if isinstance(value, bytes) else value.encode()
        if nx and kb in self.store:
            return None
        self.store[kb] = vb
        return True

    async def eval(self, script, numkeys, key, token):
        if self.fail:
            raise ConnectionError("fake redis down")
        kb = key if isinstance(key, bytes) else key.encode()
        tb = token if isinstance(token, bytes) else token.encode()
        self.eval_calls.append((kb, tb))
        if self.store.get(kb) == tb:
            self.store.pop(kb, None)
            return 1
        return 0

    async def ping(self):
        if self.fail:
            raise ConnectionError("fake redis down")
        return True


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.delenv("EVOFLOW_REDIS_URL", raising=False)
    rc._redis_client = None
    rc._redis_unavailable_logged = False
    yield
    rc._redis_client = None
    rc._redis_unavailable_logged = False


@pytest.fixture
def fake_redis_module(monkeypatch):
    """Inject a fake redis.asyncio module with a controllable client."""
    client = FakeRedis()

    redis_mod = types.ModuleType("redis")
    asyncio_mod = types.ModuleType("redis.asyncio")

    class _Redis:
        @staticmethod
        def from_url(*args, **kwargs):
            return client

    asyncio_mod.Redis = _Redis
    redis_mod.asyncio = asyncio_mod
    monkeypatch.setitem(sys.modules, "redis", redis_mod)
    monkeypatch.setitem(sys.modules, "redis.asyncio", asyncio_mod)
    return client


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def test_disabled_by_default():
    assert rc.resolve_redis_url() == ""
    assert rc.redis_enabled() is False
    assert rc.get_redis() is None


def test_env_enables(monkeypatch):
    monkeypatch.setenv("EVOFLOW_REDIS_URL", "redis://localhost:6379/0")
    assert rc.redis_enabled() is True


def test_missing_package_warns_once(monkeypatch, caplog):
    monkeypatch.setenv("EVOFLOW_REDIS_URL", "redis://localhost:6379/0")
    # no fake module injected -> real import fails (redis not installed)
    got = rc.get_redis()
    assert got is None
    assert rc._redis_unavailable_logged is True


# ---------------------------------------------------------------------------
# cross_process_lock behavior matrix
# ---------------------------------------------------------------------------


async def test_lock_disabled_uses_fallback():
    lock = asyncio.Lock()
    async with rc.cross_process_lock("t", fallback=lock) as got:
        assert got is True
        assert lock.locked()
    assert not lock.locked()


async def test_lock_disabled_busy_fallback_skips():
    lock = asyncio.Lock()
    async with lock:
        async with rc.cross_process_lock("t", fallback=lock) as got:
            assert got is False


async def test_lock_disabled_no_fallback_always_true():
    async with rc.cross_process_lock("t", fallback=None) as got:
        assert got is True


async def test_lock_redis_acquire_and_release(monkeypatch, fake_redis_module):
    monkeypatch.setenv("EVOFLOW_REDIS_URL", "redis://localhost:6379/0")
    client = fake_redis_module
    assert await rc.ping_redis() is True

    async with rc.cross_process_lock("oauth") as got:
        assert got is True
        # key held while inside
        assert b"evoflow:lock:oauth" in client.store
    # released (with token compare) on exit
    assert b"evoflow:lock:oauth" not in client.store
    assert len(client.eval_calls) == 1


async def test_lock_redis_held_by_other_skips(monkeypatch, fake_redis_module):
    monkeypatch.setenv("EVOFLOW_REDIS_URL", "redis://localhost:6379/0")
    client = fake_redis_module
    # another worker holds it
    await client.set(b"evoflow:lock:oauth", b"someone-else", nx=True, px=60000)
    async with rc.cross_process_lock("oauth") as got:
        assert got is False
    # not released by us (token mismatch)
    assert b"evoflow:lock:oauth" in client.store


async def test_lock_redis_down_degrades_to_fallback(monkeypatch, fake_redis_module):
    """Redis configured but unreachable -> in-process fallback, not a skip."""
    monkeypatch.setenv("EVOFLOW_REDIS_URL", "redis://localhost:6379/0")
    fake_redis_module.fail = True
    lock = asyncio.Lock()
    async with rc.cross_process_lock("oauth", fallback=lock) as got:
        assert got is True
        assert lock.locked()
    assert not lock.locked()


async def test_lock_release_keeps_foreign_owner(monkeypatch, fake_redis_module):
    """TTL expiry mid-critical-section: our release must not delete the
    lock a successor legitimately re-acquired."""
    monkeypatch.setenv("EVOFLOW_REDIS_URL", "redis://localhost:6379/0")
    client = fake_redis_module

    async with rc.cross_process_lock("oauth") as got:
        assert got is True
        # simulate TTL expiry + another worker taking over while we run
        client.store[b"evoflow:lock:oauth"] = b"successor-token"
    # successor's lock survives our release
    assert client.store[b"evoflow:lock:oauth"] == b"successor-token"


# ---------------------------------------------------------------------------
# oauth_flow integration (the actual consumer)
# ---------------------------------------------------------------------------


async def test_refresh_uses_cross_process_lock(monkeypatch, fake_redis_module, tmp_path):
    """refresh_all_expiring must respect a lock held by another worker."""
    monkeypatch.setenv("EVOFLOW_REDIS_URL", "redis://localhost:6379/0")
    client = fake_redis_module
    # another worker holds the refresh lock
    await client.set(b"evoflow:lock:mcp_oauth_refresh", b"other", nx=True, px=60000)

    from evoflow.mcp import oauth_flow

    monkeypatch.setattr(oauth_flow, "_token_path", lambda: tmp_path / "tokens.json")
    (tmp_path / "tokens.json").write_text(
        '{"srv": {"access_token": "a", "refresh_token": "r",'
        ' "expires_at": 1, "client_id": "c", "server": {"token_endpoint": "http://x"}}}',
        encoding="utf-8",
    )
    called = []

    async def fake_refresh(server, client_id, refresh_token):
        called.append(refresh_token)
        raise AssertionError("must not refresh when lock is held")

    monkeypatch.setattr(oauth_flow, "refresh", fake_refresh)
    await oauth_flow.refresh_all_expiring()
    assert called == []  # skipped entirely


async def test_refresh_runs_when_lock_free(monkeypatch, fake_redis_module, tmp_path):
    monkeypatch.setenv("EVOFLOW_REDIS_URL", "redis://localhost:6379/0")

    from evoflow.mcp import oauth_flow

    monkeypatch.setattr(oauth_flow, "_token_path", lambda: tmp_path / "tokens.json")
    (tmp_path / "tokens.json").write_text(
        '{"srv": {"access_token": "a", "refresh_token": "r",'
        ' "expires_at": 1, "client_id": "c", "server": {"token_endpoint": "http://x"}}}',
        encoding="utf-8",
    )

    class _Tok:
        access_token = "new"
        refresh_token = None
        expires_in = 3600
        scope = None

    async def fake_refresh(server, client_id, refresh_token):
        return _Tok()

    monkeypatch.setattr(oauth_flow, "refresh", fake_refresh)
    monkeypatch.setattr(oauth_flow, "_update_server_headers", lambda *a, **k: None)
    await oauth_flow.refresh_all_expiring()
    data = (tmp_path / "tokens.json").read_text(encoding="utf-8")
    assert '"access_token": "new"' in data
