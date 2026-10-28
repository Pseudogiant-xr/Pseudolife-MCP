| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| `/api/overview`'s tag count with a non-string tag on an entry | `list_tags` counts it as a dict key (a list or dict tag raises, and `_safe` serves `{"tags": [], "total": 0}`) | any non-string tag serves the `_safe` default | no tool writes one (`normalize_tags` keeps strings), as the `memory_episode_summary` distributions row above |
