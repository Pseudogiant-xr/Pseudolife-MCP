"""Document parsing and text extraction for RAG ingestion.

Supports PDF (pypdfium2/pypdf), Markdown, HTML (BeautifulSoup), and plain text.
Provides a unified interface for extracting clean text from various file formats.
"""

from __future__ import annotations

from pathlib import Path


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
        return path.read_text(encoding="utf-8", errors="replace")
    else:
        # Try reading as text
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise ValueError(f"Cannot read file {path.name}: {e}")


def _extract_pdf(path: Path) -> str:
    """Extract text from PDF using pypdfium2 or pypdf as fallback."""
    # Try pypdfium2 first (Chromium's PDFium; better extraction quality).
    # Permissively licensed (Apache-2.0/BSD-3), unlike the AGPL PyMuPDF it
    # replaced — keep this path copyleft-free.
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
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
    except ImportError:
        pass

    # Fall back to pypdf (maintained successor to PyPDF2; same PdfReader API)
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)
    except ImportError:
        raise ValueError("No PDF library available. Install pypdfium2 or pypdf.")
    except Exception as e:
        raise ValueError(f"Failed to read PDF: {e}")


def _extract_html(path: Path) -> str:
    """Extract text from HTML using BeautifulSoup."""
    try:
        from bs4 import BeautifulSoup
        html = path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        # Remove script and style elements
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()
        return soup.get_text(separator="\n", strip=True)
    except ImportError:
        # Fallback: regex-based tag stripping
        import re
        html = path.read_text(encoding="utf-8", errors="replace")
        clean = re.sub(r"<[^>]+>", " ", html)
        clean = re.sub(r"\s+", " ", clean)
        return clean.strip()
    except Exception as e:
        raise ValueError(f"Failed to read HTML: {e}")
