| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| POST | /api/pair | 3/4 | deferred | served | Not a `ConsoleRoutes` entry (`web/api.py` serves it before the bearer gate), so `routes.json` omits it. Rust serves the refusals (405, 403 `forbidden_origin`, 415, 429, 413, 400 `pairing_refused`), which hosted CI compares (`body-pair-budget`, `body-limits`, security cases); a valid body on an authenticated install redeems (B5; `run.py` `pairing`, live only) |
