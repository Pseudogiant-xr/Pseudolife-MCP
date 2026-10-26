"""document_ingest reads one regular file of bounded size.

``document_ingest`` checked only that its path existed, then read the whole
file: a FIFO blocked the call (and the service lock) until something wrote
to it, a device read without end, and a file of any size was read into
memory at once. Maintainer ruling 2026-10-25 (cortex
``rust-cutover-B7-ingest-hardening``): accept only regular files, refusing
a directory, FIFO, socket or device with an error that names it, and cap
the bytes; the Python and Rust daemons change together.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
import torch

from pseudolife_memory.mcp_server import _error_payload
from pseudolife_memory.memory import document_parser
from pseudolife_memory.memory import reference_bank as rb
from pseudolife_memory.service import MemoryService
from pseudolife_memory.utils.config import ReferenceConfig


class _Embedder:
    def encode(self, texts):
        return torch.tensor([[float(len(t)), 1.0, 0.0, 0.0] for t in texts])


class _Storage:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def upsert_reference_chunks(self, rows, *, replace=True) -> int:
        self.rows.extend(rows)
        return len(rows)


def _service(tmp_path: Path, monkeypatch) -> tuple[MemoryService, _Storage]:
    svc = MemoryService(data_dir=str(tmp_path / "data"))
    monkeypatch.setattr(svc, "_ensure_init", lambda: None)
    storage = _Storage()
    svc._reference = rb.ReferenceBank(ReferenceConfig(), storage, embedding_dim=4)
    svc._embedder = _Embedder()
    return svc, storage


def test_a_directory_is_refused_by_name(tmp_path, monkeypatch) -> None:
    svc, storage = _service(tmp_path, monkeypatch)
    folder = tmp_path / "notes.md"
    folder.mkdir()
    with pytest.raises(ValueError) as info:
        svc.ingest_document(str(folder))
    payload = _error_payload("document_ingest", info.value)
    assert payload["error"] == "not_a_regular_file"
    assert f"{folder} is a directory" in payload["message"]
    assert storage.rows == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX")
def test_a_fifo_is_refused_instead_of_blocking_the_call(tmp_path, monkeypatch) -> None:
    svc, storage = _service(tmp_path, monkeypatch)
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)
    outcome: dict = {}

    def call() -> None:
        try:
            outcome["result"] = svc.ingest_document(str(fifo))
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcome["error"] = exc

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(timeout=10)
    try:
        assert not worker.is_alive(), "document_ingest blocked reading a FIFO"
    finally:
        if worker.is_alive():
            # Unblock the reader so the thread ends with the test.
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            worker.join(timeout=10)
    payload = _error_payload("document_ingest", outcome["error"])
    assert payload["error"] == "not_a_regular_file"
    assert f"{fifo} is a FIFO" in payload["message"]
    assert storage.rows == []


def test_a_file_over_the_cap_is_refused_before_it_is_read(tmp_path, monkeypatch) -> None:
    svc, storage = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(document_parser, "MAX_DOCUMENT_BYTES", 1024)
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * 1025)
    with pytest.raises(ValueError) as info:
        svc.ingest_document(str(big))
    payload = _error_payload("document_ingest", info.value)
    assert payload["error"] == "document_too_large"
    assert f"{big} is 1025 bytes" in payload["message"]
    assert "1024 bytes" in payload["message"]
    assert storage.rows == []


def test_a_regular_file_at_the_cap_is_ingested(tmp_path, monkeypatch) -> None:
    """The near-miss legitimate call: a normal file of exactly the cap."""
    svc, storage = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(document_parser, "MAX_DOCUMENT_BYTES", 1024)
    doc = tmp_path / "manual.md"
    doc.write_bytes(b"word " * 204 + b"word")
    assert doc.stat().st_size == 1024
    result = svc.ingest_document(str(doc))
    assert result["chunks_total"] >= 1
    assert len(storage.rows) == result["chunks_total"]


def test_a_symlink_to_a_regular_file_is_ingested(tmp_path, monkeypatch) -> None:
    svc, storage = _service(tmp_path, monkeypatch)
    target = tmp_path / "real.txt"
    target.write_text("A short document behind a link.", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this host cannot create symlinks")
    result = svc.ingest_document(str(link))
    assert result == {"source": "link.txt", "chunks_stored": 1, "chunks_total": 1}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX")
def test_the_read_itself_refuses_a_fifo_without_blocking(tmp_path) -> None:
    """A FIFO swapped in after the caller's check: the read opens it without
    blocking and refuses it from the open handle."""
    fifo = tmp_path / "late.txt"
    os.mkfifo(fifo)
    with pytest.raises(document_parser.DocumentRefused, match=r" is a FIFO; "):
        document_parser.read_document_bytes(fifo)
    # The PDF branch reads through it too (the libraries get the bytes).
    pdf = tmp_path / "late.pdf"
    os.mkfifo(pdf)
    with pytest.raises(document_parser.DocumentRefused, match=r" is a FIFO; "):
        document_parser.extract_text(pdf)


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs Linux /proc")
def test_a_file_that_reports_no_size_is_cut_at_the_cap(monkeypatch) -> None:
    """``/proc`` entries stat as empty regular files and read on: the read
    stops one byte past the cap and refuses."""
    monkeypatch.setattr(document_parser, "MAX_DOCUMENT_BYTES", 100)
    status = Path("/proc/self/status")
    assert status.stat().st_size == 0
    with pytest.raises(document_parser.DocumentRefused) as info:
        document_parser.extract_text(status)
    assert str(info.value).startswith(
        "document_too_large: /proc/self/status holds more than 100 bytes")
    # pagemap reports no size and holds the whole address space (a plain
    # read ran the Rust daemon out of memory): cut at the cap, as in Rust.
    monkeypatch.setattr(document_parser, "MAX_DOCUMENT_BYTES", 1 << 20)
    with pytest.raises(document_parser.DocumentRefused) as info:
        document_parser.extract_text(Path("/proc/self/pagemap"))
    assert str(info.value).startswith(
        "document_too_large: /proc/self/pagemap holds more than")


def test_the_bounded_read_decodes_as_read_text(tmp_path) -> None:
    doc = tmp_path / "crlf.data"
    raw = b"caf\xc3\xa9\r\nlone\rcr \xff end\r\n"
    doc.write_bytes(raw)
    assert document_parser.extract_text(doc) == doc.read_text(encoding="utf-8",
                                                              errors="replace")


def test_the_cap_is_large_enough_for_a_long_manual() -> None:
    # A 1,000-page technical PDF with figures runs to tens of megabytes.
    assert document_parser.MAX_DOCUMENT_BYTES >= 32 * 1024 * 1024
