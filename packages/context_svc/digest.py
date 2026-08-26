"""Hierarchical digests — pre-computed summaries stored in the `digests` table.

Why: sending the full corpus in every /ask prompt is (a) expensive linearly
with channel size, (b) eventually blows past the model's context window,
(c) forces the model to re-read months of trivia to answer a small question.
Digests compress each day into ~500 tokens once, then all queries reuse
those digests instead of raw messages.

Two levels for MVP:
    - `day` — one row per (channel, YYYY-MM-DD) covering that calendar day.
    - `week` — one row per (channel, YYYY-Www) built by digesting daily digests.

Digests are generated lazily.  When we need context for a query, we call
`ensure_digests_up_to_yesterday(channel_id)` — it materializes any missing
days from the last-cached-day up to yesterday.  Today's messages are always
served raw (they change too often to cache).

`build_context_for_query(channel_id, recent_hours)` returns a compact prompt
context: raw messages for `recent_hours` + all daily digests older than that.
Result is bounded by channel age × ~600 tok/day rather than by message count.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from packages.common.db import session
from packages.common.models import Channel, Digest, Message
from packages.context_svc.openrelay_client import chat_completion
from packages.context_svc.prompts import build_system_prompt
from packages.context_svc.sanitize import strip_reasoning

log = logging.getLogger("digest")

# Cost/quality trade-off: 500 tokens is enough to cover a typical noisy day
# without letting individual quotes bleed through.  Bumping this doesn't
# usually improve answer quality for aggregate questions.
DAY_DIGEST_MAX_TOKENS = 500

_DAY_USER_PROMPT = """Ниже — все сообщения канала за {day}.
Сделай **компактный дайджест дня** для последующего поиска. Формат:

- одна вводная строка: настроение и главная тема дня в 8-15 словах
- 3-8 буллетов конкретных фактов/событий/решений/цитат, каждый до 25 слов
- если есть заметные имена, тикеры, суммы, ссылки — упоминай их дословно

Никакого вступления, никакого <think>-блока, сразу дайджест.

