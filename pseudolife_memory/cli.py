"""Console-script dispatch — deliberately torch-free at import time.

* ``pseudolife-mcp``           — stdio shim: find/start the daemon, proxy.
* ``pseudolife-mcp serve``     — the HTTP memory daemon (deployment mode).
* ``pseudolife-mcp embedded``  — v0.1 in-process stdio server (escape hatch).
* ``pseudolife-mcp briefing``  — print the session-start briefing (for a hook).
* ``pseudolife-mcp prompt-hook`` — the per-turn memory-change note (for a hook).

The heavy imports (torch, sentence-transformers) happen only inside the
``serve`` / ``embedded`` branches; the shim path imports nothing heavier
than the mcp client SDK, so per-session startup stays ~instant.
"""

from __future__ import annotations

import sys

_USAGE = """\
pseudolife-mcp — persistent long-term memory for coding agents over MCP

usage: pseudolife-mcp [mode]

modes:
  (no arg)       stdio shim: find/start the daemon and proxy to it
  channel        experimental Claude channel transport (requires explicit opt-in)
  coordination-recovery  offline mailbox recovery after a database restore
  board-audit    export, verify, redact or stats on the agent board's audit log
                 (operator-only; reads PSEUDOLIFE_MCP_DATABASE_URL)
  serve          run the HTTP memory daemon (deployment mode)
  embedded       in-process stdio server — no daemon, no Postgres (escape hatch)
  briefing       print the session-start briefing (for a SessionStart hook;
                 --hook-json emits the memory core + bounded briefing as
                 Claude Code/Codex hook JSON; --coordination prints the
                 agent-board check-in, only where the board works)
  prompt-hook    per-turn UserPromptSubmit hook: prints a note only when new
                 lessons or other sessions' status notes landed since this
                 session's last one (installs without the plugin)
  doctor         check runtime, health, MCP and coordination (read-only by default;
                 --disposable-proof writes only to an explicit fixture server)
  tunnel         guided optional Secure MCP Tunnel setup, cloud verification,
                 status/doctor, private key renewal and opted-in autostart
                 (`tunnel setup` resumes saved steps; `tunnel --help`)
  connect        point this machine's client registrations at a daemon URL
                 (`connect <url>`): replaces the old URL and, with
                 --token-file, the token file; verifies the daemon accepts
                 them first; all-or-nothing with backups (--dry-run shows
                 the plan; --help for options)
  invite         on the daemon host: give another machine its own principal
                 (`invite <machine>`): prints a short-lived, single-use
                 pairing code, no daemon restart; --list, --revoke NAME
  pair           join a bank with a pairing code from `invite` on the daemon
                 host (`pair <url> <code>`, or --read-code): mints the token
                 here, writes it owner-only and sends the daemon only its
                 SHA-256; never prints the token or the code
  expose         on the daemon host: put the daemon on the tailnet with
                 Tailscale Serve (`expose tailscale`), refusing a daemon
                 without a token and never replacing another serve;
                 `expose off` removes only that forward, `expose status`
                 prints the client URL
  move           move this host's Docker-tier bank to another Docker-tier
                 checkout host over key-based ssh (`move --to <ssh-target>`):
                 final backup from the stopped daemon, database fence,
                 restore and row-count check on the target, one start there,
                 then this machine's clients re-pointed; the source is
                 stopped and fenced, never deleted, and a failure rolls back
                 (--dry-run shows the plan; --help for options)
  update         update the daemon and the client side from a release, with
                 no checkout: pull the pinned GHCR image, back the bank up,
                 tag a rollback, recreate only the daemon, wait for health,
                 then the shim runtime, the plugin cache and the Codex step
                 (`--check` only reports whether a newer release exists;
                 `--tag X` pins one; pip installs upgrade the package)
  backup         back up the bank: pg_dump + state archive with rotation
                 (pip tiers; the Docker tier keeps ops/backup.ps1)
  export         write a portable logical export of the bank (ZIP of JSONL
                 tables + manifest; tier- and PG-version-independent)
  import         load a logical export into a fresh, empty bank
  episode-start  open a session episode (legacy hook helper)
  episode-end    close it
  wait-mail      block until the daemon rings this session for agent mail
                 (plain mail never wakes), print the mail and exit (arm as a
                 background command to wake an idle session; exit 0 ring,
                 3 timeout, 2 setup; --help for options)
  lease          hold a named lease around a command: `lease run NAME --
                 COMMAND...` takes an OS file lock the agent board mirrors
                 (FIFO queue, holder, expected end); `lease hold NAME
                 --while-pid PID` holds one for a process already running;
                 `lease check NAME` exits 1 while it is held (a launch gate);
                 `lease list` shows them; `lease break NAME` (operator)
                 frees a stuck one
  version       print the package version, and the shim runtime it runs
                from with that runtime's source commit (also --version)
  help          show this message (also -h / --help)

credentials (token-gated daemon): PSEUDOLIFE_MCP_TOKEN=<bearer>, or
  PSEUDOLIFE_MCP_TOKEN_FILE=<owner-only file holding it> for hosts that
  sanitize the launch environment (Claude Desktop); the file wins when both
  are set. ops/register_claude_desktop.py probes this text for the file form.

docs: https://github.com/Pseudogiant-xr/Pseudolife-MCP
"""


