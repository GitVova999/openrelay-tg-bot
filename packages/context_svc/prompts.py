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


def build_system_prompt(ch: Channel, task: str = "assistant") -> str:
    """Return the system message for this channel's LLM calls.

    `task` = "assistant" | "digest" | "summary" | "faq" — currently only
    tweaks the wording; kept as a parameter so we can specialize later.
    """
    if ch.system_prompt:
        return ch.system_prompt

    lang = (ch.language or "ru").lower()
    lang_name = _LANG_NAME.get(lang, lang)
    clamp = _LANG_CLAMP.get(lang, _LANG_CLAMP["ru"])

    parts = [
        f"Ты — AI-помощник Telegram-канала «{ch.title}».",
        f"Отвечай на {lang_name} языке. {clamp}",
        "Никаких <think>, <reasoning>, <analysis> блоков — сразу ответ.",
    ]
    if ch.topics:
        parts.append(f"Тематика канала: {ch.topics}.")
    if ch.tone:
        parts.append(f"Тон и стиль: {ch.tone}.")
    else:
        parts.append("Стиль: нейтральный, по делу, без канцелярита и извинений.")
    return "\n".join(parts)
