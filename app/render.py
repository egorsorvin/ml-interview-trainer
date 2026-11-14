"""Markdown + формулы ($..$, $$..$$) → HTML. Формулы дорисовывает KaTeX на клиенте."""

import re

from markdown_it import MarkdownIt
from markupsafe import Markup
from mdit_py_plugins.dollarmath import dollarmath_plugin

_md = (
    MarkdownIt("commonmark", {"html": False})
    .enable(["table", "strikethrough"])
    .use(dollarmath_plugin, allow_space=True, allow_digits=True, double_inline=True)
)

_CODE = re.compile(r"(```.*?```|`[^`\n]+`)", re.S)
_DISPLAY = re.compile(r"\\\[(.*?)\\\]", re.S)
_INLINE = re.compile(r"\\\((.*?)\\\)", re.S)


def _normalize_math(text: str) -> str:
    """LLM иногда пишет \\(..\\) и \\[..\\] вместо $..$ — приводим к $ вне блоков кода."""
    parts = _CODE.split(text)
    for i in range(0, len(parts), 2):
        parts[i] = _DISPLAY.sub(lambda m: "$$" + m.group(1) + "$$", parts[i])
        parts[i] = _INLINE.sub(lambda m: "$" + m.group(1) + "$", parts[i])
    return "".join(parts)


def md(text: str | None) -> Markup:
    return Markup(_md.render(_normalize_math(text or "")))


def md_inline(text: str | None) -> Markup:
    return Markup(_md.renderInline(_normalize_math(text or "")))
