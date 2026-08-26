"""Summarize a channel's messages over a time window.

MVP path — direct inference call (no retrieval; whole window fits in-context).
Once channels grow past ~50k tokens per window we switch to hierarchical
digests + retrieval; the interface stays the same.

Beta-mode inference: talks to Gonka upstream directly with a shared internal
API key, bypassing our own x402 payment layer.  This is our own server calling
our own stack — end users still pay per request via the bot (billing_svc
records usage → treasury/user-pays flows).  Production path (Simple/Docker
tiers) will route through OpenRelay so external inference providers can
plug in without OpenRelay-internal secrets.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from packages.common.config import settings
from packages.common.db import session
from packages.common.models import Channel, Message

log = logging.getLogger("summarize")

INFERENCE_URL = os.environ.get("INFERENCE_URL", "https://proxy.gonka.gg/v1/chat/completions")
INFERENCE_KEY = os.environ.get("INFERENCE_KEY", "")


async def _fetch_window(channel_id: int, since: datetime | None, limit: int | None) -> list[Message]:
    async with session() as s:
        stmt = select(Message).where(Message.channel_id == channel_id)
        if since is not None:
            stmt = stmt.where(Message.sent_at >= since)
        stmt = stmt.order_by(Message.sent_at)
        if limit:
            stmt = stmt.limit(limit)
        return list((await s.execute(stmt)).scalars().all())


def _format_transcript(msgs: list[Message]) -> str:
    """One line per message: [date] name: text."""
    lines: list[str] = []
    for m in msgs:
        stamp = m.sent_at.strftime("%Y-%m-%d %H:%M") if m.sent_at else "?"
        who = m.sender_name or "?"
        text = m.text.replace("\n", " ").strip()
        lines.append(f"[{stamp}] {who}: {text}")
    return "\n".join(lines)


PROMPT_TEMPLATE = """Ты — аналитик TG-каналов. Ниже — сообщения канала «{title}» за период {period}.
Сделай краткое саммари: главные темы, повторяющиеся мотивы, тон, что-либо необычное.
Формат: 5-8 буллетов через дефис. Русский язык. Без вступительной фразы, сразу буллеты.

===
{transcript}
===
"""


async def summarize_channel(
    tg_chat_id: int,
    hours: int | None = 24,
    since: datetime | None = None,
    max_output_tokens: int = 600,
) -> dict:
    """Return {ok, summary, model, in_tokens_est, msg_count, period}."""
    s = settings()
    if not INFERENCE_KEY:
        raise RuntimeError("INFERENCE_KEY env not set (beta bypass)")

    if since is None and hours is not None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)

    async with session() as db:
        ch = (await db.execute(select(Channel).where(Channel.tg_chat_id == tg_chat_id))).scalar_one_or_none()
    if ch is None:
        return {"ok": False, "error": f"channel {tg_chat_id} not registered"}

    msgs = await _fetch_window(ch.id, since, limit=None)
    if not msgs:
        return {"ok": False, "error": "no messages in window"}

    period = (
        f"с {msgs[0].sent_at:%Y-%m-%d} по {msgs[-1].sent_at:%Y-%m-%d}"
        if len(msgs) > 1
        else f"на {msgs[0].sent_at:%Y-%m-%d}"
    )
    transcript = _format_transcript(msgs)
    prompt = PROMPT_TEMPLATE.format(title=ch.title, period=period, transcript=transcript)

    async with httpx.AsyncClient(timeout=180) as http:
        r = await http.post(
            INFERENCE_URL,
            headers={"Authorization": f"Bearer {INFERENCE_KEY}", "content-type": "application/json"},
            json={
                "model": s.model_large,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_output_tokens,
                "temperature": 0.4,
            },
        )
    if r.status_code != 200:
        return {"ok": False, "error": f"upstream {r.status_code}: {r.text[:200]}"}
    j = r.json()
    summary = j["choices"][0]["message"]["content"]
    usage = j.get("usage", {})
    return {
        "ok": True,
        "summary": summary,
        "model": s.model_large,
        "period": period,
        "msg_count": len(msgs),
        "in_tokens": usage.get("prompt_tokens", len(transcript) // 4),
        "out_tokens": usage.get("completion_tokens", 0),
    }


def _main() -> None:
    import asyncio
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s")
    if len(sys.argv) < 2:
        print("usage: summarize <tg_chat_id> [hours]")
        sys.exit(2)
    chat_id = int(sys.argv[1])
    hours = int(sys.argv[2]) if len(sys.argv) > 2 else None  # None = whole channel

    res = asyncio.run(summarize_channel(chat_id, hours=hours))
    if not res["ok"]:
        print(f"FAIL: {res['error']}")
        sys.exit(1)
    print(f"\n=== SUMMARY ({res['msg_count']} msgs, {res['period']}, "
          f"~{res['in_tokens']}→{res['out_tokens']} tok, model={res['model']}) ===\n")
    print(res["summary"])


if __name__ == "__main__":
    _main()
