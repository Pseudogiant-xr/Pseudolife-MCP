### Fixed

- The Rust daemon parity harness waits for the stored-principal view to load before the `encodings` scenario's cases. A bearer that matches no configured token reaches that view, and before its first read either daemon answers `principals_unavailable`. That is correct startup behaviour, but a slow CI runner sometimes caught the Rust arm in that window, and the scenario then reported a false difference.
