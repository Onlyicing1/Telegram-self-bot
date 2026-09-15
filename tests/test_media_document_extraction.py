"""
Media Processing M1.2 — bounded PDF/DOCX document extraction.

The M1 boundary (``backend/services/media_service.py``) is unchanged in shape;
this file pins the M1.2 addition to it:

  1. PDF and DOCX are extractable WITHOUT touching the transfer, validation,
     cleanup, zero-context or fail-closed contracts of M1.
  2. Extraction is finite: page count, XML element count, archive entry bytes
     and extracted characters all have hard module bounds.
  3. Container signatures corroborate the declared MIME — a file that merely
     claims to be a PDF/DOCX never reaches the parser as if it were one.
  4. DOCX is an UNTRUSTED archive: only ``word/document.xml`` is read, nothing
     is unpacked to disk, unrelated/traversal entries are inert, and a body
     declaring a DTD or entity is refused.
  5. Honest outcomes only: a container with no extractable text reports empty
     content with a reason; a malformed, encrypted or unparsable payload raises
     ``MediaError`` (hard failure) instead of fabricated content.
  6. The model-facing rendering (``as_context_text``) still carries no caption,
     sender, chat id, message id or filename.

No live Telegram and no providers: the Telegram boundary is a scripted fake
shaped like the Telethon client surface the facade consumes, while the PDF and
DOCX fixtures are real container bytes built in-process, so ``pypdf``,
``zipfile`` and ElementTree all run for real.
"""
from __future__ import annotations

import io
import os
import tempfile
import zipfile
from typing import Any

import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeFilename,
    MessageMediaDocument,
)

from backend.services import media_service
from backend.services.media_service import (
    MAX_ARCHIVE_ENTRY_BYTES,
    MAX_DOCX_TEXT_ELEMENTS,
    MAX_EXTRACTED_CHARS,
    MAX_PDF_PAGES,
    MediaError,
    MediaStatus,
)

OWNER = 7770001
CHAT = -1007778889999
MESSAGE_ID = 574942
CAPTION = "caption-that-must-never-reach-the-model"
FILE_NAME = "report.pdf"

PDF_MIME = "application/pdf"
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# ── Real PDF fixture builder ────────────────────────────────────────────────


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(page_texts: list[str]) -> bytes:
    """Build a minimal, valid multi-page PDF with one text line per page."""
    font_num = 3
    num = 4
    page_nums: list[int] = []
    content_nums: list[int] = []
    for _ in page_texts:
        page_nums.append(num)
        num += 1
        content_nums.append(num)
        num += 1

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{n} 0 R' for n in page_nums)}] "
        f"/Count {len(page_texts)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for text, page_num, content_num in zip(page_texts, page_nums, content_nums):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            f"<< /Font << /F1 {font_num} 0 R >> >> /Contents {content_num} 0 R >>".encode()
        )
        stream = f"BT /F1 18 Tf 72 700 Td ({_pdf_escape(text)}) Tj ET".encode("latin-1")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"

    xref_pos = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def build_encrypted_pdf(text: str = "Secret") -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(build_pdf([text]))).pages:
        writer.add_page(page)
    writer.encrypt("hunter2")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ── Real DOCX fixture builder ───────────────────────────────────────────────


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _docx_document(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>{body}</w:body></w:document>'
    )


