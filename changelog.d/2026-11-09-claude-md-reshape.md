### Changed (2026-11-09 — agent conventions reshaped: fewer, truer rules, same guards)

- `CLAUDE.md` is rewritten rule by rule (5,448 to 4,439 words) after a
  four-round, four-model council review: two rules with false premises are
  gone (the merge-queue paragraph; the Windows rerun clause that cited the
  closed issue #830), procedures move behind triggered pointers
  (`docs/dev/install-ux.md`, `docs/dev/glossary.md`, the new
  `derived-state` skill, a "Sharing" section in `docs/dev/dev-machines.md`),
  and `tests/test_claude_md_citations.py` now fails when any tracked file
  cites a `CLAUDE.md` heading that no longer exists.
- Agent behaviour that changes: a Windows-only hosted CI failure gets one
  rerun on the current merge ref naming the test and its flake issue, and a
  second or deterministic failure goes to the Windows dev VM (the
  `ops/integrate.py` merge refusal says the same); a batch is built when two
  or more PRs are ready, or a lone PR has waited about an hour or is urgent
  or a release cut; the Codex second review skips docs-only and test-only
  diffs; a friction fix may be an issue with the repro when the fix is not
  small, and a reviewer or subagent reports it instead; reviewers report
  stale memory and the owning session corrects it; the maintainer's
  standing merge delegation may be written in their own instructions.
- The PR template renders the plain-language lead first and asks for two
  fixed Verification lines (local full suite; reviews).
