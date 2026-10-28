| Name | Python | Rust | Why |
|---|---|---|---|
| MCP-BIND-PRE-PARSE | a JSON-text argument holding an unpaired surrogate escape decodes to a str with a lone surrogate; nesting fails at a stack-dependent depth (RecursionError; the text stays a string) | the text stays a string (a Rust `String` cannot hold a lone surrogate); nesting deeper than 900 levels stays a string | no shipped producer sends either; the depth bound keeps the parser's stack finite |
