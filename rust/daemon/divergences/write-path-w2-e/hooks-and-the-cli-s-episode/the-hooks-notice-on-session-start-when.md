| Item | Python | Rust | Owner / why |
|---|---|---|---|
| The hooks notice on session-start when `PSEUDOLIFE_PLUGIN_DIR` is unset | the daemon's hooks digest falls back to `plugin/` beside the package, so a plugin of the daemon's version with other hooks gets the notice | no digest, so no notice | inherits "Plugin dir for `hooks_digest`" above: the images set the variable, and so does a checkout run |
