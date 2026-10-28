"""Keep the declared-divergence ledger as one file per entry.

The ledger lives under ``divergences/``: one directory per ``##`` section
(``###`` sections nest inside their parent), whose ``_section.md`` holds the
heading and the section's prose, and one file per entry. An entry is a data
row of a table, stored under its own table's header and separator lines, or
a paragraph that opens with a bold label (``**MCP-REFUSAL-CLEANUP**``). Two
pull requests that add or close different entries then touch different
files. ``divergences.md`` is a pointer.

A branch written against the single-file ledger forward-merges by keeping
its own ``divergences.md`` during ``git merge`` and running this script: it
diffs that file against the merge base's and applies only the branch's own
changes, entry by entry, so entries master changed or closed since stay as
master has them, then restores the pointer. An entry or section heading both
sides changed is written with conflict markers and the script exits 1. On
the pointer it changes nothing. Without a merge base it refuses (that would
overwrite master's entries with the branch's copies), except ``--full``
into an empty directory, the one-time conversion.

    python rust/daemon/split_divergences.py            # during git merge: apply the branch's changes
    python rust/daemon/split_divergences.py --check    # verify the layout only
    python rust/daemon/split_divergences.py --render   # print the whole ledger
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "divergences.md"
ROOT_DIR = HERE / "divergences"
LEDGER_GIT_PATH = "rust/daemon/divergences.md"
SECTION_FILE = "_section.md"
# Names are the opening words, so editing the rest of a row keeps its file
# (two edits of one row then conflict in git, never pass as two rows). These
# limits keep the deepest path near 135 characters, inside Windows MAX_PATH
# from a typical worktree root; no entry or section collided at 32 (2026-10-28,
# 238 entries).
SECTION_LIMIT = 32
ENTRY_LIMIT = 40
TABLE_SEPARATOR = re.compile(r"\|(\s*:?-+:?\s*\|)+")
CONFLICT = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.M)

STUB = """\
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
"""


class LedgerError(Exception):
    pass


# ---- names --------------------------------------------------------------------------

def slug(text: str, limit: int = ENTRY_LIMIT) -> str:
    """The file or directory name for an entry key or (``SECTION_LIMIT``) a
    section heading: its opening words, lowercased and hyphenated."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not base:
        raise LedgerError(f"{text!r} has no letters or digits to name its file")
    if len(base) <= limit:
        return base
    head = base[:limit + 1]
    return head.rsplit("-", 1)[0] if "-" in head else base[:limit]


