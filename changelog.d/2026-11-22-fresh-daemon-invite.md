### Fixed (2026-11-22 — Invite the first machine on a fresh daemon) [#1864]
- The Rust daemon gives a writable bank its identity at startup. Once storage is ready, `pseudolife-mcp invite` works before any agent connects. If the daemon has no usable bank identity, `invite` explains what to do next.
- Native `pseudolife-mcp move` accepts a fresh target with a bank identity audit event and still refuses a target with other audit activity. Finish, roll back or release a paused move before updating the target's native client, because older clients include the event in the saved row count.
