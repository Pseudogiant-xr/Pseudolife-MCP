#!/usr/bin/env python3
"""Generate llms.txt and llms-full.txt at the repo root.

llms.txt (https://llmstxt.org/) is a curated index of the project's
documentation for AI-agent consumption; llms-full.txt is the full
concatenated text of those pages. Both are committed, and
tests/test_llms_txt.py regenerates them and fails on drift — run this
script after any README or docs/guide change:

    python ops/gen_llms_txt.py          # rewrite both files
    python ops/gen_llms_txt.py --check  # exit 1 if either is stale
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW_BASE = "https://raw.githubusercontent.com/Pseudogiant-xr/Pseudolife-MCP/master"

SUMMARY = (
    "> Persistent long-term memory for Claude Code, Codex, and other MCP "
    "clients: an associative store that ages and supersedes, a slot-keyed "
    "canonical-fact cortex, dream consolidation into facts and a knowledge "
    "graph, procedural lessons, and cited world facts — served by a local "
    "daemon your coding agent calls over MCP."
)

# Curated index: (repo-relative path, one-line description). The test
# enforces that every docs/guide page appears here, so adding a guide
# page without updating this list fails CI rather than shipping a stale
# index.
PAGES: list[tuple[str, str]] = [
    ("README.md",
     "Overview, quickstart, tool reference, and operations — start here"),
    ("docs/guide/memory-model.md",
     "The canonical-fact layers: cortex slots, supersession, contenders, "
     "world facts, lessons, and the temporal/multi-writer stamp"),
    ("docs/guide/retrieval.md",
     "How memory_search ranks: hybrid dense+lexical scoring, "
     "reranking, and the explain trace"),
    ("docs/guide/dreaming.md",
     "The consolidation pass: extractors, the cursor, claim gating, deep "
     "dream, and the review queue"),
    ("docs/guide/episodes.md",
     "Session-scoped episodes: attribution, auto-titles, resume, and "
     "consolidation"),
    ("docs/guide/configuration.md",
     "Every configuration knob, the DSN, schema version history, and "
     "extractor setups"),
    ("docs/guide/providers.md",
     "Per-coding-agent capability matrix, the hook-equivalent ladder, "
     "the AGENTS.md standard, and writer ids"),
    ("docs/guide/re-evidence.md",
     "Build-scoped reverse-engineering evidence, claims, portable archives, "
     "and the optional SRFN workflow"),
    ("docs/guide/benchmarks.md",
     "LongMemEval methodology and results, with the committed evidence "
     "artifacts behind each number"),
    ("docs/guide/comparison.md",
     "Where this sits among agent-memory projects, the axes it is built "
     "around, and when to use something else instead"),
    ("docs/guide/security-posture.md",
     "Memory poisoning (ASI06): the threat model, every shipped "
     "mitigation mapped to it, and what is not defended"),
]


def build_llms_txt() -> str:
    lines = [
        "# Pseudolife-MCP",
        "",
        SUMMARY,
        "",
        "## Documentation",
        "",
    ]
    for path, desc in PAGES:
        title = _page_title(REPO / path)
        lines.append(f"- [{title}]({RAW_BASE}/{path}): {desc}")
    lines += [
        "",
        "## Optional",
        "",
        f"- [CHANGELOG]({RAW_BASE}/CHANGELOG.md): dated, versioned change "
        "history in Keep-a-Changelog format",
        "",
    ]
    return "\n".join(lines)


def build_llms_full() -> str:
    parts = [
        "# Pseudolife-MCP — full documentation",
        "",
        SUMMARY,
        "",
    ]
    for path, _desc in PAGES:
        text = (REPO / path).read_text(encoding="utf-8").strip()
        parts += [f"<!-- source: {path} -->", "", text, "", "---", ""]
    return "\n".join(parts).rstrip() + "\n"


def _page_title(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def main() -> int:
    outputs = {
        REPO / "llms.txt": build_llms_txt(),
        REPO / "llms-full.txt": build_llms_full(),
    }
    if "--check" in sys.argv:
        stale = [
            p.name for p, content in outputs.items()
            if not p.exists() or p.read_text(encoding="utf-8") != content
        ]
        if stale:
            print(f"stale: {', '.join(stale)} — run python ops/gen_llms_txt.py")
            return 1
        print("llms.txt and llms-full.txt are current")
        return 0
    for p, content in outputs.items():
        p.write_text(content, encoding="utf-8", newline="\n")
        print(f"wrote {p.name} ({len(content):,} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
