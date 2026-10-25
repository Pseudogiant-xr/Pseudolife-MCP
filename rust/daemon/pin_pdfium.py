"""Write ``pdfium-pin.json``: the PDFium build the Rust daemon provisions.

Contributor tool for a pin bump, not run by install or update. For one
``bblanchon/pdfium-binaries`` release tag it downloads each target's
archive, checks it against the digest GitHub records for the asset,
verifies the release's build attestation with ``gh attestation verify``,
and records the archive's sha256 and the sha256 of the library inside it.
The daemon embeds the file at compile time (``src/provision/pdfium.rs``):
``pseudolife-daemon provision pdfium`` installs only an archive and a
library that match it, and PDF ingest loads only a library that matches.

Bump the pin together with the oracle's ``pypdfium2``, whose wheels bundle
the same builds: the PDF differential compares the two byte for byte.

    python rust/daemon/pin_pdfium.py chromium/8076 --pypdfium2 5.14.0
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = "bblanchon/pdfium-binaries"
OUT = Path(__file__).with_name("pdfium-pin.json")

# Daemon target (as `src/provision/pdfium.rs` names it) -> release asset and
# the library's path inside it.
TARGETS = {
    "windows-x86_64": ("pdfium-win-x64.tgz", "bin/pdfium.dll"),
    "windows-aarch64": ("pdfium-win-arm64.tgz", "bin/pdfium.dll"),
    "linux-x86_64-gnu": ("pdfium-linux-x64.tgz", "lib/libpdfium.so"),
    "linux-x86_64-musl": ("pdfium-linux-musl-x64.tgz", "lib/libpdfium.so"),
    "linux-aarch64-gnu": ("pdfium-linux-arm64.tgz", "lib/libpdfium.so"),
    "macos-aarch64": ("pdfium-mac-arm64.tgz", "lib/libpdfium.dylib"),
    "macos-x86_64": ("pdfium-mac-x64.tgz", "lib/libpdfium.dylib"),
}


def gh_json(*args: str):
    return json.loads(subprocess.run(["gh", *args], check=True, capture_output=True).stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tag", help="release tag, e.g. chromium/8076")
    ap.add_argument("--pypdfium2", required=True,
                    help="the pypdfium2 release that bundles this build (the PDF oracle)")
    args = ap.parse_args()

    release = gh_json("api", f"repos/{REPO}/releases/tags/{args.tag}")
    assets = {a["name"]: a for a in release["assets"]}
    build = args.tag.split("/")[-1]
    targets = {}
    signer = None
    with tempfile.TemporaryDirectory() as tmp:
        for target, (asset, library) in TARGETS.items():
            meta = assets[asset]
            url = meta["browser_download_url"]
            data = urllib.request.urlopen(url, timeout=300).read()
            digest = hashlib.sha256(data).hexdigest()
            if meta.get("digest") != f"sha256:{digest}":
                sys.exit(f"{asset}: downloaded sha256 {digest} is not GitHub's {meta.get('digest')}")
            path = Path(tmp, asset)
            path.write_bytes(data)
            proof = gh_json("attestation", "verify", str(path), "--repo", REPO, "--format", "json")
            cert = proof[0]["verificationResult"]["signature"]["certificate"]
            signer = signer or cert["buildSignerURI"]
            if cert["buildSignerURI"] != signer:
                sys.exit(f"{asset}: signed by {cert['buildSignerURI']}, not {signer}")
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                lib = tar.extractfile(library)
                if lib is None:
                    sys.exit(f"{asset}: no {library}")
                lib_digest = hashlib.sha256(lib.read()).hexdigest()
            targets[target] = {
                "asset": asset,
                "url": url,
                "archive_sha256": digest,
                "archive_bytes": len(data),
                "library": library,
                "library_sha256": lib_digest,
            }
            print(f"{target}: {asset} {digest[:12]}, {library} {lib_digest[:12]}")

    pin = {
        "source": f"https://github.com/{REPO}",
        "tag": args.tag,
        "dir": f"chromium-{build}",
        "oracle_pypdfium2": args.pypdfium2,
        "attestation": {
            "signer": signer,
            "verified": datetime.date.today().isoformat(),
        },
        "targets": targets,
    }
    OUT.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
