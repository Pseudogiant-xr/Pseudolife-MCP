### Fixed (2026-10-30 — a session on a machine with a dead token now says it has no memory)
- A machine whose bearer token had been revoked or rotated away started
  every session looking normal: the SessionStart hook got the public
  instructions with no briefing and nothing saying why, so two machines ran
  11 days without memory. When the caller presents a bearer the daemon
  rejects, the session-start answer now opens with one line naming the
  cause and the fix (`pseudolife-mcp invite <name>` on the daemon host,
  then `pseudolife-mcp pair <daemon-url> --read-code` here; `doctor` shows
  the state). A caller with no bearer, a daemon with authentication off,
  and a bearer the daemon cannot check while its principal store is
  unavailable get the same answer as before. The Rust daemon answers
  byte for byte the same. The line reaches the agent's context, which is
  where the plugin hooks put SessionStart output; they have no channel that
  shows text to the person directly.
