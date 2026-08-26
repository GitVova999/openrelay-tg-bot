"""Nonce-keyed idempotency cache for paid POST routes.

x402 EIP-3009 payloads carry a random 32-byte nonce that uniquely
identifies one authorization.  Two requests with the same nonce are
by construction the same payment intent, so serving the same response
to both is correct.

This module gives us:
  - `cached_or_pending(request) -> Response | None` — returns a cached
    response instantly if we've served this nonce before; returns None
    if it's fresh, and marks the key as in-flight so any concurrent
    duplicate WAITS on the same asyncio event instead of racing.
  - `store(request, response_bytes, status, headers)` — save on success.

Notes:
  - Only successful (200) responses are cached.  A 402 challenge is
    re-issuable and shouldn't dedup.
  - TTL = 10 minutes, comfortably longer than a Base tx confirmation
    window and longer than typical bot retry backoffs.
  - In-memory only.  Restart clears the cache — acceptable because
    validBefore on our EIP-3009 authorizations is short (~5-10 min),
    so a lost cache entry just means one extra genuine settle.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("idempotency")

TTL_SECONDS = 600
_MAX_ENTRIES = 4096  # rough safety bound to prevent runaway growth

# Entry states:
#   ("pending", asyncio.Event, None)  → in-flight; wait on event
#   ("done",    (body, status, headers, expiry_ts))  → cached
_store: dict[str, tuple[str, Any]] = {}
_lock = asyncio.Lock()


def _extract_nonce(request: Request) -> str | None:
    """Return a stable cache key from x402 payment header, or None if absent."""
    sig = request.headers.get("payment-signature") or request.headers.get("x-payment")
    if not sig:
        return None
    try:
        raw = base64.b64decode(sig + "=" * (-len(sig) % 4))
        payload = json.loads(raw)
        # V2 shape: payload.authorization.nonce ; V1 fallback: payload.nonce
        auth = payload.get("payload", {}).get("authorization") or payload.get("payload", {})
        nonce = auth.get("nonce") if isinstance(auth, dict) else None
        if not nonce or not isinstance(nonce, str):
            return None
        # Include the route path so /v1/premium and /v1/chat don't collide.
        return f"{request.url.path}::{nonce.lower()}"
    except Exception:
        return None


def _sweep_locked() -> None:
    """Prune expired entries. Caller MUST hold _lock (asyncio.Lock is not
    reentrant — awaiting `async with _lock:` while already inside it deadlocks
    the process forever)."""
    now = time.time()
    expired = [k for k, (state, val) in _store.items()
               if state == "done" and val[3] < now]
    for k in expired:
        _store.pop(k, None)
    if len(_store) > _MAX_ENTRIES:
        done_only = [(k, v[1][3]) for k, v in _store.items() if v[0] == "done"]
        done_only.sort(key=lambda x: x[1])
        for k, _ in done_only[: len(_store) - _MAX_ENTRIES]:
            _store.pop(k, None)


async def cached_or_pending(request: Request) -> tuple[str | None, Response | None]:
    """Look up or claim the cache slot for this request.

    Returns (key, response):
      - (None, None)   — no payment header, don't cache
      - (key, Response) — cache HIT, use this response
      - (key, None)   — cache MISS, caller should proceed; MUST call
        `store(key, ...)` after success (or `abandon(key)` on failure)
        so waiting duplicates unblock.
    """
    key = _extract_nonce(request)
    if key is None:
        return None, None

    while True:
        async with _lock:
            entry = _store.get(key)
            if entry is None:
                # Claim this key for our request.
                _store[key] = ("pending", asyncio.Event())
                _sweep_locked()
                return key, None

            state, val = entry
            if state == "done":
                body, status, headers, expiry_ts = val
                if expiry_ts >= time.time():
                    log.info("idempotency HIT %s (status=%s, %d bytes)", key[:32], status, len(body))
                    # Copy headers so we don't mutate cached dict later.
                    return key, Response(content=body, status_code=status, headers=dict(headers))
                # Expired — drop and treat as miss on this loop turn.
                _store.pop(key, None)
                continue

            # pending — someone else is doing the work. Wait outside the lock.
            event = val

        try:
            await asyncio.wait_for(event.wait(), timeout=90)
        except asyncio.TimeoutError:
            log.warning("idempotency wait timed out for %s — treating as miss", key[:32])
            async with _lock:
                _store.pop(key, None)
            return key, None
        # Loop back to check whether the leader stored a result or abandoned.


async def store(key: str, body: bytes, status: int, headers: dict[str, str]) -> None:
    """Cache a successful response and unblock any concurrent duplicates."""
    if status < 200 or status >= 300:
        # Never cache errors — challenge (402), unauthorized (401), server
        # error (5xx). Successful retries should get fresh attempts.
        await abandon(key)
        return
    expiry_ts = time.time() + TTL_SECONDS
    filtered = {k: v for k, v in headers.items()
                if k.lower() not in ("date", "server", "connection", "transfer-encoding")}
    async with _lock:
        entry = _store.get(key)
        event = entry[1] if entry and entry[0] == "pending" else None
        _store[key] = ("done", (body, status, filtered, expiry_ts))
    if event:
        event.set()
    log.info("idempotency STORE %s (status=%s, %d bytes, ttl=%ds)",
             key[:32], status, len(body), TTL_SECONDS)


async def abandon(key: str) -> None:
    """Drop the pending slot so retries can try afresh."""
    async with _lock:
        entry = _store.pop(key, None)
    if entry and entry[0] == "pending":
        entry[1].set()  # unblock waiters
