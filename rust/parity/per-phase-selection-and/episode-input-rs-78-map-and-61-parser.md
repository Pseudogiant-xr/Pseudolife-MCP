| Episode site | Producer/input class | Substitution or retained reason |
|---|---|---|
| `episode_input.rs:78` map and `:61` parser | User stdin / legacy external hook ordinary JSON object and string fields | Narrow to typed serde_json fields; no general Python truthiness/repr, NaN/Infinity conversion, arbitrary numeric session coercion or PYTHONINTMAXSTRDIGITS setting. Duplicate string fields retain last-value semantics. |
