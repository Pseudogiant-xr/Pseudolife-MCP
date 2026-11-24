"""The release version's one reader and drift check (rust/ci/release_version.py)."""
import ast
import json
from pathlib import Path
import re
import subprocess

import pytest

import release_version as rv

ROOT = Path(__file__).resolve().parents[2]
LABELS = [row[0] for row in rv.FIELDS]


def copy_fields(root: Path) -> str:
    """Copy the authority and every version field of this checkout to
    ``root``; return the release version."""
    for path in {rv.AUTHORITY, *(row[1] for row in rv.FIELDS)}:
        if (ROOT / path).is_file():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes((ROOT / path).read_bytes())
    return rv.read(ROOT)


def _sub(path: str, pattern: str):
    """A mutation of ``path``: replace group 1 of the first ``pattern`` match."""
    def mutate(root: Path, new: str) -> None:
        file = root / path
        text = file.read_text(encoding="utf-8")
        match = re.search(pattern, text, re.M)
        assert match, (path, pattern)
        file.write_text(text[:match.start(1)] + new + text[match.end(1):], encoding="utf-8")
    return mutate


def _json(path: str, *keys):
    def mutate(root: Path, new: str) -> None:
        file = root / path
        data = json.loads(file.read_text(encoding="utf-8"))
        target = data
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = new
        file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return mutate


# One mutation per field, each written independently of the reader's own
# patterns, so a reader that stopped reading a field cannot agree with it.
MUTATE = {
    "pyproject.toml [project] version": _sub("pyproject.toml", r'\[project\][^\[]*?^version = "([^"]+)"'),
    "rust/daemon/Cargo.toml [package] version": _sub("rust/daemon/Cargo.toml", r'^version = "([^"]+)"'),
    "rust/Cargo.lock pseudolife-stdio": _sub("rust/Cargo.lock", r'^name = "pseudolife-stdio"\r?\nversion = "([^"]+)"'),
    "rust/Cargo.lock pseudolife-daemon": _sub("rust/Cargo.lock", r'^name = "pseudolife-daemon"\r?\nversion = "([^"]+)"'),
    "rust/shim/src/lifecycle.rs PACKAGE_VERSION": _sub("rust/shim/src/lifecycle.rs", r'PACKAGE_VERSION: &str = "([^"]+)"'),
    "server.json version": _json("server.json", "version"),
    "server.json packages[0].version": _json("server.json", "packages", 0, "version"),
    "plugin/release.json version": _json("plugin/release.json", "version"),
    "ops/docker-compose.yml daemon image tag": _sub("ops/docker-compose.yml", r"image:\s*pseudolife-daemon:(\S+)$"),
    "docs/atlas/atlas.json meta.version": _json("docs/atlas/atlas.json", "meta", "version"),
}


def test_every_field_has_a_mutation():
    assert sorted(MUTATE) == sorted(LABELS)


def test_this_checkout_is_consistent():
    assert rv.check(ROOT) == rv.read(ROOT)


@pytest.mark.parametrize("label", LABELS)
def test_drift_in_each_field_is_refused_by_name(tmp_path, label):
    value = copy_fields(tmp_path)
    assert rv.check(tmp_path) == value
    MUTATE[label](tmp_path, "9.9.9")
    with pytest.raises(rv.VersionError) as refused:
        rv.check(tmp_path)
    assert str(refused.value).startswith(f"{label} is 9.9.9 but the release version is {value} ")


def test_a_moved_authority_is_refused_at_the_first_field(tmp_path):
    copy_fields(tmp_path)
    _sub(rv.AUTHORITY, r'^version = "([^"]+)"')(tmp_path, "9.9.9")
    assert rv.read(tmp_path) == "9.9.9"
    with pytest.raises(rv.VersionError, match=re.escape(f"{LABELS[0]} is ")):
        rv.check(tmp_path)


def test_pyproject_is_checked_only_while_it_exists(tmp_path):
    value = copy_fields(tmp_path)
    (tmp_path / "pyproject.toml").unlink()
    assert rv.check(tmp_path) == value
    assert all(label != LABELS[0] for label, _ in rv.fields(tmp_path))


