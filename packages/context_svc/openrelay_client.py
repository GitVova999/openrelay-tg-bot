"""Python client for OpenRelay via x402 (CDP facilitator, /v1/premium/*).

The bot signs an EIP-3009 authorization from its wallet, POSTs to
OpenRelay premium route, CDP facilitator settles on Base.  Same code
path any external x402 consumer would use.

Robustness pattern (Cloudflare quicktunnel has a 100 s HTTP hard-limit
that we occasionally hit when Base is slow to confirm):

1. First attempt uses x402AsyncTransport, which handles the 402 challenge
   → sign → retry-with-payment flow.  We hook `_send_retry` to CAPTURE the
   `PAYMENT-SIGNATURE` header we ended up sending.
2. On 5xx / 504 / 524 / network error we retry manually POSTing the SAME
   captured payment header (backoff 15 s → 30 s).  Server-side idempotency
   cache (keyed by the EIP-3009 nonce) recognises the duplicate and returns
   the already-computed response WITHOUT re-inference or re-settlement.
   So a client retry is safe: no double charge, no double work.

Wallet key comes from `OPENRELAY_WALLET_PRIVATE_KEY`.  Recommend a dedicated
low-value wallet — never main funds.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx
from eth_account import Account
from x402 import x402Client
from x402.http.clients.httpx import x402AsyncTransport
from x402.mechanisms.evm.exact import ExactEvmScheme

log = logging.getLogger("openrelay")

OPENRELAY_BASE = os.environ.get(
    "OPENRELAY_BASE_URL", "https://openrelay.hggfffdfy687.workers.dev"
).rstrip("/")
# Route: /v1/premium/chat/completions (CDP facilitator).  /v1/chat/completions
# also works but currently routed via PayAI (limited free tier).
PREMIUM_PATH = "/v1/premium/chat/completions"

# 524 = Cloudflare origin timeout; 502/503/504 = transient upstream trouble.
_TRANSIENT_STATUSES = {502, 503, 504, 524}
_RETRY_DELAYS_S = (15, 30)  # 2 retries: 15s then 30s


class _CapturingTransport(x402AsyncTransport):
    """x402AsyncTransport that records the PAYMENT-SIGNATURE header it sends.

    Lets us re-issue exactly the same authorization on retry without asking
    the wallet to sign a fresh one (which would use a new nonce and defeat
    server-side idempotency).
    """

    captured_signature: str | None = None

    async def _send_retry(self, request, extra_headers, **kw):  # noqa: ANN001
        sig = extra_headers.get("X-PAYMENT") or extra_headers.get("PAYMENT-SIGNATURE")
        if sig:
            self.captured_signature = sig
        return await super()._send_retry(request, extra_headers, **kw)


def _build_paying_client(private_key: str) -> tuple[httpx.AsyncClient, _CapturingTransport]:
    signer = Account.from_key(private_key)
    x402 = x402Client()
    x402.register("eip155:8453", ExactEvmScheme(signer=signer))
    transport = _CapturingTransport(x402)
    return httpx.AsyncClient(transport=transport, timeout=180), transport


async def chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 1024,
    temperature: float = 0.4,
    stop: list[str] | None = None,
    private_key: str | None = None,
) -> dict[str, Any]:
    """POST to OpenRelay /v1/premium/chat/completions with automatic x402 payment.

    Returns the raw provider response dict; caller pulls `choices[0].message.content`
    and `usage`.  Raises on non-200.
    """
    key = private_key or os.environ.get("OPENRELAY_WALLET_PRIVATE_KEY", "")
    if not key:
        raise RuntimeError("OPENRELAY_WALLET_PRIVATE_KEY not set")
    if not key.startswith("0x"):
        key = "0x" + key

    url = OPENRELAY_BASE + PREMIUM_PATH
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if stop:
        body["stop"] = stop

    http, transport = _build_paying_client(key)
    try:
        # Attempt 0: full x402 flow. Signs a fresh authorization inside the
        # transport; captures the resulting PAYMENT-SIGNATURE header for reuse.
        r = await _safe_post(http, url, body)

        # Retry only on transient upstream errors, and only after we captured
        # a payment signature (i.e. we already paid; server-side idempotency
        # will short-circuit the duplicate).
        for i, delay in enumerate(_RETRY_DELAYS_S, start=1):
            if r.status_code == 200:
                break
            if r.status_code not in _TRANSIENT_STATUSES:
                break
            if not transport.captured_signature:
                log.warning("transient %s but no payment sig captured — giving up", r.status_code)
                break
            log.warning("openrelay returned %s, retry %d/%d in %ds (reusing same auth)",
                         r.status_code, i, len(_RETRY_DELAYS_S), delay)
            await asyncio.sleep(delay)
            r = await _replay_with_captured_signature(url, body, transport.captured_signature)

        if r.status_code != 200:
            raise RuntimeError(f"OpenRelay {r.status_code}: {r.text[:500]}")
        return r.json()
    finally:
        await http.aclose()


async def _safe_post(http: httpx.AsyncClient, url: str, body: dict[str, Any]) -> httpx.Response:
    try:
        return await http.post(url, json=body)
    except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.NetworkError) as e:
        # Manufacture a synthetic 504 so the retry loop above treats network
        # errors the same as an upstream 504.
        log.warning("network error on openrelay POST: %s", e)
        return httpx.Response(status_code=504, content=str(e).encode())


async def _replay_with_captured_signature(url: str, body: dict[str, Any], sig: str) -> httpx.Response:
    """Replay POST with the ALREADY-SIGNED payment header. Server dedups by nonce."""
    async with httpx.AsyncClient(timeout=180) as http:
        try:
            return await http.post(
                url,
                json=body,
                headers={"PAYMENT-SIGNATURE": sig, "Access-Control-Expose-Headers": "PAYMENT-RESPONSE"},
            )
        except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.NetworkError) as e:
            log.warning("replay network error: %s", e)
            return httpx.Response(status_code=504, content=str(e).encode())


async def _selftest() -> None:
    """`python -m packages.context_svc.openrelay_client` for a live probe."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s %(message)s")
    j = await chat_completion(
        model="deepseek-ai/DeepSeek-V4-Flash-0731",
        messages=[{"role": "user", "content": "Say 'openrelay works' in 3 words."}],
        max_tokens=20,
    )
    print(j["choices"][0]["message"]["content"])
    print("usage:", j.get("usage"))


if __name__ == "__main__":
    import asyncio
    asyncio.run(_selftest())
