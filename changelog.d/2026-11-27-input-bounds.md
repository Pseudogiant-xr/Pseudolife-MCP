### Fixed (2026-11-27 — size bounds on extractor, release and PDF reads, and stricter bracketed-host parsing)
- The extractor shim answers 413 to a `/chat/completions` body over 16 MiB
  instead of buffering it whole.
- `pseudolife-mcp update` refuses a Compose release archive that unpacks to
  more than 8 MiB, which no Compose bundle does.
- The daemon reads at most 32 MiB of a dream extractor's answer or probe
  body and at most 1 MiB of GitHub's latest-release document.
- PDF ingest refuses a PDF with more than 10,000 pages or more than 64 MiB of
  text, naming the bound, instead of extracting without limit; a page whose
  text alone would pass the budget is refused before its buffer is allocated.
- A daemon URL such as `http://evil.example[::1]` or `http://[::1]x` is no
  longer accepted or read as loopback: a bracketed host must be the whole
  host, then nothing or `:<port>`, in the native client and in the daemon's
  browser gate. The daemon's MCP Host and Origin checks accept only a numeric
  port after the allowlisted host (`localhost:1.evil.example` passed before).
