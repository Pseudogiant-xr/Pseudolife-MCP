### Fixed (2026-11-04 — Keep an existing install's credential authority)

- Docker installers refuse a different checkout or a lost authentication
  configuration before minting credentials or changing the stack. Missing
  recorded origins name the recovery step; original env-file authority remains
  valid with packaged Compose files. The daemon-host epilogue now explains why
  its checkout and `ops/.env` must be retained.
