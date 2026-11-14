### Fixed (2026-11-13 — `expose` answers output it cannot read instead of crashing)

- `pseudolife-mcp expose` stopped with a Python traceback, several times
  after it had already changed the serve configuration, when `tailscale`
  printed output that is not UTF-8, JSON nested very deeply, a
  `Self.TailscaleIPs` that is a number, or text holding a lone surrogate.
  Each now gets the ordinary answer: "could not read what `tailscale ...`
  printed (...)", with exit 4 when nothing was changed, and after a change
  exit 5 with how to check `tailscale serve status` and undo it. `expose off`
  now also exits 5 with that advice when its result cannot be read back
  after the removal ran (it exited 4, which means nothing changed).
- `expose` accepted an address written in another script's digits (or with
  an octet over 255, or a leading zero) as the tailnet address and could
  print it as the client URL. Only an ASCII dotted quad is an address now,
  and `--port` takes only ASCII digits.
