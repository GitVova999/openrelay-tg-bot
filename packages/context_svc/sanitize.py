"""Post-process LLM output for safe delivery to Telegram.

LLMs like DeepSeek V4 / MiniMax emit reasoning blocks in `<think>…</think>`,
`<reasoning>…</reasoning>`, or similar tags before the actual answer.  We
strip those, then HTML-escape the payload so any residual `<`/`>`/`&` don't
crash the aiogram HTML parser.

The stripping is intentionally regex-based, not a full HTML parse — we only
want to remove the noise, not turn arbitrary strings into safe HTML.
"""

from __future__ import annotations

import html
import re

_THINK_TAG_RE = re.compile(
    r"<(?:think|thought|reasoning|analysis|scratchpad)[^>]*>.*?</(?:think|thought|reasoning|analysis|scratchpad)>",
    re.IGNORECASE | re.DOTALL,
)
_UNCLOSED_THINK_RE = re.compile(
    r"<(?:think|thought|reasoning|analysis|scratchpad)[^>]*>.*?(?=$|\n\n)",
    re.IGNORECASE | re.DOTALL,
)

# Tags we treat as safe to keep as HTML in the final message.  Anything else
# emitted by the model gets escaped.
_SAFE_TAGS = ("b", "i", "u", "s", "code", "pre", "a", "br")


def strip_reasoning(text: str) -> str:
    """Remove <think>…</think> blocks (paired) and dangling ones (unpaired)."""
    text = _THINK_TAG_RE.sub("", text)
    text = _UNCLOSED_THINK_RE.sub("", text)
    return text.strip()


def escape_for_html(text: str) -> str:
    """Escape < > & for TG HTML parser, but preserve our own <b>/<i>/etc."""
    escaped = html.escape(text, quote=False)
    for tag in _SAFE_TAGS:
        escaped = escaped.replace(f"&lt;{tag}&gt;", f"<{tag}>")
        escaped = escaped.replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return escaped


_LEAKED_REASONING_STARTERS = (
    "we need to", "we should", "let me", "let us", "let's think",
    "first, let", "actually,", "the user is", "the user wants",
    "the user asks", "the user has", "so, the user", "ok, ",
    "okay, ", "so, we", "so we ", "i need to", "i should",
    "i'll ", "i will ", "looking at",
)

_RU_CHARS_RE = re.compile(r"[а-яА-ЯёЁ]")
_BULLET_RE = re.compile(r"^\s*(?:[-•*]|\d+\.)\s+", re.MULTILINE)


def strip_leaked_reasoning(text: str, expected_lang: str = "ru") -> str:
    """Cut leading paragraphs that look like model's chain-of-thought.

    Trigger: expected_lang=='ru' AND the first paragraph opens with a known
    English reasoning marker AND has very few Cyrillic chars.  We walk down
    paragraph by paragraph until we find one that looks like the real answer
    (bullet list, or Cyrillic-dense prose).

    Conservative — if unsure, we return the original text.  Only bites when
    the model has clearly derailed.
    """
    if expected_lang != "ru":
        return text
    paragraphs = text.split("\n\n")
    stripped = 0
    for i, p in enumerate(paragraphs):
        head = p.strip().lower()[:80]
        if not head:
            stripped = i + 1
            continue
        # Looks like a real Russian answer? Keep everything from here.
        if _BULLET_RE.search(p):
            return "\n\n".join(paragraphs[i:]).strip()
        ru_ratio = len(_RU_CHARS_RE.findall(p)) / max(len(p), 1)
        if ru_ratio > 0.10:  # dense Cyrillic = actual answer
            return "\n\n".join(paragraphs[i:]).strip()
        # English-reasoning marker at the start of the paragraph?
        if any(head.startswith(s) for s in _LEAKED_REASONING_STARTERS):
            stripped = i + 1
            continue
        # Sparse Cyrillic + no marker → still probably reasoning, keep chopping.
        if ru_ratio < 0.02 and len(p) > 40:
            stripped = i + 1
            continue
        # Otherwise stop — this paragraph might be the answer.
        break
    result = "\n\n".join(paragraphs[stripped:]).strip()
    return result or text  # never return empty from this pass


def clean_llm_output(text: str, expected_lang: str = "ru") -> str:
    """One-shot cleaner: strip <think> tags + leaked reasoning + escape HTML."""
    text = strip_reasoning(text)
    text = strip_leaked_reasoning(text, expected_lang=expected_lang)
    return escape_for_html(text)
