"""Optional Redis support for multi-worker cloud deployments.

Activated by ``EVOFLOW_REDIS_URL=redis://host:6379/0``. When unset (the
default — local / desktop mode) every helper here degrades to in-process
primitives with zero behavior change.

Current consumer: the MCP OAuth token-refresh lock (previously a per-process
``asyncio.Lock`` — with >1 gateway worker, two processes could refresh the
same token concurrently and hit ``invalid_grant``).

Dependencies: ``redis>=5`` (pyproject extra ``redis``). Lazy import; if the
package is missing or the server is unreachable, callers fall back to the
in-process lock and a single warning is logged (a single worker remains
correct — it is only multi-worker mode that requires a shared store).
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator

logger = logging.getLogger(__name__)

_redis_client: object | None = None
_redis_unavailable_logged = False


def resolve_redis_url() -> str:
    """Return the Redis URL from ``EVOFLOW_REDIS_URL`` (empty = disabled)."""
    return (os.getenv("EVOFLOW_REDIS_URL") or "").strip()


def redis_enabled() -> bool:
    return bool(resolve_redis_url())


def get_redis():
    """Process-wide async Redis client (or ``None`` when unavailable).

    The client is created once; connection failures are logged once and
    cached as ``None`` (no per-call retry storm). Redis-py reconnects
    automatically once the server comes back.
    """
    global _redis_client, _redis_unavailable_logged
    if _redis_client is not None:
        return _redis_client

    url = resolve_redis_url()
    if not url:
        return None

    try:
        from redis.asyncio import Redis
    except ImportError:
        _warn_redis_unavailable(
            "redis package not installed — install with: "
            "uv sync --extra redis  (pip install 'redis>=5'). "
            "Falling back to in-process locks."
        )
        return None

    try:
        client = Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=2.0,
            socket_timeout=5.0,
            health_check_interval=30,
        )
    except Exception as exc:  # noqa: BLE001 - bad URL etc.
        _warn_redis_unavailable(f"cannot create Redis client for {url!r}: {exc}")
        return None

    _redis_client = client
    return client


def _warn_redis_unavailable(reason: str) -> None:
    global _redis_unavailable_logged
    if _redis_unavailable_logged:
        return
    _redis_unavailable_logged = True
    logger.warning("EVOFLOW_REDIS_URL is set but Redis is unavailable: %s", reason)


async def ping_redis() -> bool:
    """Best-effort connectivity probe (used by startup diagnostics)."""
    client = get_redis()
    if client is None:
        return False
    try:
        await client.ping()
        return True
    except Exception as exc:  # noqa: BLE001
        _warn_redis_unavailable(f"ping failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Cross-process async lock
# ---------------------------------------------------------------------------

# Release only if we still own the lock (compare-and-delete). Releasing
# blindly could delete a lock another worker acquired after ours expired.
_RELEASE_LUA = (
    b"if redis.call('get', KEYS[1]) == ARGV[1] then "
    b"return redis.call('del', KEYS[1]) else return 0 end"
)


async def release_lock(name: str, token: str) -> None:
    client = get_redis()
    if client is None:
        return
    try:
        await client.eval(_RELEASE_LUA, 1, f"evoflow:lock:{name}", token)
    except Exception as exc:  # noqa: BLE001
        logger.debug("redis lock release failed for %r: %s", name, exc)


@asynccontextmanager
async def cross_process_lock(
    name: str,
    *,
    fallback: object | None = None,
    ttl_s: float = 120.0,
) -> AsyncIterator[bool]:
    """Non-blocking cross-process lock; yields ``True`` only when acquired.

    Behavior matrix:

    - Redis enabled and lock free  → yields True, releases on exit
    - Redis enabled and lock held  → yields False (caller should skip)
    - Redis enabled but unreachable → degrades to the ``fallback`` in-process
      lock (fail-open: better to risk a rare duplicate refresh than to stop
      refreshing entirely while Redis is down)
    - Redis disabled (no URL)      → ``fallback`` lock with try-skip
      semantics (``fallback=None`` means "no contention control": True)

    Typical use (poll-driven refresh loop that must never queue up)::

        async with cross_process_lock("mcp_oauth_refresh", fallback=_LOCK) as got:
            if not got:
                return
            ...
    """
    if redis_enabled():
        client = get_redis()
        if client is not None:
            token = secrets.token_hex(16)
            key = f"evoflow:lock:{name}"
            try:
                ok = await client.set(key, token, nx=True, px=int(ttl_s * 1000))
            except Exception as exc:  # noqa: BLE001 - degrade, don't block
                _warn_redis_unavailable(f"lock acquire failed for {name!r}: {exc}")
            else:
                if not ok:
                    yield False  # another worker holds it
                    return
                try:
                    yield True
                finally:
                    await release_lock(name, token)
                return
        # Redis unreachable → fall through to the local lock below.

    # No shared store (or shared store down): single-process semantics.
    if fallback is None:
        yield True
        return
    if getattr(fallback, "locked", lambda: False)():
        yield False
        return
    async with fallback:
        yield True
