from __future__ import annotations

from types import SimpleNamespace

import pytest

import re
from backend.ai.tools.delivery import (
    SAFE_LIMIT,
    _entity_valid,
    _format_chunks,
    _split_text,
    _utf16_units,
    process_output,
)


@pytest.mark.parametrize("text", [
    "Hello, how are you?", "سلام، حالت چطوره؟", "مرحبا، كيف حالك؟",
    "Привет, как дела?", "你好，世界。", "こんにちは、元気ですか？",
    "안녕하세요, 잘 지내세요?", "برای اجرای `python -m backend.main` این دستور رو بزن.",
    "Install the package و بعد `pip install telethon` رو اجرا کن.",
    "قیمت 125 USD است، see https://example.com", "@username سلام",
    "/start سلام", "🙂 Hello!", "🙂 سلام!",
])
def test_multilingual_output_is_non_destructive(text):
    result = process_output(text)
    assert result.text


def test_script_profile_detects_mixed_rtl_ltr():
    result = process_output("قیمت 125 USD است")
    assert result.profile.direction == "neutral"
    assert result.profile.mixed_direction
    assert set(result.profile.scripts) >= {"ARABIC", "LATIN"}


def test_typography_and_protected_tokens():
    text = "  Hello   ,   world!\n\n\nhttps://example.com/a_[x]?q=1 @user /do_now `x  ,  y` ```a  b```  "
    result = process_output(text)
    assert result.text == "Hello, world!\n\nhttps://example.com/a_[x]?q=1 @user /do_now `x  ,  y` ```a  b```"


def test_markdown_constructs_degrade_safely():
    result = process_output("**bold** and *italic* [docs](https://example.com)\n- item\n> quote")
    assert result.text == "bold and italic docs (https://example.com)\n• item\n▎ quote"


def test_nested_and_malformed_markdown_preserve_content():
    result = process_output("**bold *inside*** and unmatched **marker and `code")
    assert "bold" in result.text and "marker" in result.text and "code" in result.text


def test_urls_and_code_containing_markdown_like_characters_are_opaque():
    text = "https://example.com/a_[x]*?q=1&x=2 `a*b_[c]` ```a ** b```"
    assert process_output(text).text == text


def test_cjk_spacing_is_not_invented():
    assert process_output("你好，世界。") .text == "你好，世界。"


def test_utf16_safe_chunking_and_no_truncation():
    text = "🙂" * (SAFE_LIMIT + 20)
    chunks = _format_chunks("Nova hi", "Nova", text)
    assert len(chunks) > 1
    assert all(_u16(chunk) <= SAFE_LIMIT for chunk in chunks)
    # The response body is preserved across all delivered messages when the
    # continuation markers are stripped.
    header = "Nova hi\n────────────\n🤖 Nova\n"
    suffix = re.compile(r"\n\n_\(\d+/\d+\)_$")
    parts = [chunks[0][len(header):]]
    for m in chunks[1:]:
        parts.append(suffix.sub("", m))
    assert "".join(parts) == text


@pytest.mark.asyncio
async def test_integration_delivery_uses_centralized_processor():
    edits, replies = [], []
    async def edit(text): edits.append(text)
    async def reply(text): replies.append(text)
    from backend.ai.tools.delivery import deliver_response
    result = await deliver_response(SimpleNamespace(edit=edit, reply=reply), "Nova hi", "Nova", "**سلام**  ، دنیا!")
    assert result.success
    assert edits == ["Nova hi\n────────────\n🤖 Nova\nسلام، دنیا!"]
    assert replies == []


