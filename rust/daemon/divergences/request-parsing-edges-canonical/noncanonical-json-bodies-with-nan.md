| Item | Python | Rust |
|---|---|---|
| Noncanonical JSON bodies with `NaN`/`Infinity`, lone surrogate escapes, or nesting past serde's 128 levels | accepted (or a 500 past ~1000 levels); a truthy SessionEnd ID may enter its mutation handler | REST and SessionEnd return 400 `invalid_json`, preserving the native strict JSON domain; the HTTP-BODY-LIMITS corpus includes fixed/chunked `{"session_id": NaN}` probes whose Python replies are 200 `{ok:true}` with unchanged disposable bank state; outside shipped canonical SessionEnd producers |