def _paragraph(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r></w:p>'


def build_docx(paragraphs: list[str] | None = None, *, body: str | None = None,
               document_body: str | None = None,
               extra_entries: dict[str, str] | None = None) -> bytes:
    """Build a real DOCX-shaped archive (zip + ``word/document.xml``)."""
    if document_body is None:
        if body is None:
            body = "".join(_paragraph(p) for p in (paragraphs or []))
        document_body = _docx_document(body)
    else:
        document_body = f'<w:document xmlns:w="{_W_NS}"><w:body>{document_body}</w:body></w:document>'

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr("word/document.xml", document_body)
        for name, payload in (extra_entries or {}).items():
            archive.writestr(name, payload)
    return buffer.getvalue()


# ── Fake Telegram surface (the surface the facade consumes) ─────────────────


class _FakeMessage:
    def __init__(self, media: Any, *, caption: str = CAPTION, mid: int = MESSAGE_ID,
                 chat_id: int = CHAT) -> None:
        self.media = media
        self.message = caption
        self.text = caption
        self.id = mid
        self.chat_id = chat_id


class _FakeClient:
    """A scripted Telethon-shaped client that records every transfer."""

    def __init__(self, payload: bytes = b"", *, write: bool = True) -> None:
        self.payload = payload
        self.write = write
        self.calls: list[dict[str, Any]] = []

    async def download_media(self, message: Any, file: Any = None,
                             progress_callback: Any = None, **kwargs: Any) -> Any:
        self.calls.append({"op": "download_media", "message": message, "file": file})
        if self.write and file:
            with open(file, "wb") as handle:
                handle.write(self.payload)
            return file
        return None

    async def get_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.calls.append({"op": "get_messages"})
        raise AssertionError("the media boundary must never search for a message")

    async def iter_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.calls.append({"op": "iter_messages"})
        raise AssertionError("the media boundary must never scan for a message")


def _document_message(mime: str, name: str, size: int, *, caption: str = CAPTION) -> _FakeMessage:
    """A real document message so the existing classifier runs unmodified."""
    doc = Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type=mime, size=size,
        dc_id=1, attributes=[DocumentAttributeFilename(file_name=name)],
    )
    return _FakeMessage(MessageMediaDocument(document=doc), caption=caption)


async def _analyze(payload: bytes, mime: str, name: str, **kwargs: Any):
    client = _FakeClient(payload)
    analysis = await media_service.analyze_media(
        client, OWNER, _document_message(mime, name, len(payload)), **kwargs,
    )
    return client, analysis


# ── 1–3. PDF extraction ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pdf_text_layer_is_extracted_in_page_order():
    payload = build_pdf(["Alpha first", "Bravo second", "Charlie third"])

    client, analysis = await _analyze(payload, PDF_MIME, FILE_NAME)

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.has_content is True
    assert analysis.truncated is False
    order = [analysis.content.index(word) for word in ("Alpha", "Bravo", "Charlie")]
    assert order == sorted(order)
    assert [call["op"] for call in client.calls] == ["download_media"]


@pytest.mark.asyncio
async def test_docx_paragraph_text_is_extracted_in_document_order():
    payload = build_docx(["Alpha paragraph", "Bravo paragraph", "Charlie paragraph"])

    client, analysis = await _analyze(payload, DOCX_MIME, "report.docx")

    assert analysis.status == MediaStatus.EXTRACTED
    order = [analysis.content.index(word) for word in ("Alpha", "Bravo", "Charlie")]
    assert order == sorted(order)
    assert [call["op"] for call in client.calls] == ["download_media"]


@pytest.mark.asyncio
async def test_docx_table_cell_text_is_included():
    body = (
        "<w:tbl><w:tr>"
        f"<w:tc>{_paragraph('Cell One')}</w:tc>"
        f"<w:tc>{_paragraph('Cell Two')}</w:tc>"
        "</w:tr></w:tbl>"
    )

    _, analysis = await _analyze(build_docx(body=body), DOCX_MIME, "table.docx")

    assert analysis.status == MediaStatus.EXTRACTED
    assert "Cell One" in analysis.content
    assert "Cell Two" in analysis.content


# ── 4–6. Honest empty results (no fabricated content) ───────────────────────


@pytest.mark.asyncio
async def test_pdf_with_no_text_layer_reports_empty_content_honestly():
    _, analysis = await _analyze(build_pdf(["", "", ""]), PDF_MIME, "scan.pdf")

    assert analysis.has_content is False
    assert analysis.content == ""
    assert "no extractable text layer" in analysis.reason
    # The M1.1 layer turns a non-content analysis into the deterministic
    # explanation instead of calling a provider (no fabricated answer).
    rendered = analysis.as_context_text()
    assert "Content:" not in rendered
    assert analysis.reason in rendered


@pytest.mark.asyncio
async def test_docx_with_no_extractable_text_reports_empty_content_honestly():
    _, analysis = await _analyze(build_docx([]), DOCX_MIME, "empty.docx")

    assert analysis.has_content is False
    assert analysis.content == ""
    assert "no extractable text" in analysis.reason