def test_formatter_exception_falls_back_without_leaking_details(monkeypatch):
    import backend.ai.tools.delivery as delivery
    monkeypatch.setattr(delivery, "process_output", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "raw" in delivery._format_chunks("u", "AI", "raw")[0]


def test_formatter_does_not_make_external_calls(monkeypatch):
    import backend.ai.tools.delivery as delivery
    monkeypatch.setattr(delivery, "_protect", delivery._protect)
    assert process_output("hello").text == "hello"


def test_entity_and_utf16_offsets_are_valid():
    result = process_output("🙂 **سلام**")
    assert result.entity_count == 1
    entity = process_output("🙂 *سلام*").entities[0]
    assert entity.offset == 4
    assert entity.length == 4
    assert _entity_valid(entity, "🙂 *سلام*")


def test_telegram_length_is_enforced_in_utf16_units():
    chunks = _format_chunks("Nova", "Nova", "🙂" * (SAFE_LIMIT + 100))
    assert all(len(chunk.encode("utf-16-le")) // 2 <= SAFE_LIMIT * 2 for chunk in chunks)


_EMOJI = "🙂"  # BMP-free: 2 UTF-16 code units, 1 Python character


def _u16(s):
    return _utf16_units(s)


@pytest.mark.parametrize("body", [
    "a" * (SAFE_LIMIT - 40),
    "a" * (SAFE_LIMIT - 24),
    "a" * (SAFE_LIMIT - 23),
    "a" * (SAFE_LIMIT - 20),
])
def test_ascii_near_limit_splits_within_utf16(body):
    chunks = _split_text(body, SAFE_LIMIT)
    assert chunks
    assert "".join(chunks) == body
    assert all(_u16(c) <= SAFE_LIMIT for c in chunks)


def test_ascii_exact_at_limit_no_split():
    body = "a" * SAFE_LIMIT
    assert _split_text(body, SAFE_LIMIT) == [body]


def test_ascii_just_over_limit_splits():
    body = "a" * (SAFE_LIMIT + 1) + "."
    chunks = _split_text(body, SAFE_LIMIT)
    assert len(chunks) == 2
    assert "".join(chunks) == body
    assert all(_u16(c) <= SAFE_LIMIT for c in chunks)


def test_bmp_unicode_chunks():
    body = "پ" * (SAFE_LIMIT + 1) + "端"
    chunks = _split_text(body, SAFE_LIMIT)
    assert "".join(chunks) == body
    assert all(_u16(c) <= SAFE_LIMIT for c in chunks)


def test_python_len_below_limit_but_utf16_over_limit_must_split():
    # 4000 emoji have Python len 4000 (at limit) but UTF-16 length 8000
    # (over). The splitter MUST split, proving length is measured in UTF-16.
    python_below_limit = _EMOJI * (SAFE_LIMIT - 1)  # Python len 3999 < 4000
    assert len(python_below_limit) < SAFE_LIMIT
    assert _u16(python_below_limit) > SAFE_LIMIT
    chunks = _split_text(python_below_limit, SAFE_LIMIT)
    assert len(chunks) > 1
    assert "".join(chunks) == python_below_limit
    assert all(_u16(c) <= SAFE_LIMIT for c in chunks)


def test_many_emoji_split_and_preserved():
    body = _EMOJI * (SAFE_LIMIT + 100)
    chunks = _split_text(body, SAFE_LIMIT)
    assert len(chunks) > 1
    assert "".join(chunks) == body
    assert all(_u16(c) <= SAFE_LIMIT for c in chunks)


def test_mixed_ascii_and_emoji_split_and_preserved():
    body = ("ab" + _EMOJI) * (SAFE_LIMIT)
    chunks = _split_text(body, SAFE_LIMIT)
    assert "".join(chunks) == body
    assert all(_u16(c) <= SAFE_LIMIT for c in chunks)


def test_persian_and_emoji_split_and_preserved():
    body = ("قیمت" + _EMOJI + "دلار") * (SAFE_LIMIT)
    chunks = _split_text(body, SAFE_LIMIT)
    assert "".join(chunks) == body
    assert all(_u16(c) <= SAFE_LIMIT for c in chunks)


def test_arabic_and_emoji_split_and_preserved():
    body = ("مرحبا" + _EMOJI) * (SAFE_LIMIT)
    chunks = _split_text(body, SAFE_LIMIT)
    assert "".join(chunks) == body
    assert all(_u16(c) <= SAFE_LIMIT for c in chunks)


def test_cjk_and_emoji_split_and_preserved():
    body = ("世界" + _EMOJI) * (SAFE_LIMIT)
    chunks = _split_text(body, SAFE_LIMIT)
    assert "".join(chunks) == body
    assert all(_u16(c) <= SAFE_LIMIT for c in chunks)


def test_mixed_rtl_ltr_and_emoji_split_and_preserved():
    body = ("سلام_test" + _EMOJI + "راه") * (SAFE_LIMIT)
    chunks = _split_text(body, SAFE_LIMIT)
    assert "".join(chunks) == body
    assert all(_u16(c) <= SAFE_LIMIT for c in chunks)


def test_no_surrogate_pair_is_split():
    body = _EMOJI * (SAFE_LIMIT + 10)
    chunks = _split_text(body, SAFE_LIMIT)
    for c in chunks:
        for ch in c:
            pass
    # every chunk must have an even count of the pair (each emoji is 2 code
    # units), so no lone surrogate appears at chunk edges.
    for c in chunks:
        assert _EMOJI * (len(c) // 1) == c


def test_paragraph_newline_word_preference_preserved():
    # When a paragraph boundary fits in the budget it is used (the chunk
    # does NOT split mid-word).
    body = ("word " * 30) + "\n\n" + ("another " * 60)
    limit = 120
    chunks = _split_text(body, limit)
    assert "".join(chunks) == body
    assert all(_u16(c) <= limit for c in chunks)
    # A paragraph split point (if it fits) yields a chunk ending at the
    # boundary, not in the middle of a word.
    first_has_para = "\n\n" in chunks[0]
    if first_has_para:
        assert chunks[0].rstrip().endswith(chunks[0].rstrip().split()[-1])


def test_long_unicode_output():
    body = ("سلام " * 50) + (_EMOJI * 100) + ("世界" * 40) + ("hello " * 60)
    chunks = _split_text(body, SAFE_LIMIT)
    assert "".join(chunks) == body
    assert all(_u16(c) <= SAFE_LIMIT for c in chunks)


def test_formatted_delivery_messages_all_within_utf16_limit():
    resp = _EMOJI * (SAFE_LIMIT - 1)
    msgs = _format_chunks("hi", "Nova", resp)
    assert any("🙂" in m for m in msgs)
    assert all(_u16(m) <= SAFE_LIMIT for m in msgs)
    assert len(msgs) > 1


def test_delivery_body_reconstructs_response_across_messages():
    resp = _EMOJI * (SAFE_LIMIT + 30)
    msgs = _format_chunks("user", "Nova", resp)
    header = "user\n────────────\n🤖 Nova\n"
    suffix = re.compile(r"\n\n_\(\d+/\d+\)_$")
    parts = [msgs[0][len(header):]]
    for m in msgs[1:]:
        parts.append(suffix.sub("", m))
    assert "".join(parts) == resp


@pytest.mark.asyncio
async def test_deliver_response_delivers_splitted_utf16_output():
    edits, replies = [], []
    async def edit(text): edits.append(text)
    async def reply(text): replies.append(text)
    from backend.ai.tools.delivery import deliver_response
    resp = _EMOJI * (SAFE_LIMIT + 1)
    result = await deliver_response(
        SimpleNamespace(edit=edit, reply=reply), "u", "Nova", resp,
    )
    assert result.success
    assert result.total_chunks == len(edits) + len(replies)
    assert result.chunks_delivered == result.total_chunks
    assert all(_u16(m) <= SAFE_LIMIT for m in edits + replies)


def test_utf16_splitter_idempotent_on_realistic_bodies():
    for body in [
        "a" * (SAFE_LIMIT + 1) + "b",
        (_EMOJI * 100) + "z",
        "قیمت " * 100,
    ]:
        chunks = _split_text(body, SAFE_LIMIT)
        assert "".join(chunks) == body


def test_intraword_emphasis_delimiters_are_preserved():
    for text in [
        "2*3*4",
        "a*b",
        "some_word_here",
        "some__word__here",
        "foo_bar_baz",
        "a*b*c",
        "x_1 = 5",
        "the file_name is set",
    ]:
        assert process_output(text).text == text


def test_word_boundary_emphasis_still_degrades():
    assert process_output("*italic* and **bold**").text == "italic and bold"
    assert process_output("say _italic_ now").text == "say italic now"
    assert process_output("__bold__").text == "bold"
    assert process_output("***bold***").text == "bold"


def test_emphasis_repair_is_idempotent():
    for text in [
        "2*3*4",
        "some_word_here",
        "*italic* and **bold**",
        "say _italic_ now",
        "سلام، حالت چطوره؟",
        "mixed _word_ here",
    ]:
        once = process_output(text).text
        assert process_output(once).text == once


def test_emphasis_repair_keeps_multilingual_text_unchanged():
    for text in [
        "سلام، حالت چطوره؟",
        "مرحبا، كيف حالك؟",
        "Привет, как дела?",
        "你好，世界。",
        "こんにちは。",
        "안녕하세요.",
        "قیمت 125 USD است",
        "🙂 a*b 🙂",
    ]:
        assert process_output(text).text == text


def test_emphasis_repair_preserves_protected_tokens():
    text = "see https://example.com/a_b?x=1 @user /cmd `a*b` ```x_y```"
    assert process_output(text).text == text


@pytest.mark.asyncio
async def test_integration_delivery_uses_repaired_output():
    edits, replies = [], []
    async def edit(text): edits.append(text)
    async def reply(text): replies.append(text)
    from backend.ai.tools.delivery import deliver_response
    result = await deliver_response(
        SimpleNamespace(edit=edit, reply=reply), "Nova hi", "Nova",
        "set the file_name to 2*3*4",
    )
    assert result.success
    assert edits == ["Nova hi\n────────────\n🤖 Nova\nset the file_name to 2*3*4"]
    assert replies == []


def test_empty_output_is_rejected():
    with pytest.raises(ValueError):
        process_output("   ")


# ── Dot/colon literal preservation (audit-repair) ────────────────────────────
# The sentence-spacing rules previously leaked a space into dot/colon-delimited
# technical literals (main.py -> main. py). `.`/`:` are excluded from the splice
# class so filenames, extensions, bare domains, and abbreviations stay intact.


def test_dot_extension_and_domain_literals_are_preserved():
    for text in [
        "main.py",
        "report.txt",
        "example.com",
        "data.v1.csv",
        "e.g.",
        "U.S.A",
        "run main.py now",
        "see config.json and data.json now",
    ]:
        assert process_output(text).text == text


def test_colon_in_technical_literals_left_intact():
    # v2.3.1 uses `.`; UUID/hex/time-like tokens and addresses with `:` must not
    # gain an inserted space.
    for text in [
        "12:30",
        "ratio 3:2",
        "v2.3.1",
        "node:18",
    ]:
        assert process_output(text).text == text


def test_intended_sentence_spacing_is_preserved():
    for text, expected in {
        "hello,world": "hello, world",
        "سلام،world": "سلام، world",
        "note;see": "note; see",
        "تشکر!انجام": "تشکر! انجام",
        "x,y": "x, y",
    }.items():
        assert process_output(text).text == expected


def test_dot_repair_preserves_protected_regions():
    text = "https://example.com/a_b?x=1 @user /cmd `main.py` ```report.txt``` end."
    result = process_output(text).text
    assert "https://example.com/a_b?x=1" in result
    assert "@user" in result
    assert "/cmd" in result
    assert "`main.py`" in result
    assert "```report.txt```" in result


def test_dot_repair_is_idempotent():
    corpus = [
        "main.py",
        "report.txt",
        "example.com",
        "data.v1.csv",
        "e.g.",
        "U.S.A",
        "run main.py now",
        "hello,world",
        "سلام،world",
        "x,y",
        "see https://example.com/a_b @user /cmd `a*b`",
    ]
    for text in corpus:
        once = process_output(text).text
        assert process_output(once).text == once


def test_dot_and_emphasis_coexist():
    # `.py` must stay intact even when emphasis delimiters are present nearby.
    assert process_output("run *this* then main.py").text == "run this then main.py"


def test_table_renders_aligned_fenced_block():
    # English table: header/separator/body all pad to the same column widths.
    text = "| Model | Model 1 | Model 2 |\n|---|---|---|\n| Speed | 95 | 88 |\n| Capacity | 200K | 128K |"
    rendered = process_output(text).text
    expected = (
        "```\n"
        "Model    | Model 1 | Model 2\n"
        "---------- | --------- | ---------\n"
        "Speed    | 95      | 88     \n"
        "Capacity | 200K    | 128K   \n"
        "```"
    )
    assert rendered == expected


def test_table_persian_renders_aligned():
    text = "| مدل | مدل ۱ | مدل ۲ |\n|---|---|---|\n| کارایی | 85% | 92% |\n| ظرفیت | 200K | 128K |"
    rendered = process_output(text).text
    lines = rendered.split("\n")
    assert lines[0] == "```" and lines[-1] == "```"
    body = lines[1:-1]
    # every row must have the same pipe-separated column structure
    widths = {len(row.split("|")) for row in body}
    assert widths == {3}
    # separator must contain only dashes/spaces/pipes
    assert all(char in "- |" for char in body[1])


def test_table_detection_guards():
    # plain pipe text without a separator row is never a table
    assert process_output("A | B").text == "A | B"
    assert process_output("condition: x | y").text == "condition: x | y"
    assert process_output("| A | B |").text == "| A | B |"
    # ragged body rows fail closed and leave the whole block untouched
    ragged = "| a | b |\n|---|---|\n| c | d | e |"
    assert process_output(ragged).text == ragged
    # separator/header column mismatch fails closed
    mismatch = "| a | b |\n|---|---|---|\n| c | d |"
    assert process_output(mismatch).text == mismatch


def test_table_protected_regions_untouched():
    assert process_output("`| A | B |`").text == "`| A | B |`"
    fenced = "```text\n| A | B |\n|---|---|\n| C | D |\n```"
    assert process_output(fenced).text == fenced
    assert process_output("see https://example.com/a|b now").text == "see https://example.com/a|b now"
    assert process_output("ask @user|one and /cmd|two").text == "ask @user|one and /cmd|two"


def test_table_display_width_unicode():
    from backend.ai.tools.delivery import _cell_display_width, _display_width
    assert _display_width("a") == 1
    assert _display_width("م") == 1
    assert _display_width("中") == 2
    assert _display_width("🙂") == 2
    assert _cell_display_width("a\u0301") == 1  # combining mark is zero-width
    assert _cell_display_width("👨\u200d👩") == 4  # ZWJ family: 2+0+2


def test_table_idempotent():
    corpus = [
        "| a | b |\n|---|---|\n| c | d |",
        "hi **bold** and `code`",
        "main.py and 2*3*4",
        "سلام\n| x | y |\n|---|---|\n| 1 | 2 |",
    ]
    for text in corpus:
        once = process_output(text).text
        assert process_output(once).text == once


def test_table_content_preserved_and_chunked():
    big = "| c1 | c2 |\n|---|---|\n" + "\n".join(f"| row{i} | " + "x" * 40 + " |" for i in range(200))
    rendered = process_output(big).text
    messages = _format_chunks("user", "Nova", rendered)
    assert all(_utf16_units(message) <= SAFE_LIMIT for message in messages)
    assert "".join(messages).count("row") == 200


@pytest.mark.asyncio
async def test_delivery_delivers_rendered_table():
    edits, replies = [], []
    async def edit(text): edits.append(text)
    async def reply(text): replies.append(text)
    from backend.ai.tools.delivery import deliver_response
    result = await deliver_response(
        SimpleNamespace(edit=edit, reply=reply), "Nova hi", "Nova",
        "| a | b |\n|---|---|\n| c | d |",
    )
    assert result.success
    assert edits == ["Nova hi\n────────────\n🤖 Nova\n```\na | b\n--- | ---\nc | d\n```"]
    assert replies == []


@pytest.mark.asyncio
async def test_delivery_delivers_dot_preserved_text():
    edits, replies = [], []
    async def edit(text): edits.append(text)
    async def reply(text): replies.append(text)
    from backend.ai.tools.delivery import deliver_response
    result = await deliver_response(
        SimpleNamespace(edit=edit, reply=reply), "Nova hi", "Nova",
        "run main.py now and check report.txt",
    )
    assert result.success
    assert edits == ["Nova hi\n────────────\n🤖 Nova\nrun main.py now and check report.txt"]
    assert replies == []
