# Worktree and build-cache retention

Finished git worktrees and their Rust build caches pile up on a dev drive:
one hand cleanup on 2026-10-27 removed 35 worktrees and 43 cache dirs,
about 300 GB. `ops/prune_worktrees.py` does the same job with the same
checks, so it can run unattended. It is a dry run unless given `--apply`.

There are two triggers:

| Trigger | Command | Grace period |
|---|---|---|
| A session whose work has all merged (primary) | `python ops/prune_worktrees.py --mine --apply` | skipped: the owner says it is done |
| Weekly, if registered (backstop) | Scheduled Task → `python ops/prune_worktrees.py --apply` | worktrees 3 days, caches 7 days |

## Owner mode: `--mine`

Run it from inside your own worktree when all of its work has merged and
nothing is stacked on it:

```bash
python ops/prune_worktrees.py --mine                       # what it would do
python ops/prune_worktrees.py --mine --apply               # do it
python ops/prune_worktrees.py --mine --cache-dir <dir> --apply
```

- It targets only the worktree it runs from, plus one cache dir: the one
  named by `--cache-dir`, else `$CARGO_TARGET_DIR`.
- A `$CARGO_TARGET_DIR` is cleaned only when it sits directly under a
  configured cache root (below). Elsewhere it may be one dir that every
  session on the machine shares, so it is left alone with a note. Name your
  own slice with `--cache-dir` instead.
- Every worktree check below applies except the grace period. A cache dir
  has no age check. Instead, no running process may name it on its command
  line. Where the process list cannot be read, that check is skipped and
  the output says so.
- Any failed check refuses the whole run, names the reason and changes
  nothing. The cache dir is refused, too, when it is or contains a
  configured cache root, contains a worktree, sits inside another
  worktree (the main checkout included), sits inside your own worktree
  anywhere but a cache path such as `rust/target`, or is a drive root, the
  home dir or one of its ancestors.
- The removal runs from the main worktree (`git -C <main> worktree remove
  <own path>`), because a worktree cannot remove itself while a shell sits
  in it. If your shell still holds the folder (or one inside it) as its
  working directory, empty folders remain until the shell exits; git has
  already unregistered the worktree, and that counts as removed. If git
  unregistered it but files remain (one held open on Windows), the run
  prints `partial:` with the first leftover, exits 1 and leaves the cache
  alone. If git's worktree list can't be read afterwards, it prints
  `unknown:` rather than guess, and the ledger line says `state:
  unknown`.
- `git worktree remove` may take up to an hour on a large `rust/target`.
  Run `--mine --apply` where a short tool timeout won't kill it midway
  (in the background, or with a long timeout).
- A checkout under `.t3/worktrees`, `.claude/worktrees` or
  `.codex/worktrees` belongs to its harness. Owner mode leaves it in place
  and cleans only the cache dir.
- It prints one line naming what it removed. The local branch is always
  kept.

## Sweep mode

Sweep mode looks at every worktree of `--repo` under a configured worktree
root, and every immediate subdir of a configured cache root.

### Worktree rule

A worktree is removed only when all of these hold. Each check runs again
just before the removal.

1. **Merged.** After a fresh `git fetch origin master`, its HEAD is an
   ancestor of `origin/master` (`git merge-base --is-ancestor`). This works
   for detached HEADs. A HEAD with commits not on master is never removed,
   even if its branch is pushed. If the fetch fails, the whole run is
   refused rather than trusting stale refs.
2. **No links.** Nothing under the tree is a symlink or reparse point
   (Windows junctions included), found by an lstat walk that never follows
   a link. `git worktree remove --force` once followed a junction and
   deleted the data behind it. This runs before the status check, because
   git also lists an untracked symlink as a change and the link is the
   reason that matters.
3. **Clean.** `git status --porcelain --ignored=matching` shows no tracked
   or untracked change. Every ignored path is a known cache:
   `__pycache__/`, `.pytest_cache/`, `rust/target/`, `*.egg-info/`,
   `.mypy_cache/` or `.ruff_cache/`. Any other ignored path refuses the
   removal and is reported. An eval log under `local/data/` is the case
   this check exists for, and `.venv/` is refused the same way (on Linux
   and macOS a venv holds symlinks, so check 2 reports it first).
4. **Idle.** Nothing outside the cache whitelist was written within
   `--worktree-grace-days` (default 3). The same holds for the worktree's
   `HEAD` and `index` in its git admin dir. Workers reuse one worktree
   across stacked PRs, so a tree is clean and merged the moment the first
   PR lands while its session is still working in it.
5. **Not kept.** It has no keep marker (below).
6. **Not the main worktree,** and not the worktree the script runs from.
   A locked worktree is refused as well.

