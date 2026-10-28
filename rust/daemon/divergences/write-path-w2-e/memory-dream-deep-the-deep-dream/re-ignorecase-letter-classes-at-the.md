| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `re.IGNORECASE` letter classes at the variant-token boundaries (`(?<![A-Za-z0-9])`) | `ı` and `İ` count as letters | they do not (the Kelvin sign and long s match in both) | variant tokens in names with those letters only |
