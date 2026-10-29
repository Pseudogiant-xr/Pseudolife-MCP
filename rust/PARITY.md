# Behaviour parity register

Every Python behaviour the Rust port must keep, with each row's port status
and evidence.

The register lives in `parity/`, one file per table row, so pull requests
that change different rows or different sections never edit the same
lines. Each `##` section is a directory named by its heading's opening
words (`PARITY.md#cli-mode-acceptance-checklist` is now
`parity/cli-mode-acceptance-checklist/`); a `###` section nests inside its
`##` section. A section's `_section.md` holds the heading, the prose and
each table's header and separator lines, in order. Each row is a file
holding its table's header, separator and the row, named by the row's key:
its first cell (the first two under a `Method` or `Platform` column), cut
to the opening words. Edit a row in its file, add one as a new file beside
its table's rows, close one by deleting its file.
`python rust/split_parity.py --render` prints the whole register as one
document (each table's rows in name order), `--check` verifies the layout,
and tools read it through `split_parity.register_text()`.

A branch that still edits the single-file register: when merging master,
keep the branch's version of this file (`git checkout --ours
rust/PARITY.md`), run `python rust/split_parity.py`, then `git add -A
rust/PARITY.md rust/parity`. The script applies the branch's own changes
three-way against the directory and restores this pointer; a file both
sides changed on the same lines gets conflict markers to resolve by hand.
Merge, don't rebase: the script needs the merge base. A stack forward-merges
bottom-up, each layer merging the forward-merged layer below it.
