"""Synthetic PE32+ images exercise the import guard without a Windows build."""

import re
import struct

import pytest

from windows_imports import check_windows_imports, imported_dlls


def pe_image(normal=b"KERNEL32.dll", delayed=b"Ws2_32.DLL", *, include_imports=True):
    data = bytearray(0x400)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    optional, optional_size = 0x98, 240
    struct.pack_into("<HH", data, 0x84, 0x8664, 1)
    struct.pack_into("<H", data, 0x94, optional_size)
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<I", data, optional + 108, 16)
    # One section mapping RVA 0x1000 to file offset 0x200.
    struct.pack_into("<III", data, optional + optional_size + 12, 0x1000, 0x200, 0x200)
    if not include_imports:
        return bytes(data)
    for index, rva, size in ((1, 0x1000, 40), (13, 0x1040, 64)):
        struct.pack_into("<II", data, optional + 112 + index * 8, rva, size)
    struct.pack_into("<I", data, 0x20C, 0x1100)  # normal descriptor's name
    struct.pack_into("<II", data, 0x240, 1, 0x1140)  # RVA-based delay descriptor
    data[0x300:0x300 + len(normal)] = normal
    data[0x340:0x340 + len(delayed)] = delayed
    return bytes(data)


def test_checks_both_import_tables_case_insensitively():
    binary = pe_image()
    assert imported_dlls(binary) == {"kernel32.dll", "ws2_32.dll"}
    check_windows_imports(binary)


@pytest.mark.parametrize(("normal", "delayed", "unexpected"), [
    (b"VCRUNTIME140.dll", b"ws2_32.dll", "vcruntime140.dll"),
    (b"kernel32.dll", b"MSVCP140.dll", "msvcp140.dll"),
    (b"kernel32.dll", b"onnxruntime.dll", "onnxruntime.dll"),
    (b"api-ms-win-crt-heap-l1-1-0.dll", b"ws2_32.dll", "api-ms-win-crt-heap-l1-1-0.dll"),
])
def test_rejects_non_system_dlls_in_either_table(normal, delayed, unexpected):
    with pytest.raises(ValueError, match=f"outside the system allowlist: {unexpected}"):
        check_windows_imports(pe_image(normal, delayed))


@pytest.mark.parametrize(("offset", "value", "error"), [
    (0x20C, 0x1200, "outside the file-backed sections"),
    (0x114, 20, "unterminated PE import directory"),
    (0x240, 0, "expected RVA-based PE delay imports"),
])
def test_malformed_imports_fail_closed(offset, value, error):
    data = bytearray(pe_image())
    struct.pack_into("<I", data, offset, value)
    with pytest.raises(ValueError, match=error):
        check_windows_imports(bytes(data))


@pytest.mark.parametrize(("image", "error"), [
    pytest.param(b"", "malformed PE import table", id="empty-file"),
    pytest.param(bytes(0x400), "expected a Windows x64 PE32+", id="non-pe-file"),
    pytest.param(pe_image(include_imports=False), "Windows client has no PE imports", id="both-import-directories-empty"),
])
def test_missing_import_data_fails_closed(image, error):
    with pytest.raises(ValueError, match=re.escape(error)):
        check_windows_imports(image)
