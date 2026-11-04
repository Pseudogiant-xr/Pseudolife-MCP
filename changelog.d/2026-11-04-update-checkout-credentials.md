### Fixed (2026-11-04 — Preserve checkout update credentials)

- Checkout updates refuse a missing `ops/.env` before backup or deployment,
  naming the running daemon's credential-source variables without exposing their
  values and asking for the original environment file. Updates no longer copy
  commented defaults over a lost configuration. The post-deploy health gate also
  checks that the authentication mode is unchanged before moving clients.
