### Fixed (2026-11-02 — Run operator commands in either daemon image)
- Select the native operator executable in Rust-labelled daemon containers while retaining the Python image's command path, argument boundaries, terminal, streams and exit status.
- Add native stopped-daemon invite administration, HTTPS pairing, terminal code entry, maintainer polling and operator help.
- Refuse redirects during pairing credential verification, retaining the token for recovery when verification cannot be confirmed.
- Restore terminal echo when pairing code entry is interrupted on POSIX hosts.