Removal is a plain `git worktree remove <path>`, never `--force`.
`git worktree prune` runs at the end of an applied sweep. Local branches
are never deleted.

### Cache rule

An immediate subdir of a cache root is deleted only when all of these hold:

- no file under it was written within `--cache-grace-days` (default 7);
- nothing under it is a symlink or reparse point;
- no `.cargo/config.toml` pins it as `target-dir`. The files checked sit
  at the top of, and one level into, every registered worktree and each
  immediate child of a worktree root (so `rust/.cargo/config.toml`
  counts), plus the cargo home's. Pins are read again just before each
  deletion;
- it can be renamed aside before deletion. On Windows, a file held open,
  such as by a running cargo build, makes that fail, and the dir is
  skipped: no retry, no force. Linux and macOS allow the rename, so there
  the idle period (sweep) and the process check (owner mode) are the
  protection.

A cache dir that holds a POSIX mount point is refused like a link.

A cache root that contains a git worktree, or sits inside one, is skipped
whole. A drive root, the home dir or any of its ancestors is never used as
a root, whatever the configuration says.

### Never touched

The script touches nothing outside a configured root. It never touches
harness-managed checkouts (`.claude/worktrees`, `.t3/worktrees`,
`.codex/worktrees`) or temp dirs, even when one is configured as a root.
The report only counts what it left alone there.

## Configuration

Roots come from flags (`--repo`, `--worktree-root` and `--cache-root`, the
last two repeatable) or from a private file. Flags win. The tree ships no
machine-specific defaults. With no roots configured, a sweep prints how to
configure them and exits 0 without touching anything.

`~/.pseudolife-mcp/retention.toml`:

```toml
repo = "<main checkout>"
worktree_roots = ["<dir holding your worktrees>"]
cache_roots = ["<dir holding one CARGO_TARGET_DIR slice per branch>"]
worktree_grace_days = 3
cache_grace_days = 7
```

`--repo` defaults to the checkout the script lives in.

## Keep marker

To keep a worktree that would otherwise qualify, mark it either way:

```bash
touch <worktree>/.pseudolife-keep                     # file at the worktree root

git config extensions.worktreeConfig true             # once per repository
git -C <worktree> config --worktree pseudolife.keep true
```

The config key is read from every scope, so setting it at repository or
global scope keeps every worktree. Unset it with `git -C <worktree> config
--worktree --unset pseudolife.keep`.

## Ledger and output

The output is a table: each candidate, its verdict, its size and the first
failing check. Totals follow, with the reasons things were kept. The exit
code is 0 on success, including when there is nothing to do and when
candidates are refused one by one. It is 1 for an owner-mode refusal, a
failed `git worktree remove`, a worktree git unregistered but could not
fully delete (a file held open on Windows; the leftover path is named and
its ledger line says `complete: false`), a cache deleted only in part (the rest is
left in a `.<name>.retention-*` dir beside it, named in the output), or a
ledger that could not be written. It is 2 for a refused run (failed
fetch, bad repo or bad config). Config values are type-checked: roots
must be lists of paths.

Every removal appends one JSON line to
`~/.pseudolife-mcp/retention-ledger.jsonl`. The line holds `time`, `mode`
(`sweep` or `owner`), `kind`, `path`, `bytes`, `newest_mtime`, `head` and
`branch` (worktrees), the `checks` passed and `complete` (false when
files were left behind, with `state` set to `partial` or `unknown`). In a
sweep, a git failure on one row fails that row and the run carries on. That
is the path, size and reason the disk rule asks for. A dry run writes
nothing.

## The weekly Scheduled Task (Windows)

```powershell
.\ops\install-worktree-retention.ps1                          # Sunday 04:00
.\ops\install-worktree-retention.ps1 -DayOfWeek Wednesday -At 21:00
.\ops\install-worktree-retention.ps1 -Python <path to python.exe>
.\ops\install-worktree-retention.ps1 -Unregister
```

The task is named `Pseudolife-MCP worktree retention`. It runs `pwsh.exe`
by absolute path, resolved as `ops/install-cache-retention.ps1` resolves
it (see [the Docker retention runbook](../runbooks/docker-disk-retention.md)
for why a bare `pwsh.exe` fails under Task Scheduler). The script runs with
an absolute Python path and `--apply`, and each run's output goes to
`~/.pseudolife-mcp/retention-last.log`.

- **It refuses to register from a linked worktree,** because this task
  would later remove that worktree and every run after would fail
  silently. Run it from the main checkout.
- **Rerunning is safe:** with nothing changed it says so in one line.
- Until `retention.toml` names roots, each run is a no-op, and the
  installer says so.

On Linux and macOS, run the same command from cron or a systemd timer.