def _print_version() -> None:
    """``pseudolife-mcp <version>``, then the runtime this interpreter runs
    from when it is one of the side-by-side shim runtimes."""
    from pseudolife_memory import __version__, runtimes

    print(f"pseudolife-mcp {__version__}")
    try:
        runtime = runtimes.running_runtime(runtimes.default_layout())
    except ValueError:      # a half-set layout override: no runtime to name
        runtime = None
    if runtime is not None:
        origin = (f"source commit {runtime.source_commit}" if runtime.source_commit
                  else f"source {runtime.source or 'unknown'}")
        print(f"runtime {runtime.path} ({origin})")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "shim"
    if mode in ("help", "-h", "--help"):
        print(_USAGE, end="")
        sys.exit(0)
    if mode in ("version", "--version"):
        _print_version()
        sys.exit(0)
    if mode == "serve":
        from pseudolife_memory.daemon import run_daemon
        run_daemon()
    elif mode == "embedded":
        from pseudolife_memory.mcp_server import main as embedded_main
        embedded_main()
    elif mode == "shim":
        from pseudolife_memory.shim import run_shim
        run_shim()
    elif mode == "channel":
        from pseudolife_memory.shim import run_shim
        run_shim(channel=True)
    elif mode == "coordination-recovery":
        from pseudolife_memory.coordination_recovery import main as recover_main
        sys.exit(recover_main(sys.argv[2:]))
    elif mode == "board-audit":
        from pseudolife_memory.board_audit_cli import main as audit_main
        sys.exit(audit_main(sys.argv[2:]))
    elif mode == "briefing":
        from pseudolife_memory.briefing_cli import run_briefing
        run_briefing()
    elif mode == "prompt-hook":
        from pseudolife_memory.briefing_cli import run_prompt_hook
        run_prompt_hook()
    elif mode == "doorbell-prompt-seen":
        from pseudolife_memory.codex_doorbell_state import prompt_seen_hook
        prompt_seen_hook()
    elif mode == "doctor":
        from pseudolife_memory.doctor_cli import run_doctor
        run_doctor()
    elif mode == "connect":
        from pseudolife_memory.connect_cli import main as connect_main
        sys.exit(connect_main(sys.argv[2:]))
    elif mode == "invite":
        from pseudolife_memory.invite_cli import main as invite_main
        sys.exit(invite_main(sys.argv[2:]))
    elif mode == "pair":
        from pseudolife_memory.pair_cli import main as pair_main
        sys.exit(pair_main(sys.argv[2:]))
    elif mode == "expose":
        from pseudolife_memory.expose_cli import main as expose_main
        sys.exit(expose_main(sys.argv[2:]))
    elif mode == "tunnel":
        from pseudolife_memory.tunnel_cli import main as tunnel_main
        sys.exit(tunnel_main(sys.argv[2:]))
    elif mode == "move":
        from pseudolife_memory.move_cli import main as move_main
        sys.exit(move_main(sys.argv[2:]))
    elif mode == "update":
        from pseudolife_memory.update_cli import main as update_main
        sys.exit(update_main(sys.argv[2:]))
    elif mode == "backup":
        from pseudolife_memory.backup_cli import run_backup
        run_backup()
    elif mode in ("export", "import"):
        from pseudolife_memory import transfer_cli
        transfer_cli.run_transfer(mode)
    elif mode in ("episode-start", "episode-end"):
        from pseudolife_memory.episode_cli import run_episode
        run_episode(mode)
    elif mode == "wait-mail":
        from pseudolife_memory.wait_mail_cli import run_wait_mail
        sys.exit(run_wait_mail(sys.argv[2:]))
    elif mode == "lease":
        from pseudolife_memory.lease_cli import main as lease_main
        sys.exit(lease_main(sys.argv[2:]))
    else:
        print(
            f"unknown mode {mode!r}; see: pseudolife-mcp --help",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
