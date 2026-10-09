# CLI-TUNNEL-STATUS spec (native `tunnel`, read-only subset)

Oracle: `pseudolife_memory/tunnel_cli.py`, `tunnel_profiles.py`,
`tunnel_bridge.py`, `tunnel_runtime.py`, `credentials.py`,
`doctor_cli.py` at the branch base, run by CPython 3.11.9 (the interpreter
of every recorded run). Line numbers refer to that tree. Where CPython
versions disagree on an input (`datetime.fromisoformat`), the leaf defers
rather than pick one.

## Canonical argv

`tunnel <command>` then the command's own options in any order, each at most
once, each value as the next argument and never `-`-led
(`tunnel_cli.py:285-313`):

- `status [--json] [--profile N] [--profile-dir D]`
- `update [--profile N] [--profile-dir D]` (`--profile` is parsed and ignored)
- `verify [--profile N] [--profile-dir D]`
- `setup [--profile N] [--profile-dir D] [--daemon-url U] [--token-file F]
  [--tunnel-id T] [--organization-id O] [--catalog current|full]
  [--key-expires-at E] [--accept-access]`

Every other shape defers: other commands (`doctor`, `run`, `start`, `stop`,
`shim`, `service ...`), no command, `-h`/`--help`, abbreviations,
`--opt=value`, repeated options, `-`-led values, `--catalog` outside its
choices, `setup --read-key / --key-file / --open-browser / --start`, an empty
`--profile-dir`, non-Unicode argv. argparse usage and error text are not
reproduced (item 4 of the scope is deferred).

## Contract items

| Item | Oracle |
|---|---|
| `ProfileStore(args.profile_dir)` runs outside the error handler (a failure is a traceback): every failure to open the root defers | `tunnel_cli.py:318`, `tunnel_profiles.py:242-243` |
| Default root `~/.pseudolife-mcp/tunnel` (USERPROFILE / HOME) | `tunnel_profiles.py:237-238` |
| `checked_path`: abspath, no redirect among the ancestors (`tunnel path cannot be inspected safely`), no redirect at the leaf (`tunnel paths must not contain redirects`) | `tunnel_profiles.py:53-62`, `credentials.py:43-60` |
| `private_read`: owner-only regular file with one link (POSIX uid/mode, Windows protected owner DACL), `limit+1` read, `private tunnel file is too large`, otherwise `private tunnel file is unavailable or not owner-only` | `tunnel_profiles.py:154-168`, `credentials.py:237-248` |
| Profile load: JSON object, known fields only, three required fields, then `Profile.validate` in order (name, endpoint, token reference, tunnel id, organization id, state/consent types, ready needs consent and id, runtime version, catalog, key expiry, cloud reference), then the saved name equals the requested one; `tunnel profile is invalid` for the structural failures | `tunnel_profiles.py:211-234`, `270-281` |
| `validate_name` | `tunnel_profiles.py:27-32` |
| `validate_url(local=True)` messages | `tunnel_profiles.py:35-50` |
| `parse_expiry`: aware ISO timestamp, or a 10-character date at UTC midnight; anything else (naive included) refused | `tunnel_profiles.py:321-330` |
| Key presence: `read_key` succeeds (POSIX `PLAIN\0` + valid token; Windows `DPAPI\0`), any `TunnelError` is absent | `tunnel_cli.py:76-81`, `tunnel_profiles.py:299-311`, `credentials.py:251-260` |
| `key_expiry` dict and runtime class (`unknown` / `expired` / `near-expiry` under 7 days / `valid`) | `tunnel_cli.py:84-89`, `tunnel_profiles.py:333-339` |
| Runtime without a process record: `running=false, ready=false` | `tunnel_runtime.py:404-408` |
| Cloud without a process record: `identity()` raises inside `verification_status`'s guard, so `{verified: false, successful_calls: 0, challenge_current: false}`. The challenge record is read only as `_challenge` reads it, through `private_read` (a redirect, an unsafe, missing or oversized file is `{}`); a readable one with more than 100 brackets (near the recursion limit) defers | `tunnel_bridge.py:24-49`, `52-70`, `79-95` |
| Refresh outcome: `.reload.json` (pending / unavailable), else `.reload.result.json` (allowlisted state, `rolled_back is True`), else none; `str(id)` matched against `[a-f0-9]{32}` (a string, or an integer token of ASCII digits only; any float or signed token is never a match) | `tunnel_cli.py:210-233` |
| Status text lines and JSON key order (`json.dumps` default separators, ensure_ascii) | `tunnel_cli.py:179-207` |
| `update`: absent root or no profiles prints one line, exit 3; root `_prepare` checks (owner-only directory); `*.profile.json` glob (case-insensitive on Windows) with `validate_name` on each stem; profiles sorted; each loaded; pending (not ready, or ready without a key) summarized as JSON, exit 0 | `tunnel_cli.py:261-282`, `322-328`, `tunnel_profiles.py:245-257`, `283-287` |
| `verify`: load; unfinished (not ready, or no key) refused; ready with a key and no process record fails `identity()`'s private read before `begin_challenge` writes | `tunnel_cli.py:331-335`, `tunnel_bridge.py:24-49`, `75-78`, `tunnel_runtime.py:310-326` |
| `setup` refusals before `_verify_local`: profile name; an existing profile defers; `_discover` (explicit pair: URL validated first; environment block; registration blocks, whose absence is decided natively); then `Profile.validate` of the new profile | `tunnel_cli.py:41-62`, `99-123`, `doctor_cli.py:144-163` |
| Errors: `TunnelError` text on stderr, exit 2 | `tunnel_cli.py:371-375` |
| Streams: UTF-8, LF translated to CRLF on Windows | `cli.rs::text_bytes` |

