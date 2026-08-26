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


def clean_llm_output(text: str) -> str:
    """One-shot cleaner: strip reasoning + escape for TG HTML."""
    return escape_for_html(strip_reasoning(text))
