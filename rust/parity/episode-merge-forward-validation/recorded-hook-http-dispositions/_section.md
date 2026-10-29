### Recorded hook HTTP dispositions

`hook-absent-accept-wildcard` applies only when the oracle has no Accept field
and the native request has one field with exactly `*/*`. The explicit 53-case
ledger is:

- `briefing-content-invalid-utf8`
- `briefing-content-redirect`
- `briefing-coordination-hook`
- `briefing-coordination-off`
- `briefing-custom-negative-unknown`
- `briefing-env-only`
- `briefing-error-ambiguous`
- `briefing-error-flag`
- `briefing-error-int`
- `briefing-error-missing`
- `briefing-health-error-json`
- `briefing-health-non-json`
- `briefing-health-null`
- `briefing-help`
- `briefing-hook-json`
- `briefing-hook-plain-context`
- `briefing-launcher-default`
- `briefing-launcher-override`
- `briefing-markdown-false`
- `briefing-plain`
- `briefing-token-control`
- `briefing-token-del`
- `briefing-token-fold`
- `briefing-token-latin1`
- `briefing-token-nonlatin`
- `prompt-baseline`
- `prompt-body-without-lf`
- `prompt-changed-note`
- `prompt-env-token`
- `prompt-invalid-input-0`
- `prompt-invalid-input-1`
- `prompt-invalid-input-2`
- `prompt-invalid-input-3`
- `prompt-invalid-input-4`
- `prompt-invalid-input-5`
- `prompt-invalid-input-6`
- `prompt-invalid-input-7`
- `prompt-invalid-input-8`
- `prompt-invalid-input-9`
- `prompt-invalid-utf8-preserves`
- `prompt-json-nan-extra`
- `prompt-json-surrogate-extra`
- `prompt-malformed-preserves`
- `prompt-mark-invalid-ascii`
- `prompt-mark-nonascii-after-lf`
- `prompt-quiet-advance`
- `prompt-redirect-preserves`
- `prompt-session-max`
- `prompt-session-too-long`
- `prompt-stdin-replacement`
- `prompt-token-file-missing`
- `prompt-token-file-no-token`
- `prompt-token-file-wins`

Outside this ledger Accept is exact. `http-field-name-case-insensitive` and distinct-name order
use HTTP association; same-name value order, multiplicity and bytes stay exact.
No Referer is generated on `/health` redirects or discarded by comparison.
The `Python-urllib/3.11` User-Agent is oracle-pinned. Before 2026-10-09 the
native transport dropped urllib's `Accept-Encoding: identity` and
`Connection: close` and added `Accept: */*`; the current `hook_http` client
sends urllib's fields exactly, and the historical wire differences remain
retained rather than receiving a comparison waiver.
`http-forbidden-input-refused` names bearer C0/DEL/folded refusals; it replaces
the pending-policy description without rewriting Python observations.
Non-JSON `/health` means quiet no-daemon, separately from the consumed payload
scope of `hook-strict-json-refusal`; valid degraded JSON 503 remains available.
