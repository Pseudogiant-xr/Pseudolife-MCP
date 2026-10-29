| Input/output | Producer | Declared native substitution |
|---|---|---|
| `wait-mail-direct-stdout`: Failed stdout writes/flushes | Caller-owned pipes and redirected descriptors. | Direct failure exits 2 and leaves mail unshown, without seen/ledger advance. `PYTHONUNBUFFERED`, block size and CPython shutdown retries do not change this behavior. Delivery retains `wait-mail: could not write the mail to stdout (<error>); left it unshown.` Help failure uses `wait-mail: could not write help to stdout (<error>).` No synthetic `TextIOWrapper` shutdown exception is emitted. |
