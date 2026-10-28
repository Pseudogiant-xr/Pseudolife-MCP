| Item | Python | Rust | Owner / why |
|---|---|---|---|
| The date mask, `isdigit` and NFC/NFKC in the gates | Python's `re` and `unicodedata` (Unicode 14) | the regex crate's Unicode `\b`/`\d`/`\s` and case folding, an approximated `isdigit`, the normalization crate's tables | rare characters only; `\w` and category M use CPython's exact tables |
