import pytest
from app.bot.formatting import markdown_to_telegram_html


def test_plain_text_and_escaping():
    assert markdown_to_telegram_html("Hello world") == "Hello world"
    assert markdown_to_telegram_html("5 < 10 & 20 > 15") == "5 &lt; 10 &amp; 20 &gt; 15"


def test_bold_formatting():
    assert markdown_to_telegram_html("**жирный текст**") == "<b>жирный текст</b>"
    assert markdown_to_telegram_html("__тоже жирный__") == "<b>тоже жирный</b>"


def test_italic_formatting():
    assert markdown_to_telegram_html("*курсив*") == "<i>курсив</i>"
    assert markdown_to_telegram_html("_курсив_") == "<i>курсив</i>"
    # Should not break snake_case identifiers
    assert markdown_to_telegram_html("user_id_field and bot_record_status") == "user_id_field and bot_record_status"


def test_strikethrough():
    assert markdown_to_telegram_html("~~зачеркнуто~~") == "<s>зачеркнуто</s>"


def test_inline_code():
    assert markdown_to_telegram_html("используйте `/operator` для вызова") == "используйте <code>/operator</code> для вызова"
    assert markdown_to_telegram_html("код с `<тегами>`: `<div>`") == "код с <code>&lt;тегами&gt;</code>: <code>&lt;div&gt;</code>"


def test_code_blocks():
    md = "```python\ndef foo():\n    return 1 < 2\n```"
    expected = "<pre><code>def foo():\n    return 1 &lt; 2</code></pre>"
    assert markdown_to_telegram_html(md) == expected


def test_links():
    assert (
        markdown_to_telegram_html("[Личный кабинет](https://lk.peipivo.top)")
        == '<a href="https://lk.peipivo.top">Личный кабинет</a>'
    )
    # Ignore invalid / javascript links
    assert (
        markdown_to_telegram_html("[Click](javascript:alert(1))")
        == "[Click](javascript:alert(1))"
    )


def test_blockquotes():
    assert markdown_to_telegram_html("> Важное сообщение\n> Вторая строка") == "<blockquote>Важное сообщение\nВторая строка</blockquote>"


def test_mixed_formatting():
    text = "Проверьте **кнопку** в `@PeipivoSalesBot`:\n* Шаг 1: `ping < 50`\n* Шаг 2: [Подробнее](https://lk.peipivo.top)"
    result = markdown_to_telegram_html(text)
    assert "<b>кнопку</b>" in result
    assert "<code>@PeipivoSalesBot</code>" in result
    assert "<code>ping &lt; 50</code>" in result
    assert '<a href="https://lk.peipivo.top">Подробнее</a>' in result


def test_empty_or_none():
    assert markdown_to_telegram_html("") == ""
    assert markdown_to_telegram_html("   ") == "   "
    assert markdown_to_telegram_html(None) is None
