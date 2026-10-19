# Unix account comparison

This isolated Linux comparison uses the actual locked `users` 0.11.0 crate as its reference and imports the production resolver directly. It is outside the runtime Cargo workspace: the shim does not depend on `users`.

Run from the repository root with Rust 1.94.0:

```sh
CARGO_INCREMENTAL=0 cargo +1.94.0 run --locked --offline --jobs 2 --manifest-path evals/rust_port/users_comparison/Cargo.toml
CARGO_INCREMENTAL=0 cargo +1.94.0 test --locked --offline --jobs 2 --manifest-path evals/rust_port/users_comparison/Cargo.toml
```

A system account, the current real account, a generated missing name and an interior-NUL name are compared across four threads. Home paths and UID values are compared internally; output includes only counts and booleans. The imported module also runs its six synthetic lookup tests.

`users` is unmaintained and has two advisories with no patched release: GHSA-m65q-v92h-cm7q (CVE-2025-5791, `get_user_groups` and `group_access_list` append `root` to group lists) and GHSA-jcr6-4frq-9gjj (unaligned read in the group-member list behind `Group` lookups). This instrument calls only `get_current_username`, `get_user_by_name`, `UserExt::home_dir` and `get_current_uid`, which read `passwd` entries and never reach either group path. The pin is deliberate: the comparison's reference is the exact crate the shim used before, so the maintained fork `uzers` would compare against a different reference and the published receipt would no longer describe this instrument. Nothing ships or builds this project in CI; it runs only by hand on Linux as the user who invokes it.

This checks the local NSS backend and real UID. It does not establish Android or other Unix behavior, unusual NSS backends, or behavior when real and effective UIDs differ. Populated error results and null home fields are deliberately rejected by the replacement; those defensive cases are not claimed equivalent to the former crate. The instrument requires Linux and a positive account entry.
