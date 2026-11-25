"""Upload validation, malware-scan hook and text extraction.

Accepted: plain text, Markdown, PDF (text layer). Type is decided from content (magic bytes and
UTF-8 validity), never from the filename or client-sent content type.
Limits: RAG_MAX_UPLOAD_BYTES (5 MiB), RAG_MAX_PDF_PAGES (200), extracted text RAG_MAX_TEXT_CHARS.
Scanning: if MALWARE_SCAN_URL is set, the bytes are POSTed there (e.g. a ClamAV REST sidecar)
and anything but {"clean": true} is rejected. RAG_REQUIRE_SCAN=true refuses uploads when no
scanner is configured (set it in deployed environments).
OCR of scanned PDFs is not supported (planned); such PDFs fail with "no extractable text".
"""

import io
import os
import unicodedata

import httpx

MAX_BYTES = int(os.getenv("RAG_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))
MAX_PAGES = int(os.getenv("RAG_MAX_PDF_PAGES", "200"))
MAX_TEXT = int(os.getenv("RAG_MAX_TEXT_CHARS", "2000000"))


class Rejected(Exception):
    """Reason is safe to show to the uploader."""


def detect_type(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected("Unsupported file type: upload PDF, Markdown or plain text") from None
    if "\x00" in text:
        raise Rejected("Unsupported file type: upload PDF, Markdown or plain text")
    return "text/markdown" if any(text.lstrip().startswith(p) for p in ("#", "---")) else "text/plain"


def validate(data: bytes) -> str:
    if not data:
        raise Rejected("Empty file")
    if len(data) > MAX_BYTES:
        raise Rejected(f"File too large (max {MAX_BYTES // (1024 * 1024)} MiB)")
    return detect_type(data)


async def scan(data: bytes) -> str:
    """Returns 'clean' or 'not_scanned'; raises Rejected for infected or unscannable files."""
    url = os.getenv("MALWARE_SCAN_URL", "").strip()
    if not url:
        if os.getenv("RAG_REQUIRE_SCAN", "false").lower() in {"1", "true", "yes"}:
            raise Rejected("Uploads are disabled: no malware scanner configured")
        return "not_scanned"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, content=data, headers={"Content-Type": "application/octet-stream"})
    if r.status_code != 200 or r.json().get("clean") is not True:
        raise Rejected("File rejected by malware scan")
    return "clean"


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return text.strip()[:MAX_TEXT]


def extract(data: bytes, mime: str) -> str:
    if mime == "application/pdf":
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise Rejected("Encrypted PDFs are not supported")
            if len(reader.pages) > MAX_PAGES:
                raise Rejected(f"PDF has too many pages (max {MAX_PAGES})")
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except PdfReadError:
            raise Rejected("Could not read PDF") from None
    else:
        text = data.decode("utf-8")
    text = _clean(text)
    if len(text) < 20:
        raise Rejected("No extractable text (scanned PDFs need OCR, which is not supported yet)")
    return text
