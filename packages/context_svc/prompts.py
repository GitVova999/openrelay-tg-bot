"""Per-channel prompt assembly.

Every LLM call routed through the bot builds its system message here so
language / tone / topic conventions are enforced uniformly and are editable
per-channel via /settings.

If `channel.system_prompt` is set, we use it verbatim (owner override).
Otherwise we assemble from language/tone/topics + a language-clamp footer
that reliably fights the model's English default (Gonka often silently
routes to models that default to English regardless of the user prompt).
"""

from __future__ import annotations

from packages.common.models import Channel

_LANG_CLAMP = {
    "ru": "STRICTLY answer in Russian. Не отвечай на английском ни в каком случае.",
    "en": "Answer strictly in English.",
    "es": "Responde estrictamente en español.",
}

_LANG_NAME = {"ru": "русском", "en": "English", "es": "español"}

# Anti-reasoning clamp — some Gonka-hosted models (notably MiniMax M2.x) leak
# their chain-of-thought inline, in English, without <think> tags, and burn
# the whole token budget on it before writing the final answer.  This chunk
# is prepended to every system prompt to fight that class of behaviour.
_NO_THINK_RULES = (
    "OUTPUT FORMAT — ABSOLUTELY CRITICAL:\n"
    "1. NEVER write reasoning, planning, self-talk, or analysis before your answer.\n"
    "2. NEVER start with meta-phrases like: 'We need to', 'Let me', 'Let us', "
    "'First,', 'Actually,', 'OK,', 'So,', 'Итак,', 'Давайте,', 'Сначала,'.\n"
    "3. NEVER use <think>, <analysis>, <scratchpad>, <reasoning>, or similar tags.\n"
    "4. Your VERY FIRST character must be the very first character of the FINAL answer.\n"
    "5. No drafts, no self-corrections, no summaries-of-your-own-plan. "
    "Just the final answer, in one pass."
)

# Feed these to the OpenAI-compatible `stop` param to hard-cut reasoning drift.
STOP_SEQUENCES = [
    "<think>",
    "<thinking>",
    "<reasoning>",
    "<analysis>",
    "<scratchpad>",
    # Common English reasoning starters after a paragraph break.
    "\n\nWe need to",
    "\n\nWe should",
    "\n\nLet me",
    "\n\nLet us",
    "\n\nLet's think",
    "\n\nFirst, let",
    "\n\nActually,",
    "\n\nThe user is asking",
    "\n\nThe user wants",
]


def build_system_prompt(ch: Channel, task: str = "assistant") -> str:
    """Return the system message for this channel's LLM calls.

    `task` = "assistant" | "digest" | "summary" | "faq" — currently only
    tweaks the wording; kept as a parameter so we can specialize later.
    """
    if ch.system_prompt:
        return ch.system_prompt + "\n\n" + _NO_THINK_RULES

    lang = (ch.language or "ru").lower()
    lang_name = _LANG_NAME.get(lang, lang)
    clamp = _LANG_CLAMP.get(lang, _LANG_CLAMP["ru"])

    parts = [
        f"Ты — AI-помощник Telegram-канала «{ch.title}».",
        f"Отвечай на {lang_name} языке. {clamp}",
    ]
    if ch.topics:
        parts.append(f"Тематика канала: {ch.topics}.")
    if ch.tone:
        parts.append(f"Тон и стиль: {ch.tone}.")
    else:
        parts.append("Стиль: нейтральный, по делу, без канцелярита и извинений.")
    parts.append("")
    parts.append(_NO_THINK_RULES)
    return "\n".join(parts)
