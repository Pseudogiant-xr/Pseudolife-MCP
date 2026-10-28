| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| Maintainer passkey verifier: clientDataJSON nested past 512 levels | `json.loads` accepts it up to the stack's recursion limit (about 990 levels, moving with the caller's stack), then raises `RecursionError` rather than `AssertionInvalid` | refused as `client_data_json` from level 513 | a browser's clientDataJSON is one flat object; the cap keeps the parser's stack bounded |
