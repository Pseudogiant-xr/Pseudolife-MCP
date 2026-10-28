| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A reply's served model holding a lone surrogate | PostgreSQL refuses the response identity's JSON (`invalid input syntax for type json`, with its DETAIL and CONTEXT) | the same statement fails on the same server, its text laid out as psycopg's (message, DETAIL, CONTEXT) without psycopg's closing parameter line | log-only, as the tick's `error` text for a database failure (above) |