# ── 7–9. Bounds are enforced and observable ─────────────────────────────────


@pytest.mark.asyncio
async def test_pdf_page_bound_is_enforced():
    pages = [f"Page{index}" for index in range(MAX_PDF_PAGES + 5)]

    _, analysis = await _analyze(build_pdf(pages), PDF_MIME, "many.pdf")

    assert analysis.truncated is True
    # Non-vacuous: without the page bound the last page would be present too.
    assert f"Page{MAX_PDF_PAGES - 1}" in analysis.content
    assert f"Page{MAX_PDF_PAGES + 4}" not in analysis.content


@pytest.mark.asyncio
async def test_pdf_character_ceiling_is_enforced():
    pages = [f"Page{index} " + ("lorem ipsum dolor sit amet " * 200) for index in range(10)]

    _, analysis = await _analyze(build_pdf(pages), PDF_MIME, "big.pdf")

    assert analysis.truncated is True
    # Non-vacuous: removing the ceiling makes this fail.
    assert len(analysis.content) <= MAX_EXTRACTED_CHARS


@pytest.mark.asyncio
async def test_docx_character_ceiling_is_enforced():
    paragraphs = ["word " * 500 for _ in range(20)]

    _, analysis = await _analyze(build_docx(paragraphs), DOCX_MIME, "long.docx")

    assert analysis.truncated is True
    assert len(analysis.content) <= MAX_EXTRACTED_CHARS


@pytest.mark.asyncio
async def test_docx_element_bound_is_enforced():
    # Empty paragraphs are still walked, so the element bound trips on its own
    # here — the character ceiling cannot mask it.
    paragraphs = ["Alpha"] + [""] * (MAX_DOCX_TEXT_ELEMENTS + 100)

    _, analysis = await _analyze(build_docx(paragraphs), DOCX_MIME, "huge.docx")

    assert analysis.truncated is True
    assert analysis.content == "Alpha"


# ── 10–14. Container signatures and malformed payloads fail closed ──────────


@pytest.mark.asyncio
async def test_pdf_mime_without_a_pdf_header_fails_closed():
    with pytest.raises(MediaError) as exc:
        await _analyze(b"this is plainly not a pdf", PDF_MIME, "liar.pdf")

    assert "%PDF-" in str(exc.value)


@pytest.mark.asyncio
async def test_malformed_pdf_fails_closed():
    with pytest.raises(MediaError):
        await _analyze(b"%PDF-1.4\nnot really a document body", PDF_MIME, "bad.pdf")


@pytest.mark.asyncio
async def test_encrypted_pdf_fails_closed():
    with pytest.raises(MediaError, match="encrypted"):
        await _analyze(build_encrypted_pdf(), PDF_MIME, "locked.pdf")


@pytest.mark.asyncio
async def test_docx_mime_without_a_zip_container_fails_closed():
    with pytest.raises(MediaError, match="readable DOCX container"):
        await _analyze(b"plain text pretending to be docx", DOCX_MIME, "fake.docx")


@pytest.mark.asyncio
async def test_zip_without_document_xml_is_not_a_docx():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("other.xml", "<a/>")

    with pytest.raises(MediaError, match="not a DOCX document"):
        await _analyze(buffer.getvalue(), DOCX_MIME, "missing.docx")


# ── 15–18. DOCX is an untrusted archive ─────────────────────────────────────


@pytest.mark.asyncio
async def test_docx_archive_is_never_unpacked_to_disk():
    def _forbidden(*args: Any, **kwargs: Any):  # pragma: no cover - must never run
        raise AssertionError("the DOCX archive must never be extracted to disk")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(zipfile.ZipFile, "extract", _forbidden)
        patcher.setattr(zipfile.ZipFile, "extractall", _forbidden)
        _, analysis = await _analyze(build_docx(["Safe text"]), DOCX_MIME, "d.docx")

    assert analysis.status == MediaStatus.EXTRACTED
    assert "Safe text" in analysis.content


