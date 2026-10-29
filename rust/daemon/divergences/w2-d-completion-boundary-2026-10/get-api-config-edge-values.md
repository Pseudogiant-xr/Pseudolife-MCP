| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| `GET /api/config` edge values | a value set at boot as `.nan` or `.inf` is served as NaN or Infinity; an int knob value past the i128 range (`1e300`, a 45-digit literal) prints the exact integer in its bound message | served as `null` (the boot leaf is kept as JSON); the bound message prints the value clamped to the i128 range (still a 400) | a NaN or infinite knob value is a broken config either way; the Console never sends such an integer |
