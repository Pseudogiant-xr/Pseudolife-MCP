| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A failed lesson batch's `error` text for a database failure | psycopg's message | tokio-postgres' message | failure path only; extractor failures and the acknowledgement mismatch carry Python's text |
