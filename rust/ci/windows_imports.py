"""Check the normal and delay-load imports of the Windows x64 client."""

import argparse
from pathlib import Path
import struct


# Windows system libraries used by the client. Additions need to ship with
# Windows itself on every supported version. Keep the list explicit so a new
# redistributable dependency fails the build even on a provisioned runner.
SYSTEM_DLLS = frozenset({
    "advapi32.dll",
    "api-ms-win-core-synch-l1-2-0.dll",
    "bcrypt.dll",
    "bcryptprimitives.dll",
    "combase.dll",
    "crypt32.dll",
    "dbghelp.dll",
    "iphlpapi.dll",
    "kernel32.dll",
    "ncrypt.dll",
    "ntdll.dll",
    "ole32.dll",
    "oleaut32.dll",
    "secur32.dll",
    "shell32.dll",
    "user32.dll",
    "userenv.dll",
    "ws2_32.dll",
})


def imported_dlls(data: bytes) -> set[str]:
    """Read DLL names from an AMD64 PE32+ image (Microsoft PE/COFF format)."""
    try:
        pe, = struct.unpack_from("<I", data, 0x3C)
        machine, count = struct.unpack_from("<HH", data, pe + 4)
        optional_size, = struct.unpack_from("<H", data, pe + 20)
        optional = pe + 24
        magic, = struct.unpack_from("<H", data, optional)
        if data[:2] != b"MZ" or data[pe:pe + 4] != b"PE\0\0" or (machine, magic) != (0x8664, 0x20B):
            raise ValueError("expected a Windows x64 PE32+ executable")
        directories, = struct.unpack_from("<I", data, optional + 108)
        if optional_size < 112 or directories > (optional_size - 112) // 8:
            raise ValueError("truncated PE data directories")
        sections = [
            struct.unpack_from("<III", data, optional + optional_size + index * 40 + 12)
            for index in range(count)
        ]

        def bounds(rva: int) -> tuple[int, int]:
            for virtual, size, raw in sections:
                if virtual <= rva < virtual + size and raw + size <= len(data):
                    return raw + rva - virtual, raw + size
            raise ValueError(f"PE import RVA {rva:#x} is outside the file-backed sections")

        dlls = set()
        # Directory index, descriptor width, and the index of its name RVA.
        for index, words, name_index in ((1, 5, 3), (13, 8, 1)):
            if index >= directories:
                continue
            rva, size = struct.unpack_from("<II", data, optional + 112 + index * 8)
            if (rva, size) == (0, 0):
                continue
            start, end = bounds(rva)
            if not rva or start + size > end:
                raise ValueError("PE import directory exceeds its section")
            for offset in range(start, start + size - words * 4 + 1, words * 4):
                descriptor = struct.unpack_from(f"<{words}I", data, offset)
                if not any(descriptor):
                    break
                if index == 13 and descriptor[0] != 1:
                    raise ValueError("expected RVA-based PE delay imports")
                name, end = bounds(descriptor[name_index])
                terminator = data.find(b"\0", name, end)
                if terminator <= name:
                    raise ValueError("missing or unterminated PE import name")
                dlls.add(data[name:terminator].decode("ascii").lower())
            else:
                raise ValueError("unterminated PE import directory")
    except (struct.error, UnicodeDecodeError) as error:
        raise ValueError("malformed PE import table") from error
    if not dlls:
        raise ValueError("Windows client has no PE imports")
    return dlls


def check_windows_imports(data: bytes) -> None:
    dlls = imported_dlls(data)
    unexpected = dlls - SYSTEM_DLLS
    if unexpected:
        raise ValueError("Windows client imports DLLs outside the system allowlist: " + ", ".join(sorted(unexpected)))
    print("Windows client imports only allowlisted system DLLs: " + ", ".join(sorted(dlls)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    try:
        check_windows_imports(args.binary.read_bytes())
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
