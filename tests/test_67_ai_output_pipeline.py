from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.ai.tools.delivery import SAFE_LIMIT, _format_chunks, process_output, _entity_valid


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
    assert all(len(chunk) <= SAFE_LIMIT for chunk in chunks)
    assert "🙂" * (SAFE_LIMIT + 20) in "".join(chunks)


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
