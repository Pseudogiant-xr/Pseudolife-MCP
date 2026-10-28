| Item | Python | Rust |
|---|---|---|
| Malformed or non-object SessionEnd bodies | becomes an empty object and returns 200 `{ok:true}` with no writes | deliberately returns 400 `invalid_json`: never acknowledge a close that did not happen; non-object SessionEnd bodies also use `invalid_json`, while other REST routes use `body_must_be_object`; empty bodies and valid objects retain their handling. The four HTTP-BODY-LIMITS scenarios now contain 132 live/golden cases, including 16 invalid SessionEnd probes. |
