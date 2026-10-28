| Item | Python | Rust | Owner / why |
|---|---|---|---|
| The daemon version the session-start notices compare and print | the package version (PEP 440: a pre-release reads `0.20.0rc1`) | the crate version (semver: `0.20.0-rc1`) | inherits `version` ("free until cutover") above; the strings are equal for every final release, and a pre-release cut would print a false "plugin and daemon differ" notice on each Rust session until the daemon reports the package version (W3-J / the release procedure) |
