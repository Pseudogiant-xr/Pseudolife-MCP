| Key | Python | Rust | Owner | D7 bucket | Code verification / B14b owner |
|---|---|---|---|---|---|
| `version` | the package version | the crate version | closed: crate and package release versions are pinned together | closed | `rust/daemon/src/health.rs::payload` uses `CARGO_PKG_VERSION`; `rust/daemon/Cargo.toml` and `pyproject.toml` are both 0.21.0; `tests/test_plugin_packaging.py` pins their equality and `health.rs::payload_reports_the_release_version_never_the_placeholder` pins the served value. B14b owner: none (closed). |
