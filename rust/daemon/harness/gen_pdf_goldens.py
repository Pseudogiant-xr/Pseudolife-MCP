"""Write ``goldens/pdf_text.json``: the Python daemon's text for every PDF of
``pdf_corpus/``, from ``document_parser.extract_text`` itself under the
pypdfium2 the image locks (whose wheels bundle the PDFium build the Rust
daemon pins), so the Rust test compares against the oracle's own output.

Each entry records the file's sha256 and either its text or the exception
the oracle raised (type and message). Run with the oracle venv (the
pypdfium2 version is checked against ``ops/requirements.lock.txt``):

    python rust/daemon/harness/gen_pdf_goldens.py [--check]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CORPUS = HERE / "pdf_corpus"
OUT = HERE / "goldens" / "pdf_text.json"
sys.path.insert(0, str(ROOT))


def locked_pypdfium2() -> str:
    for line in (ROOT / "ops" / "requirements.lock.txt").read_text(encoding="utf-8").splitlines():
        if line.strip().lower().startswith("pypdfium2=="):
            return line.split("==", 1)[1].strip()
    sys.exit("ops/requirements.lock.txt locks no pypdfium2")


def build() -> dict:
    import pypdfium2.version as v

    from pseudolife_memory.memory.document_parser import extract_text

    # The image's CPython: the recursion error's text (DOC-PDF-RECURSION)
    # differs between minor versions.
    if sys.version_info[:2] != (3, 12):
        sys.exit(f"needs CPython 3.12 (the daemon image's), found {sys.version.split()[0]}")
    if v.PYPDFIUM_INFO.version != locked_pypdfium2():
        sys.exit(f"needs pypdfium2 {locked_pypdfium2()}, found {v.PYPDFIUM_INFO.version}")
    files = []
    for path in sorted(CORPUS.glob("*.pdf")):
        entry = {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        try:
            entry["text"] = extract_text(path)
        except Exception as e:  # noqa: BLE001 - every outcome is recorded
            entry["error"] = type(e).__name__
            entry["message"] = str(e)
        files.append(entry)
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "pypdfium2": v.PYPDFIUM_INFO.version,
        "pdfium": str(v.PDFIUM_INFO.build),
        "files": files,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if the committed goldens differ")
    args = ap.parse_args()
    text = json.dumps(build(), indent=1, ensure_ascii=False) + "\n"
    if args.check:
        if OUT.read_text(encoding="utf-8") != text:
            sys.exit(f"{OUT} is stale; run python {Path(__file__).relative_to(ROOT)}")
        print("pdf goldens current")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
