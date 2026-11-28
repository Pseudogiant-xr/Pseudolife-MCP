### Fixed (2026-11-28 — a first install that approves the Codex hooks no longer reports them broken)
- On a first install, the installers set up the Codex hooks before they register the pseudolife-memory server with Codex. The hook check then read the daemon with no token, so a daemon that needs one (every client-only or paired install, and a local install with a token) refused it.
- The native installer reported Codex setup incomplete ("The memory daemon did not list its episodes; check daemon access") and exited 5. The Python client's installer printed "Hook setup failed (HTTPError)". In both cases the hooks were installed and working.
- This hit installs that approved the hooks, interactively or with `--codex-hook-trust yes`, on a Codex with no earlier Pseudolife registration.
- The check now reads the daemon with the hooks' own credential, the token and URL in Codex's managed connection. It refuses an exported `PSEUDOLIFE_MCP_DAEMON_URL` that names another daemon before any request, as the hooks do.
- Rerunning an affected install already passed, and still does.
