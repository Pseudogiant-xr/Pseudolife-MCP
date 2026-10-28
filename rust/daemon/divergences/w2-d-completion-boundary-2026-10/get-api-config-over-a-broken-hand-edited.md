| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| `GET /api/config` over a broken hand-edited config.yaml | a file that is not UTF-8, an impossible timestamp (`2026-13-45`) or a bad explicit tag (`!!int "1e5"`) makes the read fail (400, or 405 for `!!bool maybe`) | no `saved` is shown and the read answers 200 | only a restart knob's `saved` hint, on a file the next start refuses anyway |
