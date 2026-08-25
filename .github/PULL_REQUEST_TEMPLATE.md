<!--
Open the body with 1-3 plain-language sentences: the problem as a user or
maintainer would notice it, then what this change does about it. The dense
detail goes below. A body that opens with a plan inventory is unreadable to
a human six months later.

Title convention: type(scope): why it matters — not the mechanism.
  good: fix(dream): stop concurrent dreams double-extracting the same window
  bad:  fix(dream): add non-blocking guard to dream_run
-->

## What and why

<!-- The problem first, in one breath. Then the change. -->

## Detail

<!--
Design decisions, what was measured, what was deliberately not shipped,
and any evidence with its date. Link the issue this closes.
-->

## Verification

<!--
How you know it works. Paste the test invocation and its exit code:
  HF_HUB_OFFLINE=1 python -m pytest tests/ > /tmp/pytest-last.log 2>&1; echo $?
Never pipe a test run through a pager — the pipe replaces the suite's exit
code with the pager's.
-->

## Checklist

- [ ] `CHANGELOG.md` entry under `[Unreleased]` (docs-only and test-only
      changes are exempt)
- [ ] Full suite green, with the bench Postgres up (`127.0.0.1:5433`) —
      PG-backed tests skip silently without it, which is not a pass
- [ ] New behaviour has a test that was **watched fail** before the fix
- [ ] Docs updated, and `python ops/gen_llms_txt.py` re-run if any README or
      `docs/guide/` page changed
- [ ] No PII: no emails, OS usernames, hostnames, LAN IPs, tokens or keys —
      this repo is public and merged-PR commits stay reachable forever
- [ ] Schema bump? Every pinned surface updated together (see `CLAUDE.md`)
- [ ] Benchmark number added or changed? Its run artifact is committed in
      the same change, with a row in `tests/test_eval_evidence.py`
