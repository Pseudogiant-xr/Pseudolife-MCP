| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| `coordination.maintainer.origin` with a non-ASCII netloc | `urlsplit` raises when the netloc's NFKC form adds a `/`, `?`, `#`, `@` or `:` (a fullwidth `＠`), so `problem` escapes and the maintainer reads answer 503 `coordination_unavailable` | no NFKC check: the origin is refused by `config_problem`'s exact-origin rule, 409 `maintainer_https_required` | a misconfiguration either way, refused either way; only the status and code differ |
