"""Python client for OpenRelay via x402 (CDP facilitator, /v1/premium/*).

This is the real payment path we run in beta — the bot signs an EIP-3009
`TransferWithAuthorization` from its wallet against OpenRelay's premium
route, which uses the CDP facilitator (no free-tier limits, $0.01/req floor).

We use the same x402 python SDK the server runs on, so signature format
and version match by construction. `eth_account.Account.from_key(...)` is
wrapped automatically by ExactEvmScheme into a signer.

Wallet key comes from OPENRELAY_WALLET_PRIVATE_KEY.  Recommend a dedicated
low-value wallet — never our main funds.
"""

from __future__ import annotations

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


def _build_paying_client(private_key: str) -> httpx.AsyncClient:
    signer = Account.from_key(private_key)
    x402 = x402Client()
    x402.register("eip155:8453", ExactEvmScheme(signer=signer))
    transport = x402AsyncTransport(x402)
    return httpx.AsyncClient(transport=transport, timeout=180)


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

    async with _build_paying_client(key) as http:
        r = await http.post(url, json=body)
    if r.status_code != 200:
        raise RuntimeError(f"OpenRelay {r.status_code}: {r.text[:500]}")
    return r.json()


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
