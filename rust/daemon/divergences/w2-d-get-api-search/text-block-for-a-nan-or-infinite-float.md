| Item | Python | Rust | Why / owner |
|---|---|---|---|
| Text block for a NaN or infinite float in an answer (`memory_history`'s `as_of` echo, a world fact's `retrieved_at`) | the SDK's `pydantic_core.to_json` text spells it `NaN` / `Infinity`, while structured content holds null | null in both (the envelope renders the text from the structured value) | structured content, which the harness compares, matches; only `float()` spellings reach these (`as_of="inf"`, `retrieved_at="inf"`) |
