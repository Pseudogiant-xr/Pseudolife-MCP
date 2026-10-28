| Surface | Python behaviour | Owner |
|---|---|---|
| Stored callers' POST `/api/config` and `/api/daemon-notice` gate | HTTP already returns 403 `operator_principal_required` by stored-name inference; method-aware source policy is implemented for HTTP-SECURITY to consume with `resolve_detailed` | HTTP-SECURITY |
