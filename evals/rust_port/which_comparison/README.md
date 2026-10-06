# Actual crate comparison for executable lookup

This standalone CPU instrument links registry `which` 8.0.6 and includes the
production resolver directly from `rust/shim/src/lifecycle_executable.rs`.
It compares paths and acceptance on disposable filesystem fixtures. It adds no
`which` dependency to the production workspace.

Run from the repository with Python 3.11 or newer and the existing Rust 1.94.0
toolchain/cache:

```sh
python evals/rust_port/which_comparison.py \
  --target-dir <existing-cargo-target> \
  --resolver-reference-head <verified-git-head> \
  --output <new-receipt.json> --log <private-command-log>
```

`--smoke` runs only the ordinary-path group. The controller uses the locked
registry crate offline, verifies its package and file checksums, and binds
the instrument, lockfile, production resolver and resulting binary hashes.
The caller verifies the reference head; it is not a claim that an uncommitted
comparison instrument existed at that head. Logs stay private because compiler
paths can identify the machine. Output paths must be new: receipts are never
silently overwritten. The instrument restores no global host environment:
each comparison runs in its own child with explicit PATH, home and cwd, and
the parent removes its owned fixture after the child exits.

Common cases cover ordered PATH, cwd-relative command/PATH entries, missing
and empty PATH, directories, standalone home expansion and literal tilde
prefixes. Unix cases add execute permissions, ordinary/denied/dangling file
symlinks and non-Unicode names/PATH entries. Windows cases add PATHEXT order,
filename casing, known/unknown extensions, raw candidates, extensionless
native binaries/text, invalid-Unicode PATHEXT and directory junctions. File
symlink creation errors are reported as unavailable coverage, never a pass.

Windows policy is deliberate per-call PATHEXT parsing. The real crate's public
`which::which` API also rereads in 8.0.6 because `Sys for &T` uses the default
parser; `WhichConfig::new_with_sys(RealSys)` uses the actual cached override.
After `.CMD` changes to `.EXE`, the public API and replacement reject a later
`.CMD` candidate; the owned configuration accepts it from the cached list.
That one expected difference is recorded separately from equal public-API
cases. A different result fails the instrument.

This is bounded path/acceptance evidence. Error types/text, exhaustive ACLs,
all platform variants and arbitrary concurrent environment mutation are not
claimed equivalent. No full suite, service, GPU or installation is involved.
