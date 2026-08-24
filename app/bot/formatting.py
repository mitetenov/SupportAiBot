"""Markdown to Telegram-compatible HTML formatter and tag-aware chunk splitter."""

from __future__ import annotations

import html
import re
import urllib.parse
import uuid

MAX_MESSAGE_LENGTH = 4096

_CODE_BLOCK_RE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE1 = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_RE2 = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC_RE1 = re.compile(r"(?<!\w)\*([^*\n]+?)\*(?!\w)")
_ITALIC_RE2 = re.compile(r"(?<!\w)_([^_\n]+?)_(?!\w)")
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(((?:https?://|tg://)[^\)\s]+)\)")
_BLOCKQUOTE_RE = re.compile(
    r"(?:^[ \t]*(?:&gt;|>)[ \t]?(.*(?:\n[ \t]*(?:&gt;|>).*)*))", re.MULTILINE
)
_HTML_TAG_RE = re.compile(r"(</?([a-zA-Z0-9_-]+)(?:\s+[^>]*?)?>)")
_TAG_STRIP_RE = re.compile(r"<[^>]+>")

_ALLOWED_SCHEMES: set[str] = {"http", "https", "tg"}


def _is_valid_url(url: str) -> bool:
    """Validate that a URL has an allowed scheme and a valid target."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
            return False
        if parsed.scheme.lower() in ("http", "https") and not parsed.netloc:
            return False
        return True
    except Exception:
        return False


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

    # 3. Escape HTML characters in non-code text (quotes will be escaped in attributes)
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

    # 8. Markdown links [text](url) with attribute quote escaping and URL validation
    def _replace_link(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2)
        if not _is_valid_url(url):
            return match.group(0)
        escaped_url = html.escape(url, quote=True)
        return f'<a href="{escaped_url}">{label}</a>'

    processed = _LINK_RE.sub(_replace_link, processed)

    # 9. Restore code placeholders
    for placeholder, content in placeholders.items():
        processed = processed.replace(placeholder, content)

    return processed


def strip_html_tags(text: str | None) -> str | None:
    """Strip HTML tags and unescape HTML entities for plain text fallback."""
    if text is None:
        return None
    stripped = _TAG_STRIP_RE.sub("", text)
    return html.unescape(stripped)


def split_telegram_html(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Break Telegram HTML text into chunks <= max_length, safely balancing tags across boundaries."""
    if not text:
        return [""]
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0
    open_tags: list[tuple[str, str]] = []  # (tag_name, full_open_tag)

    def _close_suffix_len() -> int:
        return sum(len(t[0]) + 3 for t in open_tags)

    def _close_suffix_str() -> str:
        return "".join(f"</{t[0]}>" for t in reversed(open_tags))

    def _open_prefix_str() -> str:
        return "".join(t[1] for t in open_tags)

    pos = 0
    while pos < len(text):
        tag_match = _HTML_TAG_RE.search(text, pos)
        if tag_match and tag_match.start() == pos:
            # We are at an HTML tag
            full_tag = tag_match.group(1)
            tag_name = tag_match.group(2).lower()
            is_closing = full_tag.startswith("</")
            pos = tag_match.end()

            if is_closing:
                # Pop matching tag from open_tags (from top of stack)
                for i in range(len(open_tags) - 1, -1, -1):
                    if open_tags[i][0] == tag_name:
                        open_tags.pop(i)
                        break
            else:
                open_tags.append((tag_name, full_tag))

            current_parts.append(full_tag)
            current_len += len(full_tag)
            continue

        # We are at text before next tag (or end of string)
        text_end = tag_match.start() if tag_match else len(text)
        chunk_text = text[pos:text_end]
        pos = text_end

        # Add chunk_text, splitting across chunk boundaries if needed
        text_offset = 0
        while text_offset < len(chunk_text):
            avail = max_length - current_len - _close_suffix_len()
            if avail <= 0 and current_parts:
                # Flush current chunk with closing tags
                chunk_str = "".join(current_parts) + _close_suffix_str()
                chunks.append(chunk_str)
                # Start new chunk with reopened tags
                prefix = _open_prefix_str()
                current_parts = [prefix] if prefix else []
                current_len = len(prefix)
                avail = max_length - current_len - _close_suffix_len()

            rem_text = chunk_text[text_offset:]
            if len(rem_text) <= avail:
                current_parts.append(rem_text)
                current_len += len(rem_text)
                text_offset += len(rem_text)
            else:
                # Need to cut within avail
                cut_len = max(1, avail)
                candidate = rem_text[:cut_len]
                # Try breaking at newline or space
                nl = candidate.rfind("\n")
                if nl >= 0:
                    slice_text = rem_text[:nl]
                    text_offset += nl + 1
                else:
                    sp = candidate.rfind(" ")
                    if sp >= 0:
                        slice_text = rem_text[:sp]
                        text_offset += sp + 1
                    else:
                        slice_text = candidate
                        text_offset += cut_len

                current_parts.append(slice_text)

                # Flush chunk
                chunk_str = "".join(current_parts) + _close_suffix_str()
                chunks.append(chunk_str)

                # Reopen tags for next chunk
                prefix = _open_prefix_str()
                current_parts = [prefix] if prefix else []
                current_len = len(prefix)

    if current_parts:
        final_chunk = "".join(current_parts) + _close_suffix_str()
        if final_chunk:
            chunks.append(final_chunk)

    return chunks if chunks else [""]
