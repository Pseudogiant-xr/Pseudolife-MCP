"""Regenerate the transfer row's input archives,
``fixtures/transfer-archives/{current,tricky,ties}.zip``, from this checkout.

The archives were first written by the frozen Python export at 6ca817d70.
This tool rebuilds them without the package: it seeds the row's source banks
with the row's own seeders (``rows/transfer.py`` ``_seed``, which runs
``rows/_transfer_seed._seed_bank``, ``_seed_tricky`` and ``_seed_ties``) on
disposable banks under the test login, exports each with the native client
and pins the manifest's ``created_at`` as the row does. Run it after a schema
bump, then re-record the transfer goldens:

    python rust/cli_harness/regenerate_transfer_archives.py --candidate <pseudolife-stdio>

``--check`` writes nothing: it compares what it would write with the committed
archives, member by member (JSONL rows as JSON; the manifest without
``created_at`` and ``pseudolife_version``), and exits 1 on any difference.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rust.cli_harness.rows import transfer  # noqa: E402

# The manifest fields that name the writer or its clock, not the bank.
WRITER_FIELDS = ("created_at", "pseudolife_version")


def _members(blob: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        members = {}
        for name in zf.namelist():
            data = zf.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                for field in WRITER_FIELDS:
                    manifest.pop(field, None)
                members[name] = manifest
            elif name.endswith(".jsonl"):
                members[name] = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
            else:
                members[name] = data
    return members


def differences(new: bytes, old: bytes) -> list[str]:
    """The members whose content differs, or that only one archive has."""
    a, b = _members(new), _members(old)
    found = [f"only in the new archive: {name}" for name in sorted(set(a) - set(b))]
    found += [f"only in the committed archive: {name}" for name in sorted(set(b) - set(a))]
    found += [f"differs: {name}" for name in sorted(set(a) & set(b)) if a[name] != b[name]]
    return found


def build(candidate: Path) -> dict[str, bytes]:
    """Seed the three source banks and export each with the native client."""
    transfer._bank.NAMES.require_process()
    sources = {"current": (transfer.SRC, transfer._seed), "tricky": (transfer.TRICKY, transfer._seed_tricky),
               "ties": (transfer.TIES, transfer._seed_ties)}
    archives = {}
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        for key, (bank, seed) in sources.items():
            transfer._create(bank)
            seed(bank)
            transfer._wait_idle(bank)
            out = Path(tmp) / f"{key}.zip"
            env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), **transfer._dsn(bank)}
            # The row's export cases run the candidate with ["export", ...].
            subprocess.run([str(candidate), "export", "--out", str(out)], env=env, check=True,
                           stdin=subprocess.DEVNULL, timeout=300)
            archives[key] = transfer._rewrite(out.read_bytes(), transfer._pin_created_at)
    return archives


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--candidate", required=True, type=Path, help="the native pseudolife-stdio to export with")
    parser.add_argument("--check", action="store_true", help="compare with the committed archives; write nothing")
    args = parser.parse_args(argv)
    archives = build(args.candidate.resolve())
    status = 0
    for key, blob in archives.items():
        path = transfer.ARCHIVE_DIR / f"{key}.zip"
        if args.check:
            found = differences(blob, path.read_bytes())
            print(f"{key}: {'same content' if not found else '; '.join(found)}")
            status |= bool(found)
        else:
            path.write_bytes(blob)
            print(f"wrote {path.relative_to(ROOT)}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
