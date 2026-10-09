"""Write (or check) the Rust daemon's copy of the Python schema DDL.

``pseudolife_memory.storage.schema.SCHEMA_SQL`` (base, coordination,
principals and maintainer DDL, concatenated at import) is the one DDL text
``ensure_schema`` runs as a single statement batch. The Rust daemon embeds a
byte-exact copy, ``rust/daemon/src/storage/schema.sql``, so the two cannot
drift silently: ``--check`` exits 1 when the copy differs from the Python
constant, and the harness's database-state diff proves what both create.

usage: python gen_schema_sql.py [--check]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = REPO / "rust" / "daemon" / "src" / "storage" / "schema.sql"


def main() -> int:
    sys.path.insert(0, str(REPO))
    from pseudolife_memory.storage import schema

    text = schema.SCHEMA_SQL
    header = f"-- schema_version {schema.SCHEMA_META_VERSION}\n"
    want = (header + text).encode("utf-8")
    if "--check" in sys.argv:
        have = TARGET.read_bytes() if TARGET.exists() else b""
        if have != want:
            print(f"{TARGET} differs from pseudolife_memory.storage.schema.SCHEMA_SQL; "
                  "rerun gen_schema_sql.py", file=sys.stderr)
            return 1
        print("schema.sql matches SCHEMA_SQL")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(want)
    print(f"wrote {TARGET} ({len(want)} bytes, schema {schema.SCHEMA_META_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
