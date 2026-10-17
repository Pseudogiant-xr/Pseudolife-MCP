### Added (2026-10-09 — native lease operator parity)

- Verify native `lease hold`, `break`, `delegate` and the deprecated
  `designate` alias through disposable process/database and container
  comparisons, including the shipped Windows lease launcher.
  Embedded PostgreSQL retains its declared native refusal; external DSNs
  and container transport are covered.
- Prove that a failed `lease break` or `lease delegate` leaves the bank
  untouched in both implementations: an audit write the server refuses, a
  refused COMMIT and a COMMIT whose answer is lost all roll back.
