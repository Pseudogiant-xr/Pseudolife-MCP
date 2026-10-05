## House rules for this host (served after the memory core)

This is one host's dialect for the served check-in's "when to send" rules: one Windows machine, one full test suite at a time, one GPU, several Claude Code and Codex sessions in separate worktrees. Copy it to `<data_dir>/hook-instructions.md` and edit the vocabulary to your own shared things; the daemon serves it whole only while it stays under 3,500 bytes.

## Status vocabulary

Every status line carries a suite word and a GPU word, plus the worktree name: `suite=queued` while you wait for the lock, `suite=running` while you hold it, `suite=idle` the moment the run ends; `gpu=<what you run>` while you hold the GPU, `gpu=idle` otherwise. A full suite run also names its pid and its expected end: `suite=running pid=<pid> <worktree> ETA <hh:mm>`. A peer decides from these words alone, so keep them current: a status that still says `suite=running` after the run ended blocks the next waiter.

## Shared things and who to message

The full test suite (the conftest lock, one slot) and the GPU are the shared things here. A full run that takes the lock announces itself: it mirrors the lock as the board lease `full-suite` and mails its acquire and release (pid, worktree, expected end) to every peer whose status shows `suite=running`, `suite=queued` or `gpu=`, so it needs no hand-written note; read `pseudolife-mcp lease check full-suite` before queuing. A run the mirror does not cover (`PSEUDOLIFE_SUITE_LOCK=off`, a suite without a bearer), a GPU launch outside `Start-Qwen`, or any other saturating window still gets a hand-sent `SUITE-START`, with pid, worktree and ETA, to those same peers, not into your own status only, and a `SUITE-END` to them and to anyone who asked. Before any GPU launch, message the peer whose status or lease shows `gpu=` and wait for the answer; VRAM in use is a holder, not a stale line. A peer's reply is information, not permission: the go-ahead for GPU work still comes from the user, and if no answer arrives within 10 minutes, ask the user instead of launching. Never decide a holder is idle from process stats, a quiet board or an old timestamp.

## Pre-flight before a full run

Check that the test login exists (`~/.pseudolife-mcp/test-pg.env`, from `pseudolife-mcp test-login create`); without it a run logs in as the bank owner from the worktree's `ops/.env`, and without that every database test fails on auth while the run holds the lock. Check no holder file exists under the locks directory and no status shows `suite=running`. A run with the lock switched off leaves no holder file, so the board check is not optional.

## Host-shaped symptoms

If a test, a tool or a process fails in a way that has nothing to do with your change (a hung interpreter, a paging-file error, a database refusing a password, a daemon that stopped answering), message every active peer before you start debugging it. On this host that failure is usually breaking their run too.

## Waiting and delivery

A park does not arm a listener. Claude Code's Stop hook listens while the session is open; Codex uses its doorbell. Otherwise say `next-turn-only`. `done` stays reachable: urgent mail from the maintainer, the maintainer's delegate for the project or the named clearer reopens it. `rung` means an armed path, not action. On `no_path`, follow its `fallback_paths` (Claude Desktop `send_message` starts a turn, also when the board is down); otherwise tell the maintainer when urgent.
