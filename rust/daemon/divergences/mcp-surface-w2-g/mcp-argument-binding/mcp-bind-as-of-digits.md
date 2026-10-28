| Name | Python | Rust | Why |
|---|---|---|---|
| MCP-BIND-AS-OF-DIGITS | `float()` reads non-ASCII decimal digits in an `as_of` string | refused unless `fromisoformat` accepts it | `as_of` producers send ASCII epoch seconds or ISO text; closable with a decimal-digit table |