@pytest.mark.parametrize("path", sorted({row[1] for row in rv.FIELDS if row[3]}))
def test_a_missing_required_field_file_is_refused(tmp_path, path):
    copy_fields(tmp_path)
    (tmp_path / path).unlink()
    with pytest.raises(rv.VersionError, match="unreadable or missing"):
        rv.check(tmp_path)


@pytest.mark.parametrize("tag, ok", [
    ("v{}", True), ("{}", False), ("vv{}", False), ("v{}.1", False), ("V{}", False),
])
def test_the_tag_must_be_v_and_the_release_version(tmp_path, tag, ok):
    value = copy_fields(tmp_path)
    if ok:
        assert rv.check(tmp_path, tag.format(value)) == value
    else:
        with pytest.raises(rv.VersionError, match="does not match the release version"):
            rv.check(tmp_path, tag.format(value))


def test_table_string_reads_only_the_named_table():
    text = ('[workspace]\nversion = "0.0.1"\n[[bin]]\nname = "x"\nversion = "0.0.2"\n'
            '[package]  # the crate\nname = "pseudolife-stdio"\n'
            'rmcp = { version = "=3.5.0" }\nversion = "1.2.3" # cut\nversion = "4.5.6"\n'
            '[dependencies]\nversion = "0.0.3"\n')
    assert rv.table_string(text, "package", "version") == "1.2.3"
    assert rv.table_string(text, "workspace", "version") == "0.0.1"
    assert rv.table_string(text, "bin", "version") is None
    assert rv.table_string(text, "dependencies", "version") == "0.0.3"


@pytest.mark.parametrize("line", ["version.workspace = true", "version = '1.2.3'", 'version = """1.2.3"""',
                                  'versions = "1.2.3"', '# version = "1.2.3"', 'version = ""'])
def test_a_manifest_without_a_plain_version_is_refused(line):
    with pytest.raises(rv.VersionError, match=r"names no \[package\] version"):
        rv.from_manifest(f'[package]\nname = "pseudolife-stdio"\n{line}\n')


def test_lock_version_reads_the_named_package_entry():
    text = ('version = 4\n\n[[package]]\nname = "pseudolife-daemon"\nversion = "1.0.0"\n'
            'dependencies = [\n "pseudolife-stdio",\n]\n\n'
            '[[package]]\nname = "pseudolife-stdio"\nversion = "2.0.0"\n\n'
            '[[package]]\nname = "pseudolife-stdio-extra"\nversion = "3.0.0"\n')
    assert rv.lock_version(text, "pseudolife-daemon") == "1.0.0"
    assert rv.lock_version(text, "pseudolife-stdio") == "2.0.0"
    assert rv.lock_version(text, "pseudolife-stdio-extra") == "3.0.0"
    assert rv.lock_version(text, "absent") is None


def test_at_rev_reads_history_and_none_before_the_authority(tmp_path):
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    (tmp_path / "README").write_text("x\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "before"], check=True)
    before = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (tmp_path / "rust" / "shim").mkdir(parents=True)
    (tmp_path / rv.AUTHORITY).write_text('[package]\nname = "pseudolife-stdio"\nversion = "0.18.0"\n')
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "crate"], check=True)
    assert rv.at_rev("HEAD", tmp_path) == "0.18.0"
    assert rv.at_rev(before, tmp_path) is None
    assert rv.at_rev("no-such-rev", tmp_path) is None


def test_cli_prints_and_checks(tmp_path, capsys):
    value = copy_fields(tmp_path)
    assert rv.main(["--root", str(tmp_path), "print"]) == 0
    assert rv.main(["--root", str(tmp_path), "check", "--tag", f"v{value}"]) == 0
    assert capsys.readouterr().out.split() == [value, value]
    MUTATE["plugin/release.json version"](tmp_path, "9.9.9")
    assert rv.main(["--root", str(tmp_path), "check"]) == 1
    assert "plugin/release.json version is 9.9.9" in capsys.readouterr().err


def test_the_reader_needs_no_tomllib_and_only_the_standard_library():
    """ops/integrate.py runs on Python 3.10, which has no tomllib."""
    tree = ast.parse(Path(rv.__file__).read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= {"__future__", "argparse", "json", "pathlib", "re", "subprocess", "sys", "typing"}
