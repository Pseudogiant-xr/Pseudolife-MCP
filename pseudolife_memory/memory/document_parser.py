"""Document parsing and text extraction for RAG ingestion.

Supports PDF (pypdfium2/pypdf), Markdown, HTML (BeautifulSoup), and plain text.
Provides a unified interface for extracting clean text from various file formats.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

# The largest file document_ingest reads, in bytes (64 MiB). An estimate,
# not a measurement (2026-10-25): a 1,000-page technical manual as a PDF
# with figures runs to tens of megabytes, its text to a small part of that.
# A text file this size is ~16M tokens, ~37,000 chunks of 512 tokens with
# their overlap, about 16 hours of CPU embedding at the ~1.6 s per chunk
# measured for INGEST_ENCODE_BATCH (reference_bank.py), so the cap stops a
# mistaken path (a disk image, an archive) before it is read into memory,
# not a real document. The Rust daemon pins the same value
# (rust/daemon/src/documents.rs).
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024


class DocumentRefused(ValueError):
    """A ``code: detail`` refusal of the file itself (its kind or size),
    raised as it is: no extraction branch wraps it."""


def file_problem(path: Path, st: os.stat_result) -> str | None:
    """Why a file with this ``stat`` cannot be ingested, as a ``code:
    detail`` refusal, or ``None``: only a regular file of at most
    ``MAX_DOCUMENT_BYTES`` is read. A directory, FIFO, socket or device
    would fail, block or never end."""
    mode = st.st_mode
    if not stat.S_ISREG(mode):
        kind = ("a directory" if stat.S_ISDIR(mode)
                else "a FIFO" if stat.S_ISFIFO(mode)
                else "a socket" if stat.S_ISSOCK(mode)
                else "a character device" if stat.S_ISCHR(mode)
                else "a block device" if stat.S_ISBLK(mode)
                else "not a regular file")
        return (f"not_a_regular_file: {path} is {kind}; document_ingest reads one "
                f"regular file, not a directory, FIFO, socket or device")
    if st.st_size > MAX_DOCUMENT_BYTES:
        return (f"document_too_large: {path} is {st.st_size} bytes; document_ingest "
                f"reads files of at most {MAX_DOCUMENT_BYTES} bytes: split the "
                f"document or ingest the part you need")
    return None


def read_document_bytes(path: Path) -> bytes:
    """The file's bytes, read once through a bound. Opened without blocking
    (a FIFO put in its place after the caller's check cannot hold the
    call), checked again on the open handle, then read at most one byte
    past the cap: a file that grows, or one that reports no size (a
    ``/proc`` entry), is refused rather than read without end."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as f:
        problem = file_problem(path, os.fstat(f.fileno()))
        if problem is not None:
            raise DocumentRefused(problem)
        # In 64 KiB blocks, not one cap-sized read, which would reserve the
        # whole cap for every file.
        blocks: list[bytes] = []
        total = 0
        while total <= MAX_DOCUMENT_BYTES:
            block = f.read(64 * 1024)
            if not block:
                break
            blocks.append(block)
            total += len(block)
    data = b"".join(blocks)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentRefused(
            f"document_too_large: {path} holds more than {MAX_DOCUMENT_BYTES} bytes; "
            f"document_ingest reads files of at most {MAX_DOCUMENT_BYTES} bytes: split "
            f"the document or ingest the part you need")
    return data


def _read_text(path: Path) -> str:
    """``Path.read_text(encoding="utf-8", errors="replace")`` through the
    bounded read: the same replacement decoding and text-mode newlines."""
    text = read_document_bytes(path).decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def extract_text(path: Path) -> str:
    """Extract text content from a file based on its extension.

    Supports: .txt, .md, .pdf, .html, .htm

    Args:
        path: Path to the file.

    Returns:
        Extracted text content.

    Raises:
        ValueError: If the file type is unsupported or reading fails.
    """
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return _extract_pdf(path)
    elif suffix in (".html", ".htm"):
        return _extract_html(path)
    elif suffix in (".txt", ".md", ".markdown", ".rst", ".csv", ".json", ".yaml", ".yml"):
        return _read_text(path)
    else:
        # Try reading as text
        try:
            return _read_text(path)
        except DocumentRefused:
            raise
        except Exception as e:
            raise ValueError(f"Cannot read file {path.name}: {e}")


def _extract_pdf(path: Path) -> str:
    """Extract text from PDF using pypdfium2 or pypdf as fallback."""
    # Try pypdfium2 first (Chromium's PDFium; better extraction quality).
    # Permissively licensed (Apache-2.0/BSD-3), unlike the AGPL PyMuPDF it
    # replaced — keep this path copyleft-free.
    try:
        import pypdfium2 as pdfium
    except (ImportError, OSError):
        # Not installed, or its bundled library cannot be loaded here
        # (ctypes raises OSError): pypdf reads the file instead.
        pdfium = None
    if pdfium is not None:
        data = read_document_bytes(path)
        try:
            pdf = pdfium.PdfDocument(data)
            try:
                pages = []
                for page in pdf:
                    textpage = page.get_textpage()
                    pages.append(textpage.get_text_range())
                    textpage.close()
                    page.close()
                return "\n\n".join(pages)
            finally:
                pdf.close()
        except pdfium.PdfiumError as e:
            # The same refusal an unreadable document gives on the pypdf
            # path, with PDFium's reason (a wrong password, not a PDF).
            raise ValueError(f"Failed to read PDF: {e}") from e

    # Fall back to pypdf (maintained successor to PyPDF2; same PdfReader API)
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(read_document_bytes(path)))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)
    except ImportError:
        raise ValueError("No PDF library available. Install pypdfium2 or pypdf.")
    except DocumentRefused:
        raise
    except Exception as e:
        raise ValueError(f"Failed to read PDF: {e}")


def _extract_html(path: Path) -> str:
    """Extract text from HTML using BeautifulSoup."""
    try:
        from bs4 import BeautifulSoup
        html = _read_text(path)
        soup = BeautifulSoup(html, "html.parser")
        # Remove script and style elements
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()
        return soup.get_text(separator="\n", strip=True)
    except ImportError:
        # Fallback: regex-based tag stripping
        import re
        html = _read_text(path)
        clean = re.sub(r"<[^>]+>", " ", html)
        clean = re.sub(r"\s+", " ", clean)
        return clean.strip()
    except DocumentRefused:
        raise
    except Exception as e:
        raise ValueError(f"Failed to read HTML: {e}")
