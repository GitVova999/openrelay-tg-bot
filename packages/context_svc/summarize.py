"""Summarize a channel's messages over a time window.

MVP path — direct inference call (no retrieval; whole window fits in-context).
Once channels grow past ~50k tokens per window we switch to hierarchical
digests + retrieval; the interface stays the same.

Inference route: OpenRelay /v1/premium/chat/completions with real x402/CDP
payment from OPENRELAY_WALLET_PRIVATE_KEY.  Full e2e — no Gonka-direct
bypass — so this exercises the same path external consumers use.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from packages.common.config import settings
from packages.common.db import session
from packages.common.models import Channel, Message
from packages.context_svc.digest import build_context_for_query, ensure_digests
from packages.context_svc.openrelay_client import chat_completion
from packages.context_svc.sanitize import strip_reasoning

log = logging.getLogger("summarize")


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


# If the caller asked for a window > this many hours we skip raw messages and
# summarize from cached daily digests. Cheaper + fits any window.
RAW_WINDOW_CAP_HOURS = 48


async def summarize_channel(
    tg_chat_id: int,
    hours: int | None = 24,
    since: datetime | None = None,
    max_output_tokens: int = 900,
) -> dict:
    """Return {ok, summary, model, in_tokens_est, msg_count, period}."""
    s = settings()

    if since is None and hours is not None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)

    async with session() as db:
        ch = (await db.execute(select(Channel).where(Channel.tg_chat_id == tg_chat_id))).scalar_one_or_none()
    if ch is None:
        return {"ok": False, "error": f"канал {tg_chat_id} не зарегистрирован"}

    # Sanity: does the channel have ANY messages, and where do they live?
    async with session() as db:
        oldest = (await db.execute(
            select(Message.sent_at).where(Message.channel_id == ch.id).order_by(Message.sent_at).limit(1)
        )).scalar_one_or_none()
        newest = (await db.execute(
            select(Message.sent_at).where(Message.channel_id == ch.id).order_by(Message.sent_at.desc()).limit(1)
        )).scalar_one_or_none()

    if oldest is None:
        return {"ok": False, "error": "в канале ещё нет распарсенных сообщений"}

    msgs = await _fetch_window(ch.id, since, limit=None)
    if not msgs:
        return {
            "ok": False,
            "error": (
                f"нет сообщений в этом окне.\n"
                f"Данные канала: c {oldest:%Y-%m-%d} по {newest:%Y-%m-%d}.\n"
                f"Попробуй /summarize {int((datetime.now(timezone.utc) - oldest).total_seconds() // 3600)+1} "
                f"(весь канал) или уточни другое окно."
            ),
        }

    # Cheap window (< 48 h): send raw messages verbatim.
    # Big window: pre-compute daily digests + feed digests to model.
    if hours is not None and hours <= RAW_WINDOW_CAP_HOURS:
        transcript_block = _format_transcript(msgs)
        prompt_body = f"===\n{transcript_block}\n==="
        est_in = len(transcript_block) // 4
    else:
        # Make sure digests up to yesterday exist.
        await ensure_digests(ch.id)
        context, cstats = await build_context_for_query(ch.id, recent_hours=RAW_WINDOW_CAP_HOURS)
        prompt_body = context
        est_in = cstats["context_tokens_est"]

    from packages.context_svc.prompts import STOP_SEQUENCES, build_system_prompt
    period = f"с {msgs[0].sent_at:%Y-%m-%d} по {msgs[-1].sent_at:%Y-%m-%d}"
    user_prompt = (
        f"Ниже — контекст канала за период {period}.\n"
        "Сделай краткое саммари: главные темы, повторяющиеся мотивы, тон, что-либо необычное.\n"
        "Формат: 5-8 буллетов через дефис. Без вступительной фразы, сразу буллеты.\n\n"
        + prompt_body
    )
    system_prompt = build_system_prompt(ch, task="summary")

    try:
        j = await chat_completion(
            model=s.model_large,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_output_tokens,
            temperature=0.4,
            stop=STOP_SEQUENCES,
        )
    except Exception as e:
        return {"ok": False, "error": f"инференс упал: {e!s}"[:400]}

    summary = strip_reasoning(j["choices"][0]["message"]["content"])
    usage = j.get("usage", {})
    return {
        "ok": True,
        "summary": summary,
        "model": j.get("model", s.model_large),
        "period": period,
        "msg_count": len(msgs),
        "in_tokens": usage.get("prompt_tokens", est_in),
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
