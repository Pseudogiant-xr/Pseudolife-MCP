| Input/output | Producer | Declared native substitution |
|---|---|---|
| `wait-mail-ascii-columns-help`: Terminal dimensions and help | Terminals/callers supply COLUMNS; the help drift guard runs at COLUMNS80. | Positive ASCII integers; invalid grammar, zero or machine-integer overflow falls back to 80. COLUMNS80 retains the exact help asset and usage. Other widths use simple word wrapping, retain whole words and omit argparse's extreme-width thresholds/action-group emulation. |
