| Input/output | Producer | Declared native substitution |
|---|---|---|
| `wait-mail-ascii-numeric-options`: Timeout and interval | Shell/CLI callers and existing launchers supply finite decimal seconds. | ASCII `[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?`, finite after conversion; existing timeout `(0,86400]` and interval `[0.01,60]` bounds remain. Unicode digits, underscores, outer whitespace, NaN/Infinity and overflow refuse with argparse-shaped usage/error and exit 2. |
