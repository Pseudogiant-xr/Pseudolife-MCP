### Fixed (2026-10-10 — native connect request fields, maintainer console listing, harness identity and credential checks)

- The experimental native `connect` sends its health probe, credential check
  and board probe as Python's urllib does: `Host` as written,
  `User-Agent: Python-urllib/3.11`, `Accept-Encoding: identity`,
  `Connection: close`, no `Accept`, per-receive timeouts and urllib's
  redirect limits. It used to send reqwest's fields (`Accept: */*`, no
  `User-Agent`), which the old request capture could not see.
- The experimental native `maintainer list` writes to a Windows console
  through the console's Unicode path, as Python does, so a label such as
  `☎` renders correctly whatever the console's code page. Pipes and files
  keep the counted byte writer.
- CLI harness: doctor's runtime identity is now checked against the
  executable the harness launched (a nonexistent same-named executable
  elsewhere was accepted), and the handshake shim's cache file is compared
  byte for byte instead of as parsed JSON; connect compares its own
  requests raw, every header field and body, with generated fixture
  credentials redacted; the maintainer row seeds deterministic banks. Doctor, connect and maintainer
  gain Windows goldens.
