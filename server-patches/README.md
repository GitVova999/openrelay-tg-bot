# Server-side patches applied to `/opt/laminarity-relay/`

These live outside this repo but power the bot's payment path.  Kept here
as source of truth in case they need to be reapplied to a fresh relay
deployment.

## Files

- `_idempotency.py` — copy of `/opt/laminarity-relay/_idempotency.py`.
  Nonce-keyed in-memory cache used by the request pipeline to short-circuit
  duplicates.

## app.py patches

1. Import `_idempotency` alongside the other x402 imports:
   ```python
   from x402.http.middleware.fastapi import payment_middleware
   import _idempotency
   ```

2. In the `_capture_body_then_x402` middleware, wrap the dispatch that
   routes to the appropriate `payment_middleware` variant with an
   idempotency check.  See git blame on `app.py` around the
   "Idempotency" comment block; the wrap:
   - calls `_idempotency.cached_or_pending(request)` first; on HIT returns
     the cached response verbatim,
   - on MISS runs the payment middleware as before, then buffers the
     response body (handling both `Response` and `StreamingResponse`) and
     calls `_idempotency.store(...)` on success.

3. Also patched: CDP settle retry (2x4s backoff on `settlement_pending`),
   living in the block that constructs `_cdp_client`.  See `_cdp_settle_with_retry`.

The whole point of both patches is to make the pipeline robust to Base
network slowness + Cloudflare's 100 s quicktunnel timeout without ever
letting a client be double-charged.
