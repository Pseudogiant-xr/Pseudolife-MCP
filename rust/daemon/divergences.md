# Declared divergences: Rust daemon vs the Python daemon

What the Rust daemon deliberately does not match yet, and why. The
cutover gate (contract-first plan, method item 5) is this list reaching
zero, or the maintainer accepting what remains. Each row names the slice
expected to close it. The harness checks every "answers 501" row: the Rust
side must answer exactly `501 {"error": "not_implemented", "path": P}`.

The rows live in `divergences/`, one file per entry, so pull requests that
add or close different divergences never edit the same lines. Each section
is a directory (a `###` section nests inside its `##` section) whose
`_section.md` holds the heading and the section's prose. An entry is one
table row under its table's header, or one paragraph opening with a bold
label. Add a divergence as a new file in its section; close one by deleting
its file. `python rust/daemon/split_divergences.py --render` prints the
whole ledger as one document, and `--check` verifies the layout.

A branch that still edits the single-file ledger: when merging master, keep
the branch's version of this file (`git checkout --ours
rust/daemon/divergences.md`), run `python rust/daemon/split_divergences.py`,
then `git add -A rust/daemon/divergences.md rust/daemon/divergences`. The
script applies the branch's own changes, entry by entry, and restores this
pointer.
