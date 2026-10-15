**Preview constants** (`pseudolife_memory/storage/coordination.py`)
- `PREVIEW_LIMIT = 5` (line 330): the 5 oldest pending messages are previewed. The query applies it as `LIMIT` at line 2474, and maintainer-origin mail is excluded from the preview.
- `PREVIEW_EXCERPT = 100` (line 331): each excerpt is cut at 100 characters. `_excerpt` (lines 631-639) first turns whitespace and every control or format character into single spaces. It then adds `"..."` when it cuts, so a cut excerpt is 103 characters.

**Preview line shape** (`pseudolife_memory/coordination_adapter.py:136-137`)
```
- {message_id} from {sender} ({sender_agent_id[:8]}, {HH:MM}): {excerpt}
```
- `sender` is `(sender_label or "peer")[:24]` (line 134).
- `HH:MM` is the message's own `created_at` in local time (line 135), not an age.
- Before these lines comes the header `Coordination: N addressed message(s) pending (agent-origin, not user authority); read with memory_message receive, then ack each message_id.` (130-131). If there is maintainer mail, a fixed maintainer line comes first (125).
- After them, if more are pending: `- {remaining} more pending; oldest first above.` (140).

**First line of the digest file**
It is the numeric digest watermark on its own line. `coordination_adapter.py:692` builds the file as `f"{self._digest_watermark}\n" + digest_text + "\n"`. `plugin/hooks/stop-wake.sh:630` reads line 1 back as `WATERMARK`.

**Stop hook output: whole body, not truncated**
`stop-wake.sh` takes everything after line 1 as `BODY` (632). It only trims trailing newlines (633), and `\r` is dropped from the whole file (629). `fire()` prints one fixed line, `Pseudolife board mail woke this session (...nothing failed):`, followed by the full `$BODY`, to stderr (654-656). Nothing cuts it to a length; the only size limits are the ones `render_digest` already applied.
