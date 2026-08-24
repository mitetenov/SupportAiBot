"""Markdown to Telegram-compatible HTML formatter."""

from __future__ import annotations

import html
import re
import uuid

_CODE_BLOCK_RE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE1 = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_RE2 = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC_RE1 = re.compile(r"(?<!\w)\*([^*\n]+?)\*(?!\w)")
_ITALIC_RE2 = re.compile(r"(?<!\w)_([^_\n]+?)_(?!\w)")
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(((?:https?://|tg://)[^\)\s]+)\)")
_BLOCKQUOTE_RE = re.compile(r"(?:^[ \t]*(?:&gt;|>)[ \t]?(.*(?:\n[ \t]*(?:&gt;|>).*)*))", re.MULTILINE)


def markdown_to_telegram_html(text: str | None) -> str | None:
    """Convert standard Markdown text into Telegram-compatible HTML.

    Protects code blocks and inline code from escaping/transformations,
    escapes special HTML characters in normal text, and transforms Markdown
    markup to Telegram-supported HTML tags.
    """
    if text is None or not text.strip():
        return text

    placeholders: dict[str, str] = {}

    def _save_placeholder(content: str) -> str:
        key = f"§§MD_PH_{uuid.uuid4().hex}§§"
        placeholders[key] = content
        return key

    # 1. Protect code blocks
    def _replace_code_block(match: re.Match[str]) -> str:
        code_content = match.group(1)
        escaped_code = html.escape(code_content.strip("\r\n"), quote=False)
        return _save_placeholder(f"<pre><code>{escaped_code}</code></pre>")

    processed = _CODE_BLOCK_RE.sub(_replace_code_block, text)

    # 2. Protect inline code
    def _replace_inline_code(match: re.Match[str]) -> str:
        code_content = match.group(1)
        escaped_code = html.escape(code_content, quote=False)
        return _save_placeholder(f"<code>{escaped_code}</code>")

    processed = _INLINE_CODE_RE.sub(_replace_inline_code, processed)

    # 3. Escape HTML characters in non-code text
    processed = html.escape(processed, quote=False)

    # 4. Blockquotes (> lines)
    def _replace_blockquote(match: re.Match[str]) -> str:
        lines = match.group(0).splitlines()
        cleaned_lines: list[str] = []
        for line in lines:
            line_str = line.lstrip()
            if line_str.startswith("&gt;"):
                line_str = line_str[4:]
            elif line_str.startswith(">"):
                line_str = line_str[1:]
            if line_str.startswith(" "):
                line_str = line_str[1:]
            cleaned_lines.append(line_str)
        inner = "\n".join(cleaned_lines)
        return f"<blockquote>{inner}</blockquote>"

    processed = _BLOCKQUOTE_RE.sub(_replace_blockquote, processed)

    # 5. Bold (**text** and __text__)
    processed = _BOLD_RE1.sub(r"<b>\1</b>", processed)
    processed = _BOLD_RE2.sub(r"<b>\1</b>", processed)

    # 6. Italic (*text* and _text_ with word boundaries)
    processed = _ITALIC_RE1.sub(r"<i>\1</i>", processed)
    processed = _ITALIC_RE2.sub(r"<i>\1</i>", processed)

    # 7. Strikethrough (~~text~~)
    processed = _STRIKE_RE.sub(r"<s>\1</s>", processed)

    # 8. Markdown links [text](url)
    def _replace_link(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2)
        return f'<a href="{url}">{label}</a>'

    processed = _LINK_RE.sub(_replace_link, processed)

    # 9. Restore code placeholders
    for placeholder, content in placeholders.items():
        processed = processed.replace(placeholder, content)

    return processed
