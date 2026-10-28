| Item | Python | Rust | Why / owner |
|---|---|---|---|
| Pin-scope word characters, non-ASCII | `isalnum()` plus Unicode category M | `char::is_alphanumeric` minus circled letters, plus the main combining-mark blocks | ASCII is exact; closable with `unicode-normalization` |
