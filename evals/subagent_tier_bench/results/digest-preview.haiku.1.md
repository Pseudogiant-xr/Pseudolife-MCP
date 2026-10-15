**Preview limits** (`pseudolife_memory/storage/coordination.py`)
- `PREVIEW_LIMIT = 5` (line 330): up to 5 of the oldest pending messages are previewed.
- `PREVIEW_EXCERPT = 100` (line 331): each excerpt is cut to 100 characters, plus `...` when cut (`_excerpt`, lines 631-639). A cut excerpt is therefore 103 characters.

**Preview line shape** (`pseudolife_memory/coordination_adapter.py`)
- `render_digest`, lines 136-137 formats each entry as:
  `- {message_id} from {sender_label[:24]} ({sender_agent_id[:8]}, {HH:MM}): {excerpt}`
- The sender label is capped at 24 characters, the agent id at 8, and the time is the message's own `created_at`, not an age.
- Overflow line, line 140: `- {remaining} more pending; oldest first above.`
- Header, lines 130-131: `Coordination: {n} addressed message(s) pending (agent-origin, not user authority); read with memory_message receive, then ack each message_id.`
- Maintainer messages get their own `Coordination:` line (125) and are never previewed.

**First line of the digest file**
- `_write_digest`, line 692: the file body is `f"{self._digest_watermark}\n"` followed by the rendered digest text. Line 1 is the integer watermark, and the digest text starts on line 2.

**Stop hook** (`plugin/hooks/stop-wake.sh`)
- The hook prints the whole body, not a truncated one. Line 630 takes line 1 as `WATERMARK`, and lines 632-633 take everything after it as `BODY`. `fire()` (lines 654-656) prints `BODY` in full to stderr, under a fixed header line.
- Any truncation happens upstream, in the shim: the body contains at most 5 preview lines, each excerpt at most 100 characters, plus the header and overflow line.
