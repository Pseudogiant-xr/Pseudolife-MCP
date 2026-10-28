| Item | Python | Rust | Why |
|---|---|---|---|
| Plugin dir for `hooks_digest` | `PSEUDOLIFE_PLUGIN_DIR`, else `plugin/` beside the package | `PSEUDOLIFE_PLUGIN_DIR` only | the image sets the env var; a checkout run sets it too |
