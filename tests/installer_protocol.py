"""The installer-protocol level the native installers require of a staged release.

ops/install-native.sh names it (NATIVE_INSTALLER_PROTOCOL); ops/install-native.ps1
and the binary (rust/shim/src/cli/installer/mod.rs, INSTALLER_PROTOCOL) must
agree (tests/test_installer_native_client.py checks it).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _level(path: str, pattern: str) -> int:
    text = (ROOT / path).read_text(encoding="utf-8")
    [value] = re.findall(pattern, text, flags=re.MULTILINE)
    return int(value)


LEVEL = _level("ops/install-native.sh", r"^NATIVE_INSTALLER_PROTOCOL=([0-9]+)$")
PS_LEVEL = _level("ops/install-native.ps1", r"^\$NativeInstallerProtocol = ([0-9]+)$")
RUST_LEVEL = _level("rust/shim/src/cli/installer/mod.rs", r"^pub const INSTALLER_PROTOCOL: u32 = ([0-9]+);$")
ANSWER = f"pseudolife-mcp installer-protocol {LEVEL}"
