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

### Rules every new class shares

- **One run at a time per OS home.**
  - A second run there (a scheduled sweep beside a hand run) refuses while
    `~/.pseudolife-mcp/locks/retention.lock` (beside the ledger) is held.
  - A Windows run and a WSL run lock different files, so they can judge
    one drive at once. The second one's rename aside of a dir then fails,
    which keeps that dir, so the cost is a partial row, not lost data.
- **Absolute roots only.** An empty or relative root refuses the run, so an
  unset variable cannot sweep the current dir.
- **No link, junction or mount point.** One anywhere in a candidate keeps
  it whole. On Linux the mount list (`/proc/self/mountinfo`) is read too,
  because a bind mount on the same filesystem looks like a plain dir.
- **No process uses it.** No process names it on its command line or runs
  an executable from inside it (the CIM `ExecutablePath` on Windows,
  `/proc/<pid>/exe` on Linux). On Linux, no process may work in it either.
  When the process list cannot be read, or `/proc` hides other users'
  processes (`hidepid`), nothing is removed. A process whose command line
  and executable both live elsewhere, or one the sweep cannot see (an
  elevated Windows process), is not seen. On Windows the rename aside then
  fails while any file inside is open, which keeps it.
- **No inherited git environment.** The script clears every variable that
  points git elsewhere (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
  `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`,
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_NAMESPACE`,
  `GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`) for every
  git call.
  - It also clears the caller's config (`GIT_CONFIG`, which alone can hide a
    `pseudolife.keep` marker; `GIT_CONFIG_PARAMETERS` and the
    `GIT_CONFIG_COUNT`/`KEY`/`VALUE` family from a parent's `git -c`;
    `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_NOSYSTEM`), so git
    reads the user's own files from their usual places.
  - It sets `GIT_NO_LAZY_FETCH=1` and `GIT_NO_REPLACE_OBJECTS=1`, and passes
    `-c core.fsmonitor=false -c log.showSignature=false`.
- **Look again just before the delete.**
  - The process list is taken again right before each target or clone is
    renamed aside, not once per run.
  - After the rename, the aside copy is scanned again. Any of these puts
    the copy back under its own name, and the dir is kept:
    - a file or dir written since its last checks began (each dir's own
      time read fresh, so a copy that keeps its old file times, `cp -p`,
      still shows);
    - a new link;
    - a process using it.
  - If putting it back fails, nothing is deleted. The row says where the
    copy is, and the run exits 1.
- **Rename aside, then delete.**
  - A candidate is renamed to `.<name>.retention-<pid>-<epoch-seconds>` in
    its own parent, then deleted.
  - A delete that stops part-way leaves that dir, and the row says
    `partial` and names it. No later run finishes it: remove it by hand
    after a look.
  - No rule treats a dir with a similar name specially, so the name cannot
    be used to make anything deletable.

### Target rule (cargo targets judged by their owners)

A child of a `--target-root` is a candidate only when it carries cargo's
own markers: a `CACHEDIR.TAG` with cargo's signature **and** a
`.rustc_info.json`. A uv virtual environment carries the same tag, so the
tag alone proves nothing, and any other child is only counted. A target is
deleted when all of these hold:

- **Every owner is finished.** Its owners are the checkouts its dep-info
  files (`<profile>/*.d`, `<profile>/deps/*.d`) name as sources. An owner
  counts as finished when one of these is true:
  - **Gone:** the source path no longer exists, and no existing ancestor of
    it is a checkout.
  - **Merged:** it is a worktree whose HEAD, or a clone whose every commit
    (rule below), is a commit of the main repository on `origin/master`.

  The main checkout is never finished. Some paths name no owner:
  - a path under the cargo or rustup home, or inside the target itself;
  - a path on a drive this machine does not have;
  - a relative path;
  - a path with no drive read on Windows (a WSL path);
  - a path gone below a mount point or an empty dir on Linux (the drive
    may be absent).

  rustc writes workspace sources relative to the workspace root. The
  absolute ones come from a top-level profile `.d` file and from
  `# env-dep:CARGO_MANIFEST_DIR=` lines, which name a crate's dir exactly.

  Every dep-info file is read. A file that cannot be read whole, or a path
  that cannot be looked at (permission denied), keeps the target, since
  every owner must be finished. A finished owner's own `.cargo/config.toml`
  pin also keeps it.

  A live owner that appears only through relative paths is not found, so a
  target used only by `cargo check` or `cargo test` from a live checkout
  can be deleted after its grace period. That costs a rebuild.
- **Grace:** nothing in it, file or dir, was written within
  `--target-grace-hours` (default 24).
- **No owner found:** a target whose dep-info names no owner is deleted
  only after `--unowned-target-days` (default 14) idle.
- **No build holds it:**
  - No build holds cargo's lock on any profile's `.cargo-lock`.
  - A lock file the script cannot open (another user's build, a read-only
    file, a sharing violation) counts as held.
  - A target with no lock file in any profile is kept.
  - On Linux and macOS the script holds every profile's lock across the
    rename and the delete, so a build that starts meanwhile waits.
  - On Windows it takes each lock without waiting and releases it at once,
    because an open lock file would make the rename fail every time; there
    the rename itself fails while any file in the tree is open.
- **The usual cache checks:** it is not pinned by a `.cargo/config.toml`
  (the `[build]` table form or the dotted `build.target-dir` form).

**What is deleted:**
- Only the entries cargo's layout explains, each whole, with everything
  inside it:
  - profile dirs (which carry `.fingerprint` or `.cargo-lock`), including
    their `deps`, `build`, `incremental`, `examples` and `.fingerprint`
    dirs and everything in those, and the files cargo writes at their top
    (its lock, the outputs its own top-level dep-info names, and their debug
    info and import libraries, `-` and `_` alike);
  - target-triple dirs of profiles;
  - `tmp`, `doc` and `package`, by name, with everything in them.
- A profile dir counts only when its own dep-info names outputs inside it.
  A copy (`cp -r release release-pr1234`, a kept candidate build) still
  names the original, so it stays as a foreign entry.
- A profile dir also stays, as a foreign entry, when its top level holds
  anything else, whatever its age: a copied `server-good.exe`, notes, a
  folder of saved results, a link, or a binary whose dep-info is missing.
  A user's file deeper inside cargo's own dirs (`deps`, `build`,
  `incremental`, `examples`) goes with them.
- `criterion` (benchmark measurements, not a cache) stays by name.
- Anything else, such as copied candidate binaries or mutation-test
  sources, stays, and the row names it.
- When anything stays, `CACHEDIR.TAG` and `.rustc_info.json` stay too, so
  the dir remains a target. Once nothing of cargo's own is left, later
  sweeps keep it and name what is there.
- When nothing foreign is there, the whole target is renamed aside and
  deleted.

The home dir may be a target root, because WSL workers leave targets
directly in it and only marked children are ever candidates. A drive root
or anything above the home never is.

### Clone rule (standalone checkouts)

A child of a `--clone-root` with its own `.git` dir, and not a registered
worktree, is deleted (renamed aside, then removed) when all of these hold:

- **Every commit object in the clone is a commit of the main repository on
  `origin/master`.** That covers every ref (branches, tags,
  remote-tracking refs, notes) and every reflog entry. It also covers every
  commit the object store holds (`git cat-file --batch-all-objects`), so a
  commit left on a detached HEAD with logging off, or one whose reflog
  entry expired, still keeps the clone. A clone's remote may be a bundle or
  another machine's path, so "pushed" means nothing here.
- **It has no stash, and it is clean by the worktree rule's whitelist.**
  Untracked files are always shown (`--untracked-files=all`, whatever the
  clone's config says), and so are submodule changes. A clone holding
  submodules (`.git/modules`) or a file marked assume-unchanged or
  skip-worktree is kept, since status can hide their changes.
- **It holds no link, has no keep marker, and nothing in it was written for
  the worktree grace period.**
- **It is not the checkout this run works from.**
- **Git resolves its work tree to this very dir, and it owns no linked
  worktree** (their commits and index live in its `.git`).
- **Its own `.git` names no other dir (`commondir`), and its config sets
  nothing under which git runs a program or reads another file.** The keys
  that keep it:
  - `core.fsmonitor`, `core.hooksPath`, `core.alternateRefsCommand`;
  - filters, diff textconv or command, `diff.external`, merge drivers;
  - `include`, `credential`, `gpg.*`, `log.showSignature`;
  - `core.worktree`, `core.bare = true`, `extensions.worktreeConfig`.

  A clone with a `config.worktree` file is kept too. Such a clone is never
  inspected further.
- **No work lives only in its `.git`.** These keep it:
  - an operation in progress or left over (`rebase-apply`, `rebase-merge`,
    `sequencer`, `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`,
    `BISECT_LOG`);
  - a hook script of its own (any file in `.git/hooks` not ending in
    `.sample`);
  - `criterion` measurements under its `rust/target`.
- **No other checkout borrows its objects.**
  - A checkout made with `git clone --shared` or `--reference` names this
    clone's object dir in its `objects/info/alternates`. Every registered
    worktree and every child of every configured root is read for such a
    file, and a match keeps the clone.
  - A borrower outside the configured roots cannot be seen.
  - The objects such a borrower loses are then all commits already on
    `origin/master`, so it can be repaired by pointing its alternates at
    the main repository.

The home dir may be a clone root for the same reason as a target root.

### Scratch rule (list only)

**The scratch class is list only**, everywhere, with or without `--apply`
and with or without a marker (conductor's ruling of 16 November 2026).
Nothing under a scratch root is ever removed by this script.

**Why list only:** two reviews in a row found ways for an earlier removal
rule to delete data that existed nowhere else:
- a folder moved in under a name an earlier run had seen inherited the old
  "first seen" time;
- the temp dir under another spelling (a WSL `/mnt/c/...` path, macOS
  `/tmp`, a redirected `TEMP`) escaped "temp is list only".

Build trees are where the space is. Removal may come back later as its own
change for the disposable Windows dev VM only, designed around folder
identity rather than path.

**The listing:**
- **Opting in:** a scratch root is listed only when it holds a
  `.pseudolife-scratch-root` file its user created on purpose: a regular
  file, not a link, owned by the user on Linux and macOS. Without it the run
  refuses and says how to opt in.
- **What each row shows:** each direct child's size, when it was last
  written (arrival times counted), and which keep rule holds it, measured
  before any label. A row where no rule holds it reads `list only; nothing
  keeps it`.
- **Overlapping roots:** nothing at, under or around a scratch root is a
  candidate for the worktree, cache, target or clone class either, so
  naming the same dir under two roots removes nothing there. A registered
  worktree in a scratch root is listed, with the rule that keeps it.
- **A nested marker** inside a child changes nothing; only the root's own
  counts.

**The keep rules, shown as labels:**
- **Idle:** not idle for `--scratch-grace-days` (default 14). Idle means no
  file or dir under it written, created or moved in; the creation time on
  Windows and the change time on POSIX count.
- **Bytes:** holds no bytes (it may be a lock dir).
- **Checkouts and repositories:** a `.git`; a git repository (a dir
  holding `HEAD`, `objects` and `refs`); a `pyvenv.cfg`; a
  `.pseudolife-keep`.
- **Logs and evidence:** a `*.log` or `*.jsonl` file; anything named for
  evidence or logs (`*evidence*`, `logs`, `*-logs`), itself included.
- **Results, databases, models and programs:** `*.json`, `*.csv`, `*.tsv`,
  `*.parquet`, `*.db`, `*.sqlite*`, `*.gguf`, `*.safetensors`, `*.onnx`,
  `*.pt`, `*.pth`, `*.ckpt`, `*.exe`, `*.dll`, `*.so`, `*.dylib`, an
  executable file on Linux and macOS, a `PG_VERSION`.
- **Disk images, archives and backups, bundles, patches and keys:**
  `*.vhdx`, `*.vhd`, `*.img`, `*.iso`, `*.qcow2`, `*.vmdk`, `*.vdi`, `*.ova`,
  `*.tar`, `*.tgz`, `*.gz`, `*.xz`, `*.zst`, `*.bz2`, `*.zip`, `*.7z`,
  `*.rar`, `*.bak`, `*.dump`, `*.sql`, `*.bundle`, `*.patch`, `*.diff`,
  `*.pem`, `*.key`, `*.pfx`, `*.p12`, `*.kdbx`, `id_rsa`, `id_ed25519`,
  `id_ecdsa`, `id_dsa`.
- **Special files:** a socket, pipe or device, and another user's file.
- **Names:**
  - a name starting with `claude`, `codex` or `t3`;
  - `bin`, `scripts` or `tools`;
  - a hidden dir;
  - a name ending in a dot or space on Windows;
  - a rustup or cargo home.
- **Worktrees:** a registered worktree inside or around it.

Every period, in the config and on the command line, must be a finite
number, 0 or more.

### Deviations from the approved design (for the maintainer)

- **The merged rule's part (b), "the checkout did work of its own", and the
  `gh` merged-PR lookup are not implemented.** Every commit must already be
  on `origin/master` and the checkout clean and idle, so nothing unique can
  be lost without them. Part (b) would also have kept every merged review
  checkout, whose HEAD is its creation commit, for ever. A fresh checkout
  is protected by the grace period.
- **The scratch class is list only** (conductor's ruling of 16 November
  2026); the approved removal of idle scratch is not built.
- **On Windows the cargo build lock is not held across a target's
  delete;** the rename aside, which fails while a file in the tree is open,
  stands in for it.
- **A half-deleted aside dir is not finished by the next run;** it is
  reported and left for a person.
- **A dry run starts nothing:** with no sightings there is no grace clock
  to start, so the PC's week of dry runs shows what would go without
  changing when it goes.

### Never touched

The script touches nothing outside a configured root. It never touches
harness-managed checkouts (`.claude/worktrees`, `.t3/worktrees`,
`.codex/worktrees`) or temp dirs, even when one is configured as a root.
A temp dir named as a marked scratch root is listed and nothing more, as is
every scratch root. The report only counts what it left alone there.

## Configuration

Roots come from flags or from a private file, and flags win. The flags are
`--repo`, `--worktree-root`, `--cache-root`, `--target-root`, `--clone-root`
and `--scratch-root`; every root flag is repeatable. The tree ships no
machine-specific defaults. With no roots configured, a sweep prints how to
configure them and exits 0 without touching anything.

`~/.pseudolife-mcp/retention.toml`:

```toml
repo = "<main checkout>"
worktree_roots = ["<dir holding your worktrees>"]
cache_roots = ["<dir holding one CARGO_TARGET_DIR slice per branch>"]
target_roots = ["<dir holding cargo target dirs, judged by owner>"]
clone_roots = ["<dir holding standalone clones>"]
scratch_roots = ["<dir holding scratch dirs>"]
worktree_grace_days = 3
cache_grace_days = 7
target_grace_hours = 24
unowned_target_days = 14
scratch_grace_days = 14
```

A dir listed under both a cache root and a target root keeps the cache rule.
Use one or the other for a given root.

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
failing check. Totals follow, counting removed, partial, unknown, failed
and kept rows, with the reasons rows were not removed. The exit code is 0
on success, including when there is nothing to do and when candidates are
refused one by one. It is 1 when any row ends in one of these:

- an owner-mode refusal;
- a failed `git worktree remove`;
- a **partial** removal: git unregistered the worktree but could not
  delete every file (one held open on Windows), and the first leftover is
  named;
- an **unknown** outcome: git's worktree list could not be read after the
  removal, so the script does not guess. Owner mode prints `unknown:`; a
  sweep shows `unknown` in the verdict column;
- a row whose git call failed or timed out. In a sweep that fails only
  that row, and every row is still printed;
- a cache deleted only in part (the rest stays in a `.<name>.retention-*`
  dir beside it, named in the output);
- a failed `git worktree prune`, or a ledger that could not be written.

It is 2 for a refused run (failed fetch, bad repo or bad config). Config
values are type-checked: roots must be lists of paths. A
`.cargo/config.toml` that is not UTF-8 (PowerShell 5's `>` writes UTF-16)
keeps every cache, since its pin cannot be read, and names each file to
re-save. In a sweep, worktrees are unaffected. In owner mode the cache
check fails, so the whole run refuses; to remove only the worktree, rerun
with no `--cache-dir` and with `CARGO_TARGET_DIR` unset.

Every removal appends one JSON line to
`~/.pseudolife-mcp/retention-ledger.jsonl`. The line holds `time`, `mode`
(`sweep` or `owner`), `kind`, `path`, `bytes`, `newest_mtime`, `head` and
`branch` (worktrees), the `checks` passed and `complete`. `complete` is
true only for a confirmed full removal. Otherwise `state` says why:
`partial` (files were left behind) or `unknown` (the outcome could not be
confirmed). That is the path, size and reason the disk rule asks for. A
dry run deletes nothing and writes no ledger line; every run, dry or not,
takes the run lock file beside the ledger.

**Before the PC's week of dry runs:** give the new roots (`--target-root`,
`--clone-root`, `--scratch-root`) as flags, not in `retention.toml`. The
weekly Scheduled Task below runs with `--apply` and the roots in that file,
so roots written there are applied at its next run.

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
