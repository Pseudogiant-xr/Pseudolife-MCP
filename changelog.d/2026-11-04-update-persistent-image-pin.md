### Fixed (2026-11-04 — Keep stack redeploys on the updated release)

- Release updates warn once when the stack's persistent configuration still
  selects an older image. The message names the environment variable or daemon
  image edit that keeps the new release on a later manager redeploy, rather than
  silently reattaching an older image to the upgraded bank.
