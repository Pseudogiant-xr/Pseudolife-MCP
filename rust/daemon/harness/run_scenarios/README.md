# run.py scenario families

`run.py` loads every `*.py` module in this directory at import, in file-name
order, so a pull request that adds a scenario family adds a file here and
does not edit `run.py`'s scenario list. Two pull requests adding different
families never touch the same lines.

A module defines `register(harness)`, which receives the `run` module (the
same object whether `run.py` is the script or imported) and returns its
`Scenario` subclasses, in the order the default run takes them:

```python
"""Pairing redemption scenarios (W2F-PAIRING-HTTP)."""


def register(harness):
    class Pairing(harness.Scenario):
        name = "pairing"

        def cases(self):
            return [harness.case("redeem", "POST", "/api/pair")]

    return [Pairing]
```

A scenario name that is already registered is refused at import. The
existing scenarios in `run.py`, `background_sessions.py` and
`background_maintenance.py` keep their place and their order.

`register` may also add a per-route response scrub with
`harness.register_response_scrub("/api/config", scrub)`: `scrub(resp)`
returns the normalized response, applied by `normalize_response` to every
answer for that path (unquoted, without the query). One scrub per path.

## Declarations

Declared route cases and `/health` keys live one file each under
`../declared/`, read by `run.py` at import:

- `declared/cases/<slug>.json`, `{"case": "<case name>", "declared":
  "<owner: what>"}`: that case of `gate_cases`, `Tokens` or `Tokenless`
  must answer `501 not_implemented` on the Rust side. The pull request that
  serves the route deletes the file; a file naming a case no scenario has
  stops `run.py` with the stale name.
- `declared/health/<slug>.json`, `{"key": "<key>"}`: a `/health` key only
  Python emits. The pull request that adds the Rust producer deletes it.

`<slug>` is the name lowercased with each run of other characters replaced
by `-` (`maintainer known GET` is `maintainer-known-get.json`).
