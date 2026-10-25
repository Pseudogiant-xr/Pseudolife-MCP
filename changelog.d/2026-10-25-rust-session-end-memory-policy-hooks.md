### Added (2026-10-25 — the Rust daemon closes sessions and serves the memory-policy hook)
- On the Rust daemon the plugin's SessionEnd hook answered 501 for a
  session that had one, so a session never closed: its episode stayed open
  until the idle reaper, it kept its generic title, and the client's
  registration row never recorded the end. The separate memory-policy
  SessionStart hook answered 501 too. Both are now served as the Python
  daemon serves them. SessionEnd closes the session's root and every open
  episode below it, deletes it when it holds no entries, otherwise titles a
  generic-named root from its entries, stamps the registration row, clears
  the active-session pointer only while that session owns it, and calls the
  session-end dream trigger, which on the Rust daemon still only logs: the
  dream itself is not ported yet. The memory-policy hook serves the full
  memory-loop block to sessions whose configured variant, or A/B arm, is
  `full_separate_hook`, and nothing on a resume or compaction. The daemon
  now reads `memory_policy.variant` and `memory_policy.ab_arms` from
  `config.yaml`, where before it only validated them.
