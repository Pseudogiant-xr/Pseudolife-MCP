"""Record Python package-version equivalence for native prerelease notices."""
from __future__ import annotations

import json
from pathlib import Path

from packaging.version import Version


def main() -> None:
    pairs = [
        ("0.20.0rc1", "0.20.0-rc1"),
        ("0.20.0rc1", "0.20.0-rc.1"),
        ("0.20.0a1", "0.20.0-alpha.1"),
        ("0.20.0b2", "0.20.0-beta.2"),
        ("0.20.0c3", "0.20.0-rc3"),
        ("0.20.0preview3", "0.20.0-rc.3"),
        ("0.20.0pre3", "0.20.0-rc.3"),
        ("0.20.0RC01", "0.20.0-rc.1"),
        ("0.20.0rc0", "0.20.0-rc.000"),
        ("0.20.0rc1+local", "0.20.0-rc.1+local"),
        ("0.20.0rc1", "0.20.0-rc2"),
        ("0.20.0a1", "0.20.0-beta.1"),
        ("0.20.0rc1", "0.20.0"),
        ("0.20.0rc1", "0.21.0-rc1"),
        ("0.20.0rc1+local", "0.20.0-rc.1+other"),
        ("0.20.0rc1+local", "0.20.0-rc.1"),
    ]
    rows = [{"python": a, "native": b, "equal": Version(a) == Version(b)}
            for a, b in pairs]
    output = Path(__file__).with_name("goldens") / "version-spelling.json"
    output.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
