"""Record PyYAML's scalar values for the native startup config loader."""
from __future__ import annotations

import json
from pathlib import Path

import yaml


def cases() -> list[dict]:
    spellings = [
        "yes", "Yes", "YES", "no", "No", "NO", "on", "On", "ON", "off", "Off", "OFF",
        "true", "True", "TRUE", "false", "False", "FALSE",
        "y", "Y", "n", "N", "yEs", "nO", "oN", "oFF", "tRuE", "fAlSe",
        "010", "-010", "+010", "1_000", "0x10", "0b11", "1:30", "1:30.5",
        "0o10", "08", "1e5", "1e-1", "1.0e-1", "null", "Null", "NULL", "~", "",
        "!!bool yEs", "!!null Null", "!!null ignored", "!!int '010'", "!!float '1e5'",
        "! yes", "! 'yes'", "!!str yes", "'yes'", '"off"', "'010'", "'NULL'",
        "9223372036854775807", "-9223372036854775808",
    ]
    return [{"yaml": text, "expected": yaml.safe_load(text)} for text in spellings]


def main() -> None:
    output = Path(__file__).with_name("goldens") / "startup-yaml11.json"
    output.write_text(json.dumps({"pyyaml": yaml.__version__, "cases": cases()},
                                 indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                      encoding="utf-8")


if __name__ == "__main__":
    main()
