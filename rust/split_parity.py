"""Keep the behaviour parity register as one file per table row.

The register lives under ``parity/``: one directory per ``##`` section
(``###`` sections nest inside their parent), whose ``_section.md`` holds the
heading, the prose and each table's header and separator lines, in order,
and one file per table row, stored under its own table's header and
separator lines. Two pull requests that change different rows, or different
paragraphs, then touch different files or lines apart. ``PARITY.md`` is a
pointer. Tools read the whole register through ``register_text()``.

A branch written against the single-file register forward-merges by keeping
its own ``PARITY.md`` during ``git merge`` and running this script: it
splits that file and the merge base's the same way and applies only the
branch's own changes, file by file and three-way (``git merge-file``)
against the directory, so rows master changed since stay as master has
them, then restores the pointer. A file both sides changed on the same
lines is written with conflict markers and the script exits 1. On the
pointer it changes nothing. Without a merge base it refuses (that would
overwrite master's rows with the branch's copies), except ``--full`` into an
empty directory, the one-time conversion.

    python rust/split_parity.py            # during git merge: apply the branch's changes
    python rust/split_parity.py --check    # verify the layout only
    python rust/split_parity.py --render   # print the whole register
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "PARITY.md"
ROOT_DIR = HERE / "parity"
LEDGER_GIT_PATH = "rust/PARITY.md"
SECTION_FILE = "_section.md"
TITLE = "# Behaviour parity register\n"
# Names are a row's key cells cut to their opening words, so editing the
# rest of a row keeps its file (two edits of one row then conflict in git,
# never pass as two rows). No section or row collided at these limits
# (2026-10-29, 42 sections, 520 rows), and the deepest path stays near
# 120 characters, inside Windows MAX_PATH from a typical worktree root.
SECTION_LIMIT = 32
ENTRY_LIMIT = 40
# Header first cells whose column repeats (the Console checklist's methods,
# the measurement table's platforms): the key takes the next cell too.
TWO_CELL_KEYS = ("Method", "Platform")
TABLE_SEPARATOR = re.compile(r"\|(\s*:?-+:?\s*\|)+")
CONFLICT = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.M)
OURS_LABEL, BASE_LABEL, THEIRS_LABEL = "PARITY.md", "merge base", "parity/"

STUB = TITLE + """
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
"""


class LedgerError(Exception):
    pass


# ---- names --------------------------------------------------------------------------

def slug(text: str, limit: int = ENTRY_LIMIT) -> str:
    """The file or directory name for a row key or (``SECTION_LIMIT``) a
    section heading: its opening words, lowercased and hyphenated."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not base:
        raise LedgerError(f"{text!r} has no letters or digits to name its file")
    if len(base) <= limit:
        return base
    head = base[:limit + 1]
    return head.rsplit("-", 1)[0] if "-" in head else base[:limit]


