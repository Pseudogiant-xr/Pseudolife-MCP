"""The Python daemon's PDFium and the Rust daemon's are one build.

``ops/requirements.lock.txt`` locks the pypdfium2 release whose wheels
bundle the PDFium build ``rust/daemon/pdfium-pin.json`` pins, so both
daemons store byte-identical PDF text. A bump of either alone fails here
(a lock bump) or in the Rust tests (a pin bump); ``rust/daemon/pin_pdfium.py``
writes the pin for a new release and names the pypdfium2 to lock with it.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_lock_and_the_rust_pin_name_one_pdfium_build():
    pin = json.loads((ROOT / "rust" / "daemon" / "pdfium-pin.json").read_text(encoding="utf-8"))
    locked = [
        line.strip().split("==", 1)[1]
        for line in (ROOT / "ops" / "requirements.lock.txt").read_text(encoding="utf-8").splitlines()
        if line.strip().lower().startswith("pypdfium2==")
    ]
    assert locked == [pin["oracle_pypdfium2"]]
    assert pin["tag"].startswith("chromium/")
