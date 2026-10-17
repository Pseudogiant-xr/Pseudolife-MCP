"""Write the golden cases for the Rust port of
``memory/graph_consolidation.junk_name_reason`` (``src/junk_names.rs``).

Every expected value comes from calling the real Python function in this
checkout, so the Rust unit test asserts against the oracle, not against a
hand-written expectation. The corpus is hand-written: a positive and a
near-miss for every gate, plus the Unicode edges where Python's ``re`` and
Rust's ``regex`` differ by default (the U+001C..U+001F separators that
``str.isspace`` counts, non-ASCII digits, case-insensitive prefixes).

Output: ``goldens/w2e-junk-names.json``, a list of ``[name, reason-or-null]``.

usage: python gen_w2e_junk_names_goldens.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "w2e-junk-names.json"

CASES = [
    # empty / whitespace
    "", "   ", "\x1c\x1f", "\t\n",
    # concat artifacts
    "memory_recall<->recall.py", "a -> b", "a→b", "a ↔ b", "a <--> b", "->tail", "head->",
    "a<>b", "->", "x <- y",
    # bare numbers and dates
    "42", " 007 ", "٤٢", "12a", "2026-07-02", "2026-07-02 review roadmap", "2026-7-2",
    # stopwords
    "Live", "DONE", "n/a", "None", "null", "ok", "okay", "wip", "TODO", "pending ",
    # count prefix
    "236 memories", "8-band continuum", "3\x1cthings", "12\tfiles",
    # dump files
    "bank.sql", "BANK.SQL.GZ", "bank.sql.bak", "sql",
    # image tags vs arXiv ids
    "postgres:16.4.1", "arXiv:2606.22844", "img:1.2",
    # command strings
    "docker compose up", "docker compose", "Git commit -m", "pytest -q tests", "pip install",
    "npm run build", "curl\x1c-s\x1cx",
    # hash status
    "LOCAL master = 8e2b992", "master=8e2b992ab", "x = 8e2b99", "x = 8E2B992A",
    # action prefix
    "action: deploy", "Action:\tfix", "action:deploy", "reaction: x",
    # status shards
    "P3 SURFACE POLISH", "P12_task", "p3 polish", "P3-polish", "PX thing",
    # metric readings
    "stale 0.8", "score 0.7-0.8", "recall at 0.95", "CUDA Toolkit 13.1", "python 3.12",
    "a b c 0.5", "stale 1", "stale_ratio 0.8-1",
    # list artifacts
    "data/, ops/.env, *.pt", "User (jdoe, a@b)", "a, b", "a,", ",b", "[x, y], z", "a, ,",
    # sentences
    "this is a fairly long status phrase here", "six tokens are not a sentence",
    "seven tokens\x1cmake up a sentence for sure",
    # legitimate names
    "Payments DB", "Go", "uv", "dev box", "ops/backup.ps1", "origin/master",
    "codex-cli / installer", "pg+extractor", "Qwen3-8B", "gpt-5.6-luna", "Straße",
    # review cases: sre IGNORECASE case fixes for i, and Python's word class at \b
    "gıt commit -m x", "pİp install foo", "actıon: x", "İction: x",
    "build = abcdef12́", "x = abcdef1‿z", "build = abcdef1²", "build = abcdef1_",
    "build = abcdef1-", "build = abcdef1é", "build = abcdef1١",
]


def main() -> int:
    sys.path.insert(0, str(REPO))
    from pseudolife_memory.memory.graph_consolidation import junk_name_reason

    out = [[name, junk_name_reason(name)] for name in CASES]
    TARGET.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(out)} cases to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
