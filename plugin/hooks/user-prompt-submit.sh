#!/usr/bin/env bash
# Pseudolife-MCP UserPromptSubmit hook — stdout becomes turn context.
# Static by design: this fires on EVERY user turn, so no daemon round-trip
# and no network dependency. The one-shot SessionStart briefing loses
# salience over a long session (2026-08-25 finding); a per-turn line keeps
# the memory loop mechanical, and codifies recall-before-review
# (2026-08-28): search the bank first, then compare it against the files.
# Must never block a turn (always exits 0).

echo "Memory (PseudoLife) mid-session discipline: before reviewing code, docs, or a PR -> memory_search + memory_lesson_search the target area FIRST, then compare memory against the files and correct drift both ways (fix stale memory via memory_fact_set + memory_outcome; treat memory-vs-file mismatches as review findings). Status or in-progress questions -> memory_search (include sources: status) before or alongside git. Starting work in a new area -> memory_search + memory_lesson_search first. Launching or finishing long-running work -> memory_store a status entry. Outcome landed -> memory_outcome with used_ids."

exit 0