def cells(row: str) -> list[str]:
    """A table row's cells; a ``|`` inside backticks or escaped is text."""
    out, tick, start, i = [], False, 1, 1
    while i < len(row):
        ch = row[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            tick = not tick
        elif ch == "|" and not tick:
            out.append(row[start:i].strip())
            start = i + 1
        i += 1
    if not out:
        raise LedgerError(f"table row without a closing cell: {row[:80]!r}")
    return out


def row_key(header: str, row: str) -> str:
    """The identity of a row: its key cells (a leading bold label stands for
    the first cell)."""
    width = 2 if cells(header)[0] in TWO_CELL_KEYS else 1
    parts = cells(row)[:width]
    label = re.match(r"\*\*(.+?)\*\*", parts[0])
    if label:
        parts[0] = label.group(1).strip().rstrip(":").strip()
    return " ".join(parts)


def row_name(header: str, row: str) -> str:
    return slug(row_key(header, row))


# ---- the single-file form -------------------------------------------------------------

def blocks(lines: list[str]) -> list[list[str]]:
    out, cur = [], []
    for line in lines:
        if line.strip():
            cur.append(line)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def is_header(block: list[str]) -> bool:
    return len(block) >= 2 and block[0].startswith("|") and bool(TABLE_SEPARATOR.fullmatch(block[1].strip()))


def add_row(section: dict, head: str, sep: str, row: str, where: str) -> None:
    """Add a row under its file name. A row repeated verbatim is one row; two
    different rows one name would hold are refused."""
    name = row_name(head, row)
    text = f"{head}\n{sep}\n{row}\n"
    if section["entries"].get(name, text) != text:
        raise LedgerError(f"{where}: two rows would be {name}.md; start one with other words")
    section["entries"][name] = text


def parse_section_body(path: tuple[str, ...], body: list[str], ledger: dict) -> None:
    section = ledger.setdefault(path, {"blocks": [], "entries": {}})
    where = " / ".join(path)
    last_table = None
    for block in blocks(body):
        if not block[0].startswith("|"):
            section["blocks"].append("\n".join(block))
            last_table = None
            continue
        if not all(line.startswith("|") for line in block):
            raise LedgerError(f"{where}: malformed table starting {block[0][:60]!r}")
        if is_header(block):
            head, sep, rows = block[0], block[1], block[2:]
            if f"{head}\n{sep}" in section["blocks"]:
                raise LedgerError(f"{where}: two tables share a header ({head[:60]!r}); "
                                  "join them or tell their headers apart")
            section["blocks"].append(f"{head}\n{sep}")
            last_table = (head, sep)
        elif last_table is not None:
            # Rows a blank line cut from the table above: they are its rows.
            (head, sep), rows = last_table, block
        else:
            raise LedgerError(f"{where}: table rows without a header: {block[0][:60]!r}")
        for row in rows:
            add_row(section, head, sep, row, where)


def parse_ledger(text: str) -> dict:
    """{section path: {"blocks": [prose or table-header blocks], "entries":
    {file name: file text}}}; the title before the first ``##`` belongs to the
    pointer and is dropped."""
    text = text.replace("\r\n", "\n")
    if CONFLICT.search(text):
        raise LedgerError("PARITY.md holds conflict markers: keep this branch's version "
                          "first (git checkout --ours rust/PARITY.md), then rerun")
    ledger: dict = {}
    path: tuple[str, ...] | None = None
    body: list[str] = []
    for line in text.split("\n"):
        heading = re.match(r"(#{2,})\s+(.*\S)\s*$", line)
        if heading:
            if path is not None:
                parse_section_body(path, body, ledger)
            level, title = len(heading.group(1)), heading.group(2)
            if level == 2:
                path = (title,)
            elif level == 3 and path is not None:
                path = (path[0], title)
            else:
                raise LedgerError(f"unsupported heading {line!r}")
            body = []
        elif path is not None:
            body.append(line)
    if path is not None:
        parse_section_body(path, body, ledger)
    dirs = [section_parts(p) for p in ledger]
    if len(set(dirs)) != len(dirs):
        raise LedgerError("two section headings would share a directory; reword one's opening words")
    return ledger


# ---- the directory form -----------------------------------------------------------------

def section_parts(path: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(slug(part, SECTION_LIMIT) for part in path)


def section_text(path: tuple[str, ...], section_blocks: list[str]) -> str:
    text = f"{'#' * (len(path) + 1)} {path[-1]}\n"
    return text + "".join(f"\n{block}\n" for block in section_blocks)


def files(ledger: dict) -> dict[str, str]:
    """The directory form as {path under parity/: text}."""
    out = {}
    for path, section in ledger.items():
        directory = "/".join(section_parts(path))
        out[f"{directory}/{SECTION_FILE}"] = section_text(path, section["blocks"])
        for name, text in section["entries"].items():
            out[f"{directory}/{name}.md"] = text
    return out


def read_section_file(file: Path, depth: int) -> tuple[str, list[str]]:
    text = file.read_text(encoding="utf-8")
    if CONFLICT.search(text):
        raise LedgerError(f"{file}: unresolved conflict markers")
    lines = text.split("\n")
    heading = re.fullmatch(r"(#{2,}) (.*\S)", lines[0])
    if not heading or len(heading.group(1)) != depth + 1:
        raise LedgerError(f"{file}: the first line must be a level-{depth + 1} heading")
    section_blocks = []
    for block in blocks(lines[1:]):
        if block[0].startswith("|") and (len(block) != 2 or not is_header(block)):
            raise LedgerError(f"{file}: a table here is its header and separator lines only; "
                              "each row is its own file")
        joined = "\n".join(block)
        if block[0].startswith("|") and joined in section_blocks:
            raise LedgerError(f"{file}: two tables share a header ({block[0][:60]!r})")
        section_blocks.append(joined)
    return heading.group(2), section_blocks


def read_entry(item: Path, section_blocks: list[str]) -> tuple[str, str]:
    text = item.read_text(encoding="utf-8")
    if CONFLICT.search(text):
        raise LedgerError(f"{item}: unresolved conflict markers")
    lines = text.split("\n")
    if (len(lines) != 4 or lines[3] or not all(line.startswith("|") for line in lines[:3])
            or not is_header(lines[:2]) or TABLE_SEPARATOR.fullmatch(lines[2].strip())):
        raise LedgerError(f"{item}: an entry file holds exactly one row under its table's header "
                          "and separator lines, ending in one newline")
    head, sep, row = lines[:3]
    if f"{head}\n{sep}" not in section_blocks:
        raise LedgerError(f"{item}: its header is not a table header in {SECTION_FILE}; "
                          "copy the table's header and separator lines exactly")
    name = row_name(head, row)
    if item.stem != name:
        raise LedgerError(f"{item}: this row belongs in {name}.md")
    return name, text


def load_dir(root: Path | None = None) -> dict:
    """The directory as parse_ledger's structure, checking every name."""
    root = ROOT_DIR if root is None else root
    ledger: dict = {}
    if not root.is_dir():
        return ledger

    def walk(directory: Path, parent: tuple[str, ...]) -> None:
        marker = directory / SECTION_FILE
        if not marker.is_file():
            raise LedgerError(f"{directory}: a section directory needs {SECTION_FILE}")
        title, section_blocks = read_section_file(marker, len(parent) + 1)
        path = parent + (title,)
        if slug(title, SECTION_LIMIT) != directory.name:
            raise LedgerError(f"{directory}: heading {title!r} belongs in "
                              f"{slug(title, SECTION_LIMIT)!r}")
        section = ledger.setdefault(path, {"blocks": section_blocks, "entries": {}})
        for item in sorted(directory.iterdir()):
            if item.is_dir():
                if len(path) >= 2:
                    raise LedgerError(f"{item}: sections nest one level only")
                walk(item, path)
            elif item.name != SECTION_FILE:
                if item.suffix != ".md":
                    raise LedgerError(f"{item}: entries are .md files")
                name, text = read_entry(item, section_blocks)
                section["entries"][name] = text

    for top in sorted(root.iterdir()):
        if not top.is_dir():
            raise LedgerError(f"{top}: only section directories belong in {root.name}/")
        walk(top, ())
    return ledger


def render(ledger: dict) -> str:
    """The whole register as one document: sections by directory name, each
    table's rows by file name under its header."""
    out = [TITLE]
    for path in sorted(ledger, key=section_parts):
        section = ledger[path]
        out.append(f"\n{'#' * (len(path) + 1)} {path[-1]}\n")
        for block in section["blocks"]:
            out.append(f"\n{block}\n")
            if block.startswith("|"):
                prefix = block + "\n"
                out.extend(text[len(prefix):] for _, text in sorted(section["entries"].items())
                           if text.startswith(prefix))
    return "".join(out)


def register_text() -> str:
    """The register as one document, for tools that read it."""
    return render(load_dir())


# ---- conversion ---------------------------------------------------------------------------

def git(*args: str) -> str | None:
    done = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True,
                          encoding="utf-8")
    return done.stdout if done.returncode == 0 else None


def merge_base() -> str | None:
    """The merge base during a merge of master into a branch, else None
    (a rebase or cherry-pick has no MERGE_HEAD, so it gets no base)."""
    if git("rev-parse", "-q", "--verify", "MERGE_HEAD") is None:
        return None
    base = git("merge-base", "HEAD", "MERGE_HEAD")
    return base.strip() if base else None


def base_ledger(rev: str) -> dict | None:
    text = git("show", f"{rev}:{LEDGER_GIT_PATH}")
    if text is None:
        raise LedgerError(f"cannot read {LEDGER_GIT_PATH} at {rev}")
    ledger = parse_ledger(text)
    return ledger or None


def conflict_text(ours: str | None, theirs: str | None) -> str:
    return (f"<<<<<<< {OURS_LABEL}\n" + (ours or f"(deleted in {OURS_LABEL})\n")
            + "=======\n" + (theirs or f"(deleted in {THEIRS_LABEL})\n") + f">>>>>>> {THEIRS_LABEL}\n")


def merge3(ours: str, base: str, theirs: str) -> tuple[str, bool]:
    """git's own three-way line merge of one file; (text, clean)."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for label, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = Path(tmp) / label
            path.write_text(text, encoding="utf-8", newline="\n")
            paths.append(str(path))
        done = subprocess.run(["git", "merge-file", "-p", "-L", OURS_LABEL, "-L", BASE_LABEL,
                               "-L", THEIRS_LABEL, *paths], capture_output=True)
    if done.returncode < 0 or done.returncode > 127:
        raise LedgerError(f"git merge-file failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout.decode("utf-8").replace("\r\n", "\n"), done.returncode == 0


def disk_files(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(root.rglob("*.md")) if p.is_file()}


def write(file: Path, text: str | None) -> None:
    if text is None:
        file.unlink(missing_ok=True)
    else:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8", newline="\n")


def convert(ours: dict, base: dict | None, root: Path | None = None) -> list[str]:
    """Write ``ours`` into the directory. With a base, apply only what
    differs from it, three-way against the directory, file by file; without
    one, the directory must be empty. Returns the files left with conflict
    markers."""
    root = ROOT_DIR if root is None else root
    have_files = disk_files(root)
    if base is None and have_files:
        raise LedgerError(
            "parity/ already holds the register and no merge base is known, so converting "
            "PARITY.md would overwrite master's rows with this branch's copies. "
            "To forward-merge a branch, run this during `git merge` (not a rebase), or pass "
            "--base <the commit this branch's PARITY.md was written against>. "
            "To change a row, edit or add its file under parity/ and restore the pointer: "
            "git checkout origin/master -- rust/PARITY.md")
    want_files, was_files = files(ours), files(base or {})
    conflicts: list[str] = []
    for rel in list(want_files) + [r for r in was_files if r not in want_files]:
        want, was, have = want_files.get(rel), was_files.get(rel), have_files.get(rel)
        file = root / rel
        if base is not None and want == was:
            continue                           # unchanged here: master's copy stands
        if base is None or have == was:
            write(file, want)
        elif have == want:
            continue
        elif None not in (want, was, have):
            merged, clean = merge3(want, was, have)
            write(file, merged)
            if not clean:
                conflicts.append(f"{root.name}/{rel}")
        else:
            write(file, conflict_text(want, have))
            conflicts.append(f"{root.name}/{rel}")
    # Directories the branch emptied go; one left with rows but no section
    # file (master removed the section the branch changed) is a conflict.
    if root.is_dir():
        for directory in sorted((d for d in root.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
            left = {p.name for p in directory.iterdir()}
            if not left:
                directory.rmdir()
            elif SECTION_FILE not in left:
                rel = f"{directory.relative_to(root).as_posix()}/{SECTION_FILE}"
                write(directory / SECTION_FILE, conflict_text(want_files.get(rel), None))
                conflicts.append(f"{root.name}/{rel}")
    return conflicts


def check(root: Path | None = None, ledger: Path | None = None) -> list[str]:
    root = ROOT_DIR if root is None else root
    ledger = LEDGER if ledger is None else ledger
    problems = []
    text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
    if text.replace("\r\n", "\n") != STUB:
        problems.append(f"{ledger.name} is not the pointer: run python rust/split_parity.py")
    try:
        if not load_dir(root):
            problems.append(f"{root.name}/ holds no sections")
    except LedgerError as exc:
        problems.append(str(exc))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify the layout; change nothing")
    mode.add_argument("--render", action="store_true", help="print the whole register as one document")
    ap.add_argument("--base", help="the revision the branch's PARITY.md was written against "
                                   "(default: the merge base during a merge; none otherwise)")
    ap.add_argument("--full", action="store_true",
                    help="convert with no base into an empty parity/ (the one-time split)")
    args = ap.parse_args(argv)
    try:
        if args.render:
            sys.stdout.write(render(load_dir(ROOT_DIR)))
            return 0
        if not args.check:
            ours = parse_ledger(LEDGER.read_text(encoding="utf-8")) if LEDGER.is_file() else {}
            if ours:
                rev = args.base or (None if args.full else merge_base())
                base = base_ledger(rev) if rev else None
                if base is None and not args.full:
                    raise LedgerError(
                        "PARITY.md holds sections but there is nothing to diff them against: "
                        + ("the merge base already has the pointer, so this branch's sections are "
                           "new. Add each row as a file under parity/ instead, then restore the "
                           "pointer: git checkout origin/master -- rust/PARITY.md"
                           if rev else
                           "run this during `git merge` of master (keep this branch's "
                           "PARITY.md first), or pass --base <the commit it was written "
                           "against>; --full is only for the one-time conversion into an empty "
                           "parity/"))
                conflicts = convert(ours, base, ROOT_DIR)
                rows = sum(len(s["entries"]) for s in ours.values())
                print(f"converted {len(ours)} sections, {rows} rows"
                      + (f" (applying changes since {rev[:12]})" if base else ""))
                if conflicts:
                    LEDGER.write_text(STUB, encoding="utf-8", newline="\n")
                    print("both sides changed these; resolve the conflict markers by hand:")
                    for name in conflicts:
                        print(f"  {name}")
                    return 1
            LEDGER.write_text(STUB, encoding="utf-8", newline="\n")
        problems = check(ROOT_DIR, LEDGER)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    except LedgerError as exc:
        print(f"split_parity: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
