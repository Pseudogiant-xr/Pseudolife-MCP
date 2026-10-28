| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `remove_member` refusal (`member_remove_refused`: an assistant, lower-tier or dream retraction, `cortex.py:1121-1128`) | ported | ported (`remove_member`'s `support` and `dream`; `set_members::set_remove` takes them in `SetRemoveArgs`), replayed by the same sequences | unreachable over MCP until the dream (P1) calls it: `memory_set_remove` passes `origin=None`, `dream=False` |
