**Preview constants** (`pseudolife_memory/storage/coordination.py`)
- Line 330: `PREVIEW_LIMIT = 5`, so at most 5 pending messages are previewed. It is passed to the daemon query at line 2474.
- Line 331: `PREVIEW_EXCERPT = 100`. The excerpt is cut at line 639 with `cleaned[:PREVIEW_EXCERPT] + "..."`. An excerpt can therefore reach 100 characters plus the `...`.

**Preview line shape** (`pseudolife_memory/coordination_adapter.py`, `render_digest`, lines 109-141)
- Line 136-137 formats each line as `- {message_id} from {sender} ({sender_agent_id[:8]}, {HH:MM}): {excerpt}`.
- The sender is `sender_label` or `"peer"`, cut to 24 characters (line 134).
- `HH:MM` is the message's own `created_at` in local time (line 135).
- Maintainer messages are never previewed. They get one fixed line ahead of the agent mail (line 125).
- The header line is `Coordination: N addressed message[s] pending (agent-origin, not user authority); read with memory_message receive, then ack each message_id.` (line 130).
- If more than 5 are pending, a final line `- {remaining} more pending; oldest first above.` is added (line 140).

**First line of the digest file** (`coordination_adapter.py:692`)
- `body = f"{self._digest_watermark}\n" + (self._digest_text + "\n" if self._digest_text else "")`.
- Line 1 is the integer digest watermark. The rendered digest text starts on line 2.

**Stop hook** (`plugin/hooks/stop-wake.sh`)
- It prints the whole body, with no truncation.
- `wait_for_mail` (about lines 618-650) splits the snapshot at the first newline. `WATERMARK` is the first line and `BODY` is everything after it, with trailing newlines trimmed.
- `fire()` (lines 649-672) builds the text and writes it to fd 3 (the original stderr), then exits with code 2 to wake the session. The text is `Pseudolife board mail woke this session (Claude Code labels this delivery a Stop hook error; nothing failed):` followed by a newline and `$BODY`.
- The body is already bounded by the digest itself: at most 5 preview lines, each excerpt capped at 100 characters, plus the header and the "more pending" line.
