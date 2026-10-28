| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A non-finite float (REAL or double) in the pre-apply graph snapshot file | `NaN`, `Infinity`, `-Infinity` (`json.dumps` defaults) | `null` | the five tables' float columns hold confidences, scores and timestamps; matching Python needs a non-standard JSON writer |
