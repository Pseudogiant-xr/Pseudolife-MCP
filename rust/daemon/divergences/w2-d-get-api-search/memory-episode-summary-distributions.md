| Item | Python | Rust | Why / owner |
|---|---|---|---|
| `memory_episode_summary` distributions over non-string tags | dict keys (`True == 1`) in `(-count, tag)` order, raising a TypeError when equal counts compare tags of different types | each distinct JSON value its own key; equal counts order by its JSON text (a list or object tag fails with Python's `unhashable type` text) | no tool writes a non-string tag (`normalize_tags`); a hand-written row would have to |