@pytest.mark.asyncio
async def test_docx_unrelated_and_traversal_entries_are_never_read():
    sentinel = os.path.join(tempfile.gettempdir(), "lifeos_docx_traversal_probe.txt")
    if os.path.exists(sentinel):  # pragma: no cover - defensive
        os.remove(sentinel)

    payload = build_docx(
        ["Only this text"],
        extra_entries={
            "../../../../tmp/lifeos_docx_traversal_probe.txt": "PWNED",
            "word/embeddings/oleObject1.bin": "PWNED",
            "word/vbaProject.bin": "PWNED",
        },
    )

    _, analysis = await _analyze(payload, DOCX_MIME, "evil.docx")

    assert "Only this text" in analysis.content
    assert "PWNED" not in analysis.content
    assert not os.path.exists(sentinel)


@pytest.mark.asyncio
async def test_docx_body_with_a_doctype_or_entity_is_refused():
    payload = build_docx(
        document_body=(
            '<!DOCTYPE t [<!ENTITY x "boom">]>' + _paragraph("&x;")
        )
    )

    with pytest.raises(MediaError, match="unsafe XML construct"):
        await _analyze(payload, DOCX_MIME, "entity.docx")


@pytest.mark.asyncio
async def test_docx_body_beyond_the_entry_bound_fails_closed():
    oversized = build_docx(document_body="x" * (MAX_ARCHIVE_ENTRY_BYTES + 10))

    with pytest.raises(MediaError, match="extraction bound"):
        await _analyze(oversized, DOCX_MIME, "bomb.docx")


# ── 19–21. The M1 contracts still hold ──────────────────────────────────────


@pytest.mark.asyncio
async def test_document_context_text_carries_no_telegram_metadata():
    payload = build_docx(["Document body text"])
    message = _document_message(DOCX_MIME, "SECRET_FILENAME.docx", len(payload))

    client = _FakeClient(payload)
    analysis = await media_service.analyze_media(client, OWNER, message)

    rendered = analysis.as_context_text()
    assert "Document body text" in rendered
    for forbidden in ("SECRET_FILENAME", CAPTION, str(MESSAGE_ID), str(abs(CHAT))):
        assert forbidden not in rendered
    assert "Caption" not in rendered


@pytest.mark.asyncio
async def test_text_extraction_is_unchanged_by_this_phase():
    client = _FakeClient(b'{"a": 1}')
    message = _document_message("application/json", "data.json", 8)

    analysis = await media_service.analyze_media(client, OWNER, message)

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == '{"a": 1}'
    assert analysis.truncated is False
    assert [call["op"] for call in client.calls] == ["download_media"]


@pytest.mark.asyncio
async def test_document_extraction_cleans_up_its_temp_directory():
    payload = build_pdf(["Alpha"])

    _, analysis = await _analyze(payload, PDF_MIME, "cleanup.pdf")

    assert analysis.has_content is True
    leftovers = [n for n in os.listdir(tempfile.gettempdir()) if n.startswith("lifeos_media_")]
    assert leftovers == []


# ── 22–24. Genuinely unsupported types are untouched ────────────────────────


@pytest.mark.asyncio
async def test_types_without_an_extractor_are_still_never_transferred():
    cases = {
        "legacy .doc": ("application/msword", "report.doc"),
        "spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "s.xlsx"),
        "presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "p.pptx"),
        "archive": ("application/zip", "a.zip"),
        "image": ("image/png", "a.png"),
    }

    for label, (mime, name) in cases.items():
        client = _FakeClient(b"x")
        analysis = await media_service.analyze_media(
            client, OWNER, _document_message(mime, name, 1024),
        )

        assert analysis.status == MediaStatus.UNSUPPORTED, label
        assert analysis.content == "", label
        assert analysis.reason, label
        assert client.calls == [], f"{label} must not be transferred"


@pytest.mark.asyncio
async def test_failed_document_analysis_still_cleans_up_and_never_fabricates():
    payload = build_encrypted_pdf()
    client = _FakeClient(payload)

    with pytest.raises(MediaError):
        await media_service.analyze_media(
            client, OWNER, _document_message(PDF_MIME, "locked.pdf", len(payload)),
        )

    assert [call["op"] for call in client.calls] == ["download_media"]
    leftovers = [n for n in os.listdir(tempfile.gettempdir()) if n.startswith("lifeos_media_")]
    assert leftovers == []
