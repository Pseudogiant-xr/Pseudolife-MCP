## Backup explicit-DSN and file mode (CLI-BACKUP-DSN)

The native backup leaf (`shim/src/cli/backup/`) implements `backup_cli.py`
for the explicit `PSEUDOLIFE_MCP_DATABASE_URL` and for file mode (no DSN and
no embedded bank). The command accepts argparse inline and separate values,
repeated options (last value wins), unambiguous abbreviations, Python float
spellings, and pathlib paths including parent components. Help follows the
terminal width selected by `COLUMNS`. A missing data dir is refused using
pathlib's spelling before anything else.

Contract, checked per answered case by `python rust/cli_harness --row backup`
against the live Python oracle on disposable homes and a disposable bank, on
Windows and on Linux (the POSIX symlink member case runs on Linux only):
exit code; both streams byte-exact apart from the run timestamp in the two
new file names (validated against the arm's own clock window; two files that
normalize to one name stay distinct); every file under the home; the state
archive read back member by member (name, type, mode, uid, gid, mtime as a
float, size, link target, device major/minor, content hash; uname and gname on
both OSes); Unix owner names are checked against NSS before becoming portable
golden tokens. The dump is decompressed, with pg_dump's per-run `\restrict` key normalized only
when the `\restrict` and `\unrestrict` keys agree; and the source bank's rows
and catalog unchanged. Free: tar header layout, PAX record layout, gzip
headers and compression bytes. pg_dump is found as Python 3.11 finds it: the
newest `~/.pg0` bundle, then `shutil.which` (PATH split as text with quoted
and empty entries kept, `None` for an empty PATH, Windows' current-directory
rule and `PATHEXT`, macOS `CS_PATH`). Each member copies exactly its stat
size; a file that shrinks while archived fails the backup. A closed stdout
exits 120, as CPython's shutdown flush does (declared substitution
`backup-stdout-closed`: no interpreter trailer). Discovery errors retain the
exact final OS exception line, exit 1, and creation of an empty output dir;
`backup-discovery-traceback` removes only the preceding Python frame block.
Its adversarial test preserves differences in class, errno, message, path,
exit code and filesystem effects.

The member-kind case declares `backup-restoration`: both arms must report a
Python-extracted archive with no missing or mismatched members, preserved
hardlink identity and omitted sockets. Restoration observations are compared
without substitution; matching failures in both arms still fail the gate.

Deferred by name, before any effect: anything that could need the lite tier's
embedded instance (no DSN and `<data>/embedded_pg/PG_VERSION` present, or no
`--data-dir`/`PSEUDOLIFE_MCP_DATA_DIR` while the lite default dir exists);
an output dir that resolves (symlinks and junctions followed) to the data dir or below
one of its archived children; Windows output aliases whose ancestor identities
match the source root or an archived directory; non-UTF-8 PostgreSQL discovery
paths (installation and PATH); a numeric `COLUMNS` with more than 4300 digits;
a top-level child `Path.resolve()` would refuse (a symlink loop); directory
reparse points that escape the source tree or form a traversal cycle;
Windows directory symlinks without an existing absolute directory target
shorter than 260 UTF-16 units, or file symlinks to directories (Python extraction
infers rather than preserves their directory flag); Windows names or link
targets that Python's UTF-8/surrogateescape archive writer cannot round-trip;
raw matching rotation names that cannot be printed through UTF-8 stdout;
and on Windows an existing target with this second's timestamp.

Members include hard links, POSIX FIFOs and device nodes, raw POSIX filename
and link-target bytes, Windows symlinks and internal directory junctions.
Sockets are omitted, as Python's tarfile omits them. PAX `hdrcharset=BINARY`
keeps raw names, link targets and NSS owner names lossless, and member order
follows Python's Unicode code points including filesystem surrogateescape.
The member round-trip case extracts each arm's actual archive with Python
and checks kind, mode, numeric ownership, content, link targets and hard-link
identity. Linux device headers are checked against `/dev/null`; extraction
requires mknod permission in CI and reports an unverified result when that
permission is unavailable locally. Windows timestamps use the CRT's TZ
interpretation; the Windows rotation clock and float conversion match CPython 3.11.
Rotation compares UTC epoch times and uses Python's Unicode
case-insensitive glob matching, allowing unrelated Unicode filenames.
Windows TZ, symlink, junction, Unicode rotation and UNC fixtures require
fresh CI after the merge resolution.

The Windows zero-retention archive-creation case has a named H5 selection
exception for live and replay (including recording). CPython 3.11.9 with its
0.015625-second clock kept/deleted 254/46 of 300 freshly written 64-byte
archive-named files, and 229/71 of 300 1-MiB files. Sequential live arms and
record/replay processes can therefore disagree despite identical semantics.
Linux keeps the file-mode zero-retention case in all modes. Both OSes compare
zero-option grammar/help; native old/equal/new mtime boundary tests and the
mandatory Windows same-tick Python/native clock probe cover retention.
The comparator is unchanged.

Declared divergences: Python tracebacks after an effect (permissions, a full
disk, a pg_dump that cannot start)
become `backup failed: <error>`; output encodings other than UTF-8, as for
every CLI leaf.