## Deferred (before any effect)

- A `<name>.process.json` present (psutil live-process identity,
  `tunnel_runtime.py:387-416`).
- A Windows key with the `DPAPI` prefix (CryptUnprotectData would need a new
  unsafe module).
- `update` with a ready profile that has a key: `update_profile` first
  resolves `stable_command()`, which depends on the installed interpreter
  and launcher (`tunnel_runtime.py:946-954`); the result is `unchanged` or
  `failed` by host install, not by saved state.
- `service install` without `--accept-autostart`: the refusal follows
  `shim_command()` (`tunnel_cli.py:344`), so whether it or the launcher
  refusal prints depends on the host install. All of `service` defers.
- `setup` whose profile validates (next: `_verify_local`'s handshake), an
  existing profile, a present client registration file, `~`-led or
  drive-relative/UNC token paths.
- Python-only JSON (BOM/UTF-16/32, surrogates, NaN/Infinity, more than 100
  brackets, digit runs over 4000), and any object key that decodes to
  serde_json's reserved `$serde_json::private::Number` (arbitrary_precision
  would read that object as a number).
- Expiry spellings outside the decided set: basic format, other separators,
  a fraction of other than 3 or 6 digits (3.10 refuses, 3.11+ admits), hour
  24 (3.14 admits, 3.11 refuses), offset minutes 60+, years 1 and 9999 with
  an offset.
- A FIFO where a private file is read (Python's blocking open waits for a
  writer). URLs outside the decided set, non-ASCII
  runtime versions, UNC/device paths, dangling Windows junctions, a
  non-directory root, glob entries that are not regular files, non-ASCII
  names on Windows, a challenge record with more than 100 brackets.

## Free items

None: every byte of stdout, stderr and the home is compared.

## Harness

`python rust/cli_harness --row tunnel`. Seeds use the oracle's writers.
Rules: `tunnel-deferral` (deferral expectation), `tunnel-dpapi-key`
(per-arm DPAPI ciphertext, tokenized only after the oracle's `_dpapi`
unprotects it to the fixture key). No clock or uuid reaches any output: all
expiry and refresh ids are seeded constants (the near-expiry date is fixed
once per run), so none is tokenized. The DPAPI rule tokenizes a key file
only when its bytes equal that arm's own seeded bytes (recorded before the
arm ran), so a rewritten or re-encrypted key shows.

Guards, every arm:

- Before it runs, the child's environment is checked: `PATH` empty,
  `COMSPEC` and `PATHEXT` removed, and every program-directory variable
  (`HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, the `ProgramFiles`
  family, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, XDG dirs, the shim runtime and
  launcher overrides, `PYTHONHOME`, virtualenv/conda prefixes) inside the
  disposable home or absent. `SYSTEMROOT`/`WINDIR` and `TEMP`/`TMP` stay.
- Every daemon URL, saved in a seeded profile or passed as `--daemon-url`,
  is the arm's own loopback listener, which records every connection,
  including any still in its backlog after the arm exits. None may occur;
  the `tunnel-saved-url-connect` mutant (a connect to the saved URL) is
  caught by it.
- The caller's real `~/.pseudolife-mcp` is compared before and after the arm
  by `lstat` metadata only: names, size, mtime_ns, mode, inode, link count
  and Windows attributes. Directories are listed; no file is opened or
  read; links and junctions are recorded, never followed. Subtrees other
  live sessions write during any run (`digests`, `locks`, `suite-results`,
  `handshake-cache`, `agent-state`, `overnight`, `ledgers`) are not
  compared; everything else, `tunnel/` included, is.