def first_cell(row: str) -> str:
    """A table row's first cell; a ``|`` inside backticks or escaped is text."""
    tick = False
    i = 1
    while i < len(row):
        ch = row[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            tick = not tick
        elif ch == "|" and not tick:
            return row[1:i].strip()
        i += 1
    raise LedgerError(f"table row without a closing cell: {row[:80]!r}")


def entry_key(text: str) -> str:
    """The identity of an entry: its leading bold label, else its row's first
    cell (a table entry's text is header, separator, row)."""
    head = first_cell(text.rstrip("\n").split("\n")[-1]) if text.startswith("|") else text
    m = re.match(r"\*\*(.+?)\*\*", head)
    if m:
        return m.group(1).strip().rstrip(":").strip()
    if text.startswith("|"):
        return head
    raise LedgerError(f"a paragraph entry must open with a bold label: {text[:80]!r}")


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


def add_entry(section: dict, text: str, where: str) -> None:
    """Add an entry under its file name, refusing two entries one name holds."""
    name = slug(entry_key(text))
    if name in section["entries"]:
        raise LedgerError(f"{where}: two entries would be {name}.md; start one with other "
                          "words or a bold label (**NAME:**)")
    section["entries"][name] = text


def parse_section_body(path: tuple[str, ...], body: list[str], ledger: dict) -> None:
    section = ledger.setdefault(path, {"prose": [], "entries": {}})
    where = " / ".join(path)
    for block in blocks(body):
        if block[0].startswith("|"):
            if len(block) < 2 or not TABLE_SEPARATOR.fullmatch(block[1].strip()) \
                    or not all(line.startswith("|") for line in block):
                raise LedgerError(f"{where}: malformed table starting {block[0][:60]!r}")
            for row in block[2:]:
                text = f"{block[0]}\n{block[1]}\n{row}\n"
                add_entry(section, text, where)
        elif block[0].startswith("**"):
            text = "\n".join(block) + "\n"
            add_entry(section, text, where)
        else:
            section["prose"].append("\n".join(block))


def parse_ledger(text: str) -> dict:
    """{section path: {"prose": [blocks], "entries": {file name: file text}}};
    the preamble before the first ``##`` belongs to the pointer and is dropped."""
    if CONFLICT.search(text.replace("\r\n", "\n")):
        raise LedgerError("divergences.md holds conflict markers: keep this branch's version "
                          "first (git checkout --ours rust/daemon/divergences.md), then rerun")
    ledger: dict = {}
    path: tuple[str, ...] | None = None
    body: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
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
    dirs = [tuple(slug(part, SECTION_LIMIT) for part in p) for p in ledger]
    if len(set(dirs)) != len(dirs):
        raise LedgerError("two section headings would share a directory; reword one's opening words")
    return ledger


# ---- the directory form -----------------------------------------------------------------

def section_dir(path: tuple[str, ...], root: Path) -> Path:
    return root.joinpath(*(slug(part, SECTION_LIMIT) for part in path))


def section_text(path: tuple[str, ...], prose: list[str]) -> str:
    text = f"{'#' * (len(path) + 1)} {path[-1]}\n"
    return text + "".join(f"\n{block}\n" for block in prose)


def read_section_file(file: Path, depth: int) -> tuple[str, list[str]]:
    text = file.read_text(encoding="utf-8")
    if CONFLICT.search(text):
        raise LedgerError(f"{file}: unresolved conflict markers")
    lines = text.split("\n")
    heading = re.fullmatch(r"(#{2,}) (.*\S)", lines[0])
    if not heading or len(heading.group(1)) != depth + 1:
        raise LedgerError(f"{file}: the first line must be a level-{depth + 1} heading")
    return heading.group(2), ["\n".join(b) for b in blocks(lines[1:])]


def load_dir(root: Path = ROOT_DIR) -> dict:
    """The directory as parse_ledger's structure, checking every name."""
    ledger: dict = {}
    if not root.is_dir():
        return ledger

    def walk(directory: Path, parent: tuple[str, ...]) -> None:
        marker = directory / SECTION_FILE
        if not marker.is_file():
            raise LedgerError(f"{directory}: a section directory needs {SECTION_FILE}")
        title, prose = read_section_file(marker, len(parent) + 1)
        path = parent + (title,)
        if slug(title, SECTION_LIMIT) != directory.name:
            raise LedgerError(f"{directory}: heading {title!r} belongs in "
                              f"{slug(title, SECTION_LIMIT)!r}")
        section = ledger.setdefault(path, {"prose": prose, "entries": {}})
        for item in sorted(directory.iterdir()):
            if item.is_dir():
                if len(path) >= 2:
                    raise LedgerError(f"{item}: sections nest one level only")
                walk(item, path)
            elif item.name != SECTION_FILE:
                if item.suffix != ".md":
                    raise LedgerError(f"{item}: entries are .md files")
                text = item.read_text(encoding="utf-8")
                if CONFLICT.search(text):
                    raise LedgerError(f"{item}: unresolved conflict markers")
                parsed: dict = {}
                parse_section_body(path, text.split("\n"), parsed)
                got = parsed[path]
                if got["prose"] or len(got["entries"]) != 1:
                    raise LedgerError(f"{item}: an entry file holds exactly one row (under its "
                                      "table header) or one bold-labelled paragraph")
                (name, entry), = got["entries"].items()
                if entry != text:
                    raise LedgerError(f"{item}: not in canonical form (one trailing newline, "
                                      "no blank lines)")
                if item.stem != name:
                    raise LedgerError(f"{item}: this entry belongs in {name}.md")
                add_entry(section, text, " / ".join(path))

    for top in sorted(root.iterdir()):
        if not top.is_dir():
            raise LedgerError(f"{top}: only section directories belong in {root.name}/")
        walk(top, ())
    return ledger


def render(ledger: dict) -> str:
    """The whole ledger as one document: sections by name, entries by file
    name, consecutive rows under one table header joined into one table."""
    out = [STUB.split("\n\nThe rows live")[0] + "\n"]
    for path in sorted(ledger, key=lambda p: tuple(slug(x, SECTION_LIMIT) for x in p)):
        section = ledger[path]
        out.append(f"\n{'#' * (len(path) + 1)} {path[-1]}\n")
        out.extend(f"\n{block}\n" for block in section["prose"])
        header = None
        for name in sorted(section["entries"]):
            text = section["entries"][name]
            if text.startswith("|"):
                head, sep, row = text.rstrip("\n").split("\n")
                if header != (head, sep):
                    out.append(f"\n{head}\n{sep}\n")
                    header = (head, sep)
                out.append(row + "\n")
            else:
                header = None
                out.append("\n" + text)
    return "".join(out)


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
    return ("<<<<<<< this branch\n" + (ours or "(deleted on this branch)\n")
            + "=======\n" + (theirs or "(deleted on master)\n") + ">>>>>>> master\n")


def by_dir(ledger: dict) -> dict:
    """{directory name parts: (heading path, section)}: a section's identity is
    its directory, so a heading reworded past the name's cut is an edit."""
    return {tuple(slug(part, SECTION_LIMIT) for part in path): (path, section)
            for path, section in ledger.items()}


def heading_text(pair) -> str | None:
    return section_text(pair[0], pair[1]["prose"]) if pair else None


def convert(ours: dict, base: dict | None, root: Path = ROOT_DIR) -> list[str]:
    """Write ``ours`` into the directory. With a base, apply only what
    differs from it, three-way against the directory; without one, the
    directory must hold no entries yet. Returns the files written with
    conflict markers."""
    disk = load_dir(root)
    if base is None and any(section["entries"] for section in disk.values()):
        raise LedgerError(
            "divergences/ already holds the ledger and no merge base is known, so converting "
            "divergences.md would overwrite master's entries with this branch's copies. "
            "To forward-merge a branch, run this during `git merge` (not a rebase), or pass "
            "--base <the commit this branch's divergences.md was written against>. "
            "To add or close a divergence, add or delete its file under divergences/ and "
            "restore the pointer: git checkout origin/master -- rust/daemon/divergences.md")
    mine_by, old_by, now_by = by_dir(ours), by_dir(base or {}), by_dir(disk)
    conflicts: list[str] = []
    for key in list(mine_by) + [k for k in old_by if k not in mine_by]:
        mine, old, now = mine_by.get(key), old_by.get(key), now_by.get(key)
        directory = root.joinpath(*key)
        rel = directory.relative_to(root.parent).as_posix()
        marker = directory / SECTION_FILE
        want_head, was_head, have_head = heading_text(mine), heading_text(old), heading_text(now)
        if base is None or want_head != was_head:
            if base is not None and have_head not in (was_head, want_head):
                conflicts.append(f"{rel}/{SECTION_FILE}")
                directory.mkdir(parents=True, exist_ok=True)
                marker.write_text(conflict_text(want_head, have_head), encoding="utf-8", newline="\n")
            elif want_head is not None:
                directory.mkdir(parents=True, exist_ok=True)
                marker.write_text(want_head, encoding="utf-8", newline="\n")
        want_entries = mine[1]["entries"] if mine else {}
        was_entries = old[1]["entries"] if old else {}
        have_entries = now[1]["entries"] if now else {}
        for name in list(want_entries) + [n for n in was_entries if n not in want_entries]:
            want, was, have = want_entries.get(name), was_entries.get(name), have_entries.get(name)
            file = directory / f"{name}.md"
            if base is not None and want == was:
                continue                       # unchanged here: master's copy stands
            if base is not None and have not in (was, want):
                conflicts.append(f"{rel}/{file.name}")
                directory.mkdir(parents=True, exist_ok=True)
                file.write_text(conflict_text(want, have), encoding="utf-8", newline="\n")
            elif want is None:
                file.unlink(missing_ok=True)
            else:
                directory.mkdir(parents=True, exist_ok=True)
                file.write_text(want, encoding="utf-8", newline="\n")
        if directory.is_dir():
            left = {p.name for p in directory.iterdir()}
            if mine is None and left <= {SECTION_FILE} and f"{rel}/{SECTION_FILE}" not in conflicts:
                # The branch removed the section and nothing else is left in it.
                marker.unlink(missing_ok=True)
                directory.rmdir()
            elif SECTION_FILE not in left:
                # Master removed the section while the branch added to it.
                conflicts.append(f"{rel}/{SECTION_FILE}")
                marker.write_text(conflict_text(want_head, None), encoding="utf-8", newline="\n")
    return conflicts


def check(root: Path = ROOT_DIR, ledger: Path = LEDGER) -> list[str]:
    problems = []
    text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
    if text.replace("\r\n", "\n") != STUB:
        problems.append(f"{ledger.name} is not the pointer: run python rust/daemon/split_divergences.py")
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
    mode.add_argument("--render", action="store_true", help="print the whole ledger as one document")
    ap.add_argument("--base", help="the revision the branch's divergences.md was written against "
                                   "(default: the merge base during a merge; none otherwise)")
    ap.add_argument("--full", action="store_true",
                    help="convert with no base into an empty divergences/ (the one-time split)")
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
                        "divergences.md holds sections but there is nothing to diff them against: "
                        + ("the merge base already has the pointer, so this branch's sections are "
                           "new. Add each as a file under divergences/ instead, then restore the "
                           "pointer: git checkout origin/master -- rust/daemon/divergences.md"
                           if rev else
                           "run this during `git merge` of master (keep this branch's "
                           "divergences.md first), or pass --base <the commit it was written "
                           "against>; --full is only for the one-time conversion into an empty "
                           "divergences/"))
                conflicts = convert(ours, base, ROOT_DIR)
                rows = sum(len(s["entries"]) for s in ours.values())
                print(f"converted {len(ours)} sections, {rows} entries"
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
        print(f"split_divergences: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