===
{transcript}
==="""


async def _fetch_day_messages(channel_pk: int, day: date) -> list[Message]:
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    async with session() as s:
        return list((await s.execute(
            select(Message)
            .where(Message.channel_id == channel_pk)
            .where(Message.sent_at >= day_start)
            .where(Message.sent_at < day_end)
            .order_by(Message.sent_at)
        )).scalars().all())


def _format_messages(msgs: list[Message]) -> str:
    out: list[str] = []
    for m in msgs:
        stamp = m.sent_at.strftime("%H:%M") if m.sent_at else "?"
        who = (m.sender_name or "?")[:32]
        text = m.text.replace("\n", " ").strip()
        out.append(f"[{stamp}] {who}: {text}")
    return "\n".join(out)


async def _generate_day_digest(channel: Channel, day: date) -> Digest | None:
    """Generate + persist a digest for the given calendar day.

    Returns the Digest row if the day had messages; None if the day was empty.
    Idempotent — will not overwrite an existing digest.
    """
    async with session() as s:
        existing = (await s.execute(
            select(Digest)
            .where(Digest.channel_id == channel.id)
            .where(Digest.scope == "day")
            .where(Digest.scope_key == day.isoformat())
        )).scalar_one_or_none()
        if existing:
            return existing

    msgs = await _fetch_day_messages(channel.id, day)
    if not msgs:
        return None

    transcript = _format_messages(msgs)
    user_prompt = _DAY_USER_PROMPT.format(day=day.isoformat(), transcript=transcript)
    system_prompt = build_system_prompt(channel, task="digest")
    j = await chat_completion(
        model="deepseek-ai/DeepSeek-V4-Flash-0731",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=DAY_DIGEST_MAX_TOKENS,
        temperature=0.3,
    )
    text = strip_reasoning(j["choices"][0]["message"]["content"])
    tokens = j.get("usage", {}).get("completion_tokens", len(text) // 4)

    row = Digest(
        channel_id=channel.id,
        scope="day",
        scope_key=day.isoformat(),
        text=text,
        token_count=tokens,
        covers_from=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
        covers_to=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1),
    )
    async with session() as s:
        s.add(row)
        try:
            await s.flush()
            log.info("digest day=%s channel=%s (%d msgs → %d tok)", day, channel.id, len(msgs), tokens)
            return row
        except Exception:
            # Concurrent insert lost the race — refetch.
            await s.rollback()
            return (await s.execute(
                select(Digest)
                .where(Digest.channel_id == channel.id)
                .where(Digest.scope == "day")
                .where(Digest.scope_key == day.isoformat())
            )).scalar_one_or_none()


# Per-channel lock — prevents two concurrent /ask calls or one bot + one CLI
# process from generating the same digest twice and burning tokens.
import asyncio as _asyncio
_channel_locks: dict[int, _asyncio.Lock] = {}


async def ensure_digests(
    channel_pk: int,
    upto: date | None = None,
    *,
    max_new: int | None = None,
) -> int:
    """Backfill missing daily digests up to `upto` (defaults to yesterday).

    - Iterates ONLY days that actually have messages (skip empty days
      entirely — 300-day-old channel with sparse traffic ≠ 300 DB probes).
    - Skips days already digested.
    - Serialized per channel via an in-process lock; concurrent callers
      wait rather than double-spend on the same day.
    - `max_new` caps how many *new* digests one call may create — lets
      callers offer "prime up to N per pass" pattern without blocking the
      user for the full backlog on first-ever call.
    """
    upto = upto or (datetime.now(timezone.utc).date() - timedelta(days=1))
    lock = _channel_locks.setdefault(channel_pk, _asyncio.Lock())
    async with lock:
        async with session() as s:
            ch = (await s.execute(select(Channel).where(Channel.id == channel_pk))).scalar_one_or_none()
            if ch is None:
                return 0
            # Days with messages
            days_with_msgs = {
                r[0] for r in (await s.execute(
                    select(func.date(Message.sent_at))
                    .where(Message.channel_id == channel_pk)
                    .distinct()
                )).all()
            }
            # Days already digested
            done = {
                r[0] for r in (await s.execute(
                    select(Digest.scope_key)
                    .where(Digest.channel_id == channel_pk)
                    .where(Digest.scope == "day")
                )).all()
            }

        # date() in postgres returns date objects; scope_key stored as isoformat().
        pending = sorted(
            d for d in days_with_msgs
            if d <= upto and d.isoformat() not in done
        )
        if max_new is not None:
            pending = pending[:max_new]

        generated = 0
        for d in pending:
            digest = await _generate_day_digest(ch, d)
            if digest is not None:
                generated += 1
        return generated


async def build_context_for_query(
    channel_pk: int,
    recent_hours: int = 48,
) -> tuple[str, dict]:
    """Return (context_text, stats) suitable for /ask prompt.

    Composition:
        - Raw messages within the last `recent_hours` (verbatim)
        - Daily digests older than `recent_hours` (compressed to ~500 tok/day)

    `stats` reports how many raw messages, how many digests, estimated tokens.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=recent_hours)

    async with session() as s:
        raw = list((await s.execute(
            select(Message)
            .where(Message.channel_id == channel_pk)
            .where(Message.sent_at >= cutoff)
            .order_by(Message.sent_at)
        )).scalars().all())
        digests = list((await s.execute(
            select(Digest)
            .where(Digest.channel_id == channel_pk)
            .where(Digest.scope == "day")
            .where(Digest.covers_to <= cutoff)
            .order_by(Digest.covers_from)
        )).scalars().all())

    parts: list[str] = []
    if digests:
        parts.append("## Дайджесты за предыдущие дни (по одному на день):")
        for d in digests:
            parts.append(f"\n### {d.scope_key}\n{d.text}")
    if raw:
        parts.append(f"\n\n## Последние {recent_hours} часов — сырые сообщения:")
        parts.append(_format_messages(raw))

    context = "\n".join(parts)
    stats = {
        "raw_msgs": len(raw),
        "digest_days": len(digests),
        "context_chars": len(context),
        "context_tokens_est": len(context) // 4,
    }
    return context, stats
