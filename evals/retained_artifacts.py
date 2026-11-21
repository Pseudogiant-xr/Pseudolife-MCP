"""Immutable checkpoints for interrupted or repeated Rust quality runs."""
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from evals.retained_daemon import BenchError


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()


def fingerprint(value):
    return sha256(json_bytes(value)).hexdigest()


def read_json(path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        raise BenchError(f"missing or invalid artifact {path.name}") from None


def record(path: Path, value):
    """Publish complete bytes once, accepting a repeat only if it is identical."""
    data = json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as handle:
            staging = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(staging, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise BenchError(f"{path.name} already records different inputs or results; use a new run")
    finally:
        if staging is not None:
            staging.unlink()


def rows_in(root, manifest):
    """Read a complete set in manifest order and catch swapped or stray rows."""
    ids = manifest["question_ids"]
    rows = [read_json(root / f"{index:04d}.json") for index in range(len(ids))]
    if [row["question_id"] for row in rows] != ids:
        raise BenchError("result rows do not match the question manifest")
    return rows
