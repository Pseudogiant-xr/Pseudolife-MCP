"""Audit comparison rules, native refusals, and archive publication on disposable banks."""
from __future__ import annotations

import atexit
import base64
from contextlib import contextmanager, nullcontext
import errno
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from . import normalize
from .rows import _bank
from rust.harness_support.database_guard import ProductionDatabaseError
from rust.harness_support.import_blocker import cold_harness_imports


SECRET = "synthetic-audit-body-must-not-escape"


class Bank:
    def __init__(self, name, url):
        self.name, self.url = name, url

    def __getitem__(self, index):
        return (self.name, self.url)[index]

    def __repr__(self):
        return "<disposable audit bank>"


@pytest.fixture
def candidate():
    value = os.environ.get("PSEUDOLIFE_RUST_BINARY") or os.environ.get("R1_PAIR_CANDIDATE")
    if not value:
        pytest.skip("PSEUDOLIFE_RUST_BINARY is required")
    return str(Path(value).resolve())


def invoke(candidate, home, *args, dsn=None, extra=None, closed_stdout=False, umask=-1):
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PSEUDOLIFE_", "PG"))}
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home),
               LOCALAPPDATA=str(home), XDG_CONFIG_HOME=str(home),
               PATH=str(home / "no-programs"), BROWSER="missing-browser",
               PSEUDOLIFE_MCP_URL="http://127.0.0.1:1",
               PSEUDOLIFE_MCP_CONFIG=str(home / "absent.yaml"),
               PSEUDOLIFE_MCP_DATA_DIR=str(home / "absent"))
    if dsn is not None:
        env["PSEUDOLIFE_MCP_DATABASE_URL"] = dsn
    env.update(extra or {})
    arguments = [a if isinstance(a, bytes) else str(a) for a in args]
    if closed_stdout:
        read, write = os.pipe()
        os.close(read)
        try:
            return subprocess.run([candidate, "board-audit", *arguments], env=env,
                                  stdout=write, stderr=subprocess.PIPE, timeout=30)
        finally:
            os.close(write)
    return subprocess.run([candidate, "board-audit", *arguments], env=env,
                          capture_output=True, timeout=30, umask=umask)


@pytest.fixture
def bank():
    name = _bank.name("pl_cf_audit_native")
    url = _bank.create(name, schema=False)
    try:
        with _bank.connect(name) as conn:
            conn.execute("""CREATE TABLE coordination_events (
                seq bigint PRIMARY KEY, event text, actor text, principal text,
                agent_id text, recipient_agent_id text, project text, task text,
                message_id text, payload text, created_at double precision,
                hlc text, prev_hash text, hash text, body text, body_salt text);
                CREATE TABLE coordination_agents (agent_id text);
                CREATE TABLE coordination_messages (
                message_id text, text text, fingerprint text, sender_agent_id text,
                recipient_agent_id text, project text, task text, expires_at double precision)
                """)
            row = event()
            columns = list(row)
            conn.execute(f"INSERT INTO coordination_events ({','.join(columns)}) "
                         f"VALUES ({','.join(['%s'] * len(columns))})", list(row.values()))
            conn.commit()
        yield Bank(name, url)
    finally:
        _bank.teardown(name)


def event(**changes):
    row = dict(seq=1, event="register", actor="agent", principal="example",
               agent_id="example-agent", recipient_agent_id=None, project="p",
               task="t", message_id=None, payload='{"safe":"雪"}', created_at=1700000000.0,
               hlc="", prev_hash="0" * 64, hash="", body=None, body_salt=None)
    row.update(changes)
    row["payload"] = json.dumps(json.loads(row["payload"]), ensure_ascii=False,
                                sort_keys=True, separators=(",", ":"))
    fields = ["seq", "event", "actor", "principal", "agent_id", "recipient_agent_id",
              "project", "task", "message_id", "created_at", "hlc", "payload"]
    material = ["pseudolife-coordination-audit-v1", *(row[k] for k in fields)]
    row["hash"] = hashlib.sha256((row["prev_hash"] + json.dumps(
        material, ensure_ascii=False, separators=(",", ":"))).encode()).hexdigest()
    return row


@pytest.fixture
def audit_rules():
    with cold_harness_imports():
        from rust.harness_support import audit_oracle

        yield audit_oracle


def test_audit_hash_distinguishes_stored_text_from_parsed_payload(audit_rules):
    row = event(payload='{"z":-0.0,"a":"雪","n":1e-7}', created_at=1700000000.1234567)
    parsed = {"z": -0.0, "n": 1e-7, "a": "雪"}
    assert audit_rules.canonical_payload(parsed) == row["payload"]
    assert audit_rules.audit_hash(row["prev_hash"], row) == row["hash"]
    assert audit_rules.audit_hash(row["prev_hash"], dict(row, payload=parsed)) == row["hash"]
    unsorted = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    assert audit_rules.audit_hash(row["prev_hash"], dict(row, payload=unsorted)) != row["hash"]


@pytest.mark.parametrize("defect,seq,reason", [
    ("genesis", 1, "broken_link"),
    ("gap", 3, "sequence_gap"),
    ("link", 2, "broken_link"),
    ("payload", 2, "hash_mismatch"),
    ("head_missing", 3, "head_missing"),
    ("head_mismatch", 2, "head_mismatch"),
])
def test_audit_verifier_identifies_the_first_break(audit_rules, defect, seq, reason):
    first = event()
    last = event(seq=2, prev_hash=first["hash"])
    head = (2, last["hash"])
    assert audit_rules.verify_audit_chain([first, last], expect_head=head) == {
        "ok": True, "events": 2, "first_seq": 1, "head_seq": 2,
        "head_hash": last["hash"], "head_created_at": last["created_at"], "start_cut": None,
    }
    if defect == "genesis":
        first = event(prev_hash="a" * 64)
    elif defect == "gap":
        last = event(seq=3, prev_hash=first["hash"])
    elif defect == "link":
        last = event(seq=2, prev_hash="b" * 64)
    elif defect == "payload":
        last["payload"] = '{"changed":true}'
    elif defect == "head_missing":
        head = (seq, head[1])
    elif defect == "head_mismatch":
        head = (seq, "c" * 64)
    assert audit_rules.verify_audit_chain([first, last], expect_head=head) == {
        "ok": False, "seq": seq, "reason": reason,
    }


def _sent_event():
    body, salt = "fixture body 雪", "ab" * 16
    digest = hashlib.sha256(bytes.fromhex(salt) + body.encode("utf-8")).hexdigest()
    return event(event="send", message_id="message", body=body, body_salt=salt,
                 payload=json.dumps({"text_commitment": digest}))


@pytest.mark.parametrize("change,reason", [
    ({"body": "changed"}, "body_mismatch"),
    ({"body_salt": "AB" * 16}, "body_mismatch"),
    ({"body": None}, "body_mismatch"),
    ({"body": None, "body_salt": None}, "body_missing"),
    ({"event": "note"}, "body_mismatch"),
])
def test_audit_verifier_checks_body_commitments(audit_rules, change, reason):
    sent = _sent_event()
    assert audit_rules.verify_audit_chain([sent])["ok"]
    assert audit_rules.verify_audit_chain([event(**dict(sent, **change))]) == {
        "ok": False, "seq": 1, "reason": reason,
    }


def test_audit_verifier_validates_the_redaction_record(audit_rules):
    sent = _sent_event()
    blank = dict(sent, body=None, body_salt=None)
    old_export = {key: value for key, value in blank.items() if key != "body"}
    assert audit_rules.verify_audit_chain([old_export]) == {
        "ok": False, "seq": 1, "reason": "body_not_exported",
    }
    redact = event(seq=2, event="redact", actor="operator", prev_hash=sent["hash"],
                   payload='{"seq":1,"message_id":"message","audit_copy":"removed"}')
    assert audit_rules.verify_audit_chain([blank, redact])["ok"]
    assert audit_rules.verify_audit_chain([sent, redact]) == {
        "ok": False, "seq": 1, "reason": "body_mismatch",
    }


@pytest.mark.parametrize("actor,copy_status,message_id", [
    pytest.param("daemon", "removed", "message", id="actor"),
    pytest.param("operator", "kept", "message", id="kept-copy"),
    pytest.param("operator", "removed", "other-message", id="wrong-message"),
])
def test_audit_verifier_requires_matching_operator_redaction(audit_rules, actor, copy_status, message_id):
    sent = _sent_event()
    blank = dict(sent, body=None, body_salt=None)
    payload = {"seq": 1, "message_id": message_id, "audit_copy": copy_status}
    redact = event(seq=2, event="redact", actor=actor, prev_hash=sent["hash"],
                   payload=json.dumps(payload))
    assert audit_rules.verify_audit_chain([blank, redact]) == {
        "ok": False, "seq": 1, "reason": "body_missing",
    }


def test_audit_verifier_anchors_a_pruned_chain(audit_rules):
    gone = event(created_at=0.0)
    first = event(seq=2, prev_hash=gone["hash"], created_at=86_400.125)
    payload = {"through_seq": 1, "through_hash": gone["hash"], "retention_days": 1, "cutoff": 86_400}
    cut_args = {"seq": 3, "event": "audit_prune", "actor": "daemon",
                "created_at": 172_800.125, "prev_hash": first["hash"]}
    cut = event(**cut_args, payload=json.dumps(payload))
    verdict = audit_rules.verify_audit_chain([first, cut])
    assert verdict["ok"] and verdict["start_cut"] == {
        "seq": 3, "created_at": 172_800.125, "cutoff": 86_400,
        "retention_days": 1, "through_seq": 1,
    }
    assert audit_rules.verify_audit_chain([first, cut], expect_head=(1, gone["hash"])) == {
        "ok": False, "seq": 1, "reason": "head_pruned", "start_cut": verdict["start_cut"],
    }
    invalid = event(**cut_args, payload=json.dumps(dict(payload, cutoff=86_399)))
    assert audit_rules.verify_audit_chain([first, invalid]) == {
        "ok": False, "seq": 2, "reason": "unanchored_start",
    }


@pytest.mark.parametrize("actor,days,through_seq", [
    pytest.param("operator", 1, 1, id="actor"),
    pytest.param("daemon", 0, 1, id="zero-days"),
    pytest.param("daemon", 36_501, 1, id="above-max-days"),
    pytest.param("daemon", 1.0, 1, id="float-days"),
    pytest.param("daemon", True, 1, id="bool-days"),
    pytest.param("daemon", 1, 1.0, id="float-seq"),
    pytest.param("daemon", 1, True, id="bool-seq"),
])
def test_audit_verifier_rejects_invalid_prune_record(audit_rules, actor, days, through_seq):
    gone = event(created_at=0.0)
    first = event(seq=2, prev_hash=gone["hash"], created_at=86_400.125)
    payload = {"through_seq": through_seq, "through_hash": gone["hash"],
               "retention_days": days, "cutoff": 86_400}
    # Keep the cutoff and first retained row consistent even when days is zero
    # or above the ceiling, so those witnesses exercise the retention guards.
    cut = event(seq=3, event="audit_prune", actor=actor,
                created_at=(days + 1) * 86_400 + 0.125,
                prev_hash=first["hash"], payload=json.dumps(payload))
    assert audit_rules.verify_audit_chain([first, cut]) == {
        "ok": False, "seq": 2, "reason": "unanchored_start",
    }


@contextmanager
def _cold_audit(monkeypatch):
    # Load the row afresh even when another test already imported it.
    path = Path(__file__).with_name("rows") / "audit.py"
    spec = spec_from_file_location(f"{__package__}.rows._audit_probe", path)
    isolated = module_from_spec(spec)
    with monkeypatch.context() as patch, cold_harness_imports():
        patch.setattr(normalize, "RULES", normalize.RULES.copy())
        patch.setattr(_bank.NAMES, "labels", _bank.NAMES.labels.copy())
        try:
            spec.loader.exec_module(isolated)
            yield isolated
        finally:
            if cleanup := getattr(isolated, "_drop_all", None):
                atexit.unregister(cleanup)


@pytest.mark.parametrize("recorded", [False, True], ids=["selected-rows", "recorded-refusal"])
def test_audit_bank_export_reference_without_the_frozen_package(monkeypatch, tmp_path, recorded):
    sender = b'{"seq":2, "project":"p", "task":"t1", "agent_id":"a", "recipient_agent_id":null, "created_at":10}\n'
    recipient = b'{"seq":3,"project":"p","task":"t1","agent_id":"b","recipient_agent_id":"a","created_at":20}\n'
    data = (b'{"seq":1,"project":"p","task":"t1","agent_id":"a","recipient_agent_id":null,"created_at":9}\n'
            + sender + recipient
            + b'{"seq":4,"project":"p","task":"t1","agent_id":"a","recipient_agent_id":null,"created_at":30}\n'
            + b'{"seq":5,"project":"q","task":"t1","agent_id":"a","recipient_agent_id":null,"created_at":15}\n'
            + b'{"seq":6,"project":"p","task":"t2","agent_id":"a","recipient_agent_id":null,"created_at":15}\n'
            + b'{"seq":7,"project":"p","task":"t1","agent_id":"b","recipient_agent_id":null,"created_at":15}\n')
    with _cold_audit(monkeypatch) as audit:
        source = MagicMock(return_value=data)
        monkeypatch.setattr(audit, "export_bytes", source)
        monkeypatch.setattr(audit, "ensure", lambda kind: audit._name(kind))
        monkeypatch.setattr(_bank, "url", lambda name: f"postgresql://localhost/{name}")
        if recorded:
            case = audit.bank_export_case("export-agent-unknown", "rich", ["--agent", "ffffffff0"], recorded=True)
        else:
            case = audit.bank_export_case("export-control", "rich",
                                          ["--project", "p", "--ta=t2", "--task", "t1", "--agent", "a",
                                           "--since", "10", "--until", "30"],
                                          project="p", task="t1", agent=audit._Lazy(lambda: "a"), since=10, until=30)
        previous_umask = audit.core.pin_umask()
        try:
            want = case.reference(tmp_path / "bank-export")
        finally:
            if previous_umask is not None:
                os.umask(previous_umask)

    newline = b"\r\n" if audit.core.WINDOWS else b"\n"
    if recorded:
        source.assert_not_called()
        assert want["exit"] == 2
        assert audit.core.decode(want, "stdout") == b""
        assert audit.core.decode(want, "stderr") == b"board-audit: no agent id starts with ffffffff0" + newline
    else:
        source.assert_called_once_with("rich")
        assert want["exit"] == 0
        assert audit.core.decode(want, "stdout") == (sender + recipient).replace(b"\n", newline)
        assert audit.core.decode(want, "stderr") == b""
    assert want["files"] == {"cwd": "dir"}
    assert want["modes"] == ({} if audit.core.WINDOWS else {"cwd": "0o755"})
    assert want["db"] == "unchanged"


def test_audit_export_time_references_match_frozen_argument_parsing(monkeypatch):
    from datetime import datetime
    import sys

    with _cold_audit(monkeypatch) as audit:
        monkeypatch.setattr(_bank, "url", lambda name: f"postgresql://localhost/{name}")
        tested = set()
        for case in audit.cases():
            reference = case.reference
            if getattr(reference, "func", None) is not audit._bank_export_reference:
                continue
            _, _, filters, _ = reference.args
            bounds = {}
            for index, arg in enumerate(case.argv):
                if isinstance(arg, str):
                    flag, joined, value = arg.partition("=")
                    if flag in ("--since", "--until"):
                        bounds[flag[2:]] = value if joined else case.argv[index + 1]
            assert bounds.keys() == filters.keys() & {"since", "until"}, case.id
            for bound, arg in bounds.items():
                # The frozen CLI tries an epoch first, then an ISO datetime.
                try:
                    parsed = float(arg)
                except ValueError:
                    try:
                        parsed = datetime.fromisoformat(arg)
                    except ValueError:
                        # Some ISO spellings entered the frozen parser in 3.11.
                        if sys.version_info < (3, 11):
                            continue
                        raise
                declared = filters[bound]
                if not isinstance(declared, datetime) and isinstance(parsed, datetime):
                    parsed = parsed.timestamp()
                assert declared == parsed, (case.id, bound, arg)
                tested.add(bound)
        assert tested == {"since", "until"}


def test_audit_live_export_refuses_candidate_recordings(monkeypatch, tmp_path):
    with _cold_audit(monkeypatch) as audit:
        case_id = "export-agent-unknown"
        golden_dir = tmp_path / "goldens"
        golden_dir.mkdir()
        golden = {"cases": {case_id: {"exit": 2, "stdout": "", "stderr": ""}},
                  "recorded_from_candidate": {case_id: {"platform": audit.core.PLATFORM}}}
        (golden_dir / f"audit.{audit.core.PLATFORM}.json").write_text(json.dumps(golden), encoding="utf-8")
        monkeypatch.setattr(audit, "__file__", str(tmp_path / "rows" / "audit.py"))
        monkeypatch.setattr(audit, "ensure", lambda kind: audit._name(kind))
        monkeypatch.setattr(_bank, "url", lambda name: f"postgresql://localhost/{name}")
        case = audit.bank_export_case(case_id, "rich", ["--agent", "ffffffff0"], recorded=True)
        with pytest.raises(ValueError, match="live export refusal requires a frozen Python recording"):
            case.reference(tmp_path / "bank-export")


def test_audit_remaining_export_references(monkeypatch, tmp_path):
    unfiltered = b'{"seq":1,"task":"t2"}\n{"seq":2,"task":"t3"}\n'
    filtered = unfiltered.splitlines(keepends=True)[1]
    no_bank = ("board-audit: no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL, or run "
               "where the lite tier's data dir holds one (Docker tier: start the daemon, or run "
               "`docker exec -it pseudolife-mcp-daemon pseudolife-mcp board-audit ...` on its host)")
    expected = {
        "export-out": (0, "", unfiltered),
        "export-out-filtered": (0, "", filtered),
        "export-out-exists": (2, f"board-audit: {tmp_path / 'export-out-exists' / 'out.jsonl'} "
                              "exists; export never replaces a file", b"keep"),
        "export-agent-unknown-out": (2, "board-audit: no agent id starts with ffffffff0", b""),
        "export-agent-ambiguous-out": (2, "board-audit: --agent ab12cd34ef5 matches several ids: "
                                      "ab12cd34ef56, ab12cd34ef57; give a longer prefix", b""),
        "export-out-missing-dir-before-agent": (
            2, f"board-audit: cannot create {tmp_path / 'export-out-missing-dir-before-agent' / 'missing' / 'out.jsonl'}: "
            "No such file or directory", None),
        "export-stdout-closed": (2, "board-audit: the output was closed before the export finished", None),
        "export-invalid-payload-prefix": (2, "board-audit failed; the database error is not shown because it can "
                                          "include the connection string. Check PSEUDOLIFE_MCP_DATABASE_URL.", None),
        "export-local-minute-no-bank": (2, no_bank, None),
        "export-iso-overwritten": (2, no_bank, None),
    }
    with _cold_audit(monkeypatch) as audit:
        if audit.core.WINDOWS:
            invalid = tmp_path / "export-windows-invalid-name" / "bad<name.jsonl"
            long = tmp_path / "export-windows-long-name" / ("x" * 260)
            expected["export-windows-invalid-name"] = (2, f"board-audit: cannot create {invalid}: Invalid argument", None)
            expected["export-windows-long-name"] = (2, f"board-audit: cannot create {long}: Invalid argument", None)
            original_open = Path.open

            def open_path(path, *args, **kwargs):
                if path == long and args == ("x",):
                    raise OSError(errno.EINVAL, "Invalid argument")
                return original_open(path, *args, **kwargs)

            monkeypatch.setattr(Path, "open", open_path)
        else:
            loop = tmp_path / "export-output-loop" / "loop"
            expected["export-output-loop"] = (2, f"board-audit: cannot create {loop}: File exists", None)
        for key in tuple(os.environ):
            if key.startswith("PSEUDOLIFE_") or key in {"PL_HARNESS_BASE_DATABASE", "TZ", "TZDIR"}:
                monkeypatch.delenv(key)
        monkeypatch.setattr(audit, "ensure", lambda kind: audit._name(kind))
        monkeypatch.setattr(_bank, "url", lambda name: f"postgresql://localhost/{name}")
        monkeypatch.setattr(audit, "export_bytes", lambda kind: unfiltered)
        # The invalid-payload bank has one good row before the malformed tail.
        connection = MagicMock()
        monkeypatch.setattr(_bank, "connect", connection)
        prefix = (None,) * 9 + ("{}",) + (None,) * 6
        malformed = prefix[:9] + ("{",) + prefix[10:]
        monkeypatch.setattr(audit, "_stored_audit_rows", lambda conn: iter([prefix, malformed]))
        encode = MagicMock(return_value=b'{"seq":1}\n')
        monkeypatch.setattr(audit, "_export_line", encode)
        blocked = MagicMock(side_effect=AssertionError("export reference started a child"))
        monkeypatch.setattr(subprocess, "Popen", blocked)
        for name in dir(os):
            if name == "system" or name.startswith(("spawn", "posix_spawn", "exec")):
                monkeypatch.setattr(os, name, blocked)
        cases = {case.id: case for case in audit.cases() if case.runs_here()}
        newline = b"\r\n" if audit.core.WINDOWS else b"\n"
        previous_umask = audit.core.pin_umask()
        try:
            for case_id, (code, stderr_text, data) in expected.items():
                case = cases[case_id]
                assert case.reference is not None, case_id
                want = case.reference(tmp_path / case_id)
                assert want["exit"] == code, case_id
                stderr = stderr_text.encode("utf-8") + newline if stderr_text else b""
                assert audit.core.decode(want, "stderr") == stderr, case_id
                stdout = b'{"seq":1}' + newline if case_id == "export-invalid-payload-prefix" else b""
                assert audit.core.decode(want, "stdout") == stdout, case_id
                files = {"cwd": "dir"}
                modes = {} if audit.core.WINDOWS else {"cwd": "0o755"}
                if data is not None:
                    files["out.jsonl"] = "file:" + base64.b64encode(data).decode()
                    if not audit.core.WINDOWS:
                        modes["out.jsonl"] = "0o644"
                if case_id == "export-output-loop":
                    files["loop"] = "link:loop"
                    # A symlink created under the same umask has the seed's
                    # mode on both Linux and Darwin.
                    seed_link = tmp_path / "seed-link"
                    seed_link.symlink_to("seed-link")
                    modes["loop"] = oct(stat.S_IMODE(seed_link.lstat().st_mode))
                assert want["files"] == files, case_id
                assert want["modes"] == modes, case_id
                if "no-bank" in case_id or case_id == "export-iso-overwritten":
                    assert "db" not in want, case_id
                else:
                    assert want["db"] == "unchanged", case_id
        finally:
            if previous_umask is not None:
                os.umask(previous_umask)
        encode.assert_called_once_with(prefix)
        blocked.assert_not_called()


def test_audit_invalid_payload_reference_requires_a_malformed_second_row(monkeypatch, tmp_path):
    with _cold_audit(monkeypatch) as audit:
        monkeypatch.setattr(audit, "ensure", lambda kind: audit._name(kind))
        monkeypatch.setattr(_bank, "url", lambda name: f"postgresql://localhost/{name}")
        monkeypatch.setattr(_bank, "connect", MagicMock())
        valid = (None,) * 9 + ("{}",) + (None,) * 6
        monkeypatch.setattr(audit, "_stored_audit_rows", lambda conn: iter([valid, valid]))
        case = next(case for case in audit.cases() if case.id == "export-invalid-payload-prefix")
        with pytest.raises(AssertionError, match="second audit payload must be malformed"):
            case.reference(tmp_path / "export")


@pytest.mark.parametrize(("code", "message"), [
    (errno.ENOENT, "No such file or directory"),
    (errno.EINVAL, "Invalid argument"),
    (None, None),
])
def test_audit_long_output_reference_uses_filesystem_error(monkeypatch, tmp_path, code, message):
    with _cold_audit(monkeypatch) as audit:
        monkeypatch.setattr(audit, "ensure", lambda kind: audit._name(kind))
        monkeypatch.setattr(_bank, "url", lambda name: f"postgresql://localhost/{name}")
        case = next(case for case in audit.cases() if case.id == "export-windows-long-name")
        home = tmp_path / "long-output"
        out = home / ("x" * 260)
        original_open = Path.open

        def open_path(path, *args, **kwargs):
            if path == out and args == ("x",):
                if code is None:
                    return nullcontext()
                raise OSError(code, message)
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_path)
        if code is None:
            with pytest.raises(AssertionError, match="invalid export path was successfully opened"):
                case.reference(home)
            return
        want = case.reference(home)
        newline = "\r\n" if audit.core.WINDOWS else "\n"
        assert audit.core.decode(want, "stderr") == (
            f"board-audit: cannot create {out}: {message}{newline}".encode())
        assert want["exit"] == 2
        assert want["files"] == {"cwd": "dir"}


def test_audit_removed_redaction_reference_uses_a_separate_seed_copy(monkeypatch, tmp_path):
    from . import compare

    message_id, reason, now = "a" * 32, "wrong paste ✓", 1700000000.0
    previous = "1" * 64
    expected_event = event(seq=8, event="redact", actor="operator", principal="",
                           agent_id="sender", recipient_agent_id="recipient", project="p", task="t",
                           message_id=message_id, created_at=now, prev_hash=previous,
                           payload=json.dumps({"audit_copy": "removed", "message_id": message_id,
                                               "reason": reason, "seq": 3}))
    expected_db = {"tables": {"coordination_events": [json.dumps(expected_event)],
                              "coordination_messages": [json.dumps({"message_id": message_id, "expires_at": now})]}}
    with _cold_audit(monkeypatch) as audit:
        from rust.harness_support import fixture_rows

        seed = audit._name("rich")
        copy_bank = MagicMock()
        dump = MagicMock(return_value=expected_db)
        guard = MagicMock()
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.side_effect = [
            SimpleNamespace(fetchall=lambda: [(3, "sender", "recipient", "p", "t")]),
            SimpleNamespace(fetchone=lambda: (7, previous)),
            SimpleNamespace(fetchone=lambda: ("synthetic text",)),
            None, None, None,
        ]
        insert = MagicMock(return_value=("synthetic insert", ()))
        monkeypatch.setattr(audit, "ensure", lambda _: seed)
        monkeypatch.setattr(audit, "ids", lambda _: {"to_redact": message_id})
        monkeypatch.setattr(audit, "_copy", copy_bank)
        monkeypatch.setattr(audit, "_assert_audit_database", guard)
        monkeypatch.setattr(audit, "time", SimpleNamespace(time=lambda: now))
        monkeypatch.setattr(_bank, "connect", lambda _: conn)
        monkeypatch.setattr(_bank, "dump", dump)
        monkeypatch.setattr(_bank, "url", lambda name: f"postgresql://localhost/{name}")
        monkeypatch.setattr(fixture_rows, "checked_fixture_insert", insert)
        case = audit.redact_removed_case("removed-control", "to_redact", reason, cut=10)
        previous_umask = audit.core.pin_umask()
        try:
            want = case.reference(tmp_path / "removed")
        finally:
            if previous_umask is not None:
                os.umask(previous_umask)

        reference_bank = audit._REDACT_REFERENCE_DB
        copy_bank.assert_called_once_with(seed, reference_bank)
        guard.assert_called_once_with(conn, reference_bank)
        dump.assert_called_once_with(reference_bank)
        insert.assert_called_once_with(conn, "coordination_events", [expected_event])
        assert conn.execute.call_args_list[3].args == (
            "UPDATE coordination_events SET body=NULL,body_salt=NULL WHERE seq=%s", (3,),
        )
        assert conn.execute.call_args_list[4].args == (
            "UPDATE coordination_messages SET text=NULL,fingerprint='redacted',"
            "expires_at=LEAST(expires_at,%s) WHERE message_id=%s", (now, message_id),
        )
        assert reference_bank not in {seed, audit.REDACT_DB}
        assert case.argv[3].make() == message_id[:10]
        assert want["exit"] == 0 and want["db"] == expected_db
        assert want["files"] == {"cwd": "dir"}
        assert want["modes"] == ({} if audit.core.WINDOWS else {"cwd": "0o755"})
        report = json.loads(audit.core.decode(want, "stdout"))
        assert report == {"ok": True, "message_id": message_id, "seq": 3, "redact_seq": 8,
                          "redact_hash": expected_event["hash"], "expect_head": "8:" + expected_event["hash"],
                          "live_body_cleared": True, "audit_copy": "removed", "other_copies": [], "vacuumed": True}
        assert b"verify --expect-head 8:" + expected_event["hash"].encode() in audit.core.decode(want, "stderr")
        stale = {"tables": dict(expected_db["tables"], coordination_messages=[
            json.dumps({"message_id": message_id, "expires_at": now, "text": "synthetic text"})])}
        assert compare.diff(want, dict(want, db=stale), case.rules)[0] == "db differ:"


def test_audit_archive_edit_rehashes_without_the_frozen_package(monkeypatch):
    raw = event()
    raw["payload"] = json.loads(raw["payload"])
    with _cold_audit(monkeypatch) as isolated:
        monkeypatch.setattr(isolated, "export_rows", lambda _: [raw])
        edited = json.loads(isolated.archive("reserved-number-key-single"))

    expected = event(payload='{"safe":"雪","$serde_json::private::Number":"1"}')
    assert edited["payload"] == json.loads(expected["payload"])
    assert edited["hash"] == expected["hash"]


@pytest.mark.parametrize("variant", ["matching-head", "mismatching-head", "stored-text-edit", "no-log"])
def test_audit_bank_verify_reference_without_the_frozen_package(monkeypatch, tmp_path, variant):
    row = event()
    head = "1:" + ("0" * 64 if variant == "mismatching-head" else row["hash"])
    kind = "nolog" if variant == "no-log" else "rich"
    if variant == "no-log":
        code, stdout = 2, b""
        stderr = (b"board-audit: this bank has no audit log yet (schema older than v42); "
                  b"start a v42 daemon once to create it")
    elif variant == "stored-text-edit":
        row["payload"] = '{"safe": "雪"}'  # Same JSON value, different stored hash input.
        code, stderr = 1, b""
        stdout = b'{"ok": false, "seq": 1, "reason": "hash_mismatch"}'
    elif variant == "mismatching-head":
        code, stderr = 1, b""
        stdout = b'{"ok": false, "seq": 1, "reason": "head_mismatch"}'
    else:
        code, stderr = 0, b""
        stdout = (b'{"ok": true, "events": 1, "first_seq": 1, "head_seq": 1, "head_hash": "'
                  + row["hash"].encode("ascii") + b'", "head_created_at": 1700000000.0, "start_cut": null}')

    with _cold_audit(monkeypatch) as audit:
        name = audit._name(kind)
        conn = MagicMock()
        conn.__enter__.return_value = conn
        values = tuple(row[key] for key in audit._EXPORT_FIELDS + audit._BODY_FIELDS)
        conn.execute.side_effect = [None, SimpleNamespace(fetchone=lambda: (True,)), [values]]
        connect = MagicMock(return_value=conn)
        monkeypatch.setattr(audit, "ensure", lambda _: name)
        monkeypatch.setattr(_bank, "url", lambda label: f"postgresql://localhost/{label}")
        monkeypatch.setattr(_bank, "connect", connect)
        case = audit.bank_verify_case("bank-verify-control", kind,
                                      expect_head=None if kind == "nolog" else audit._Lazy(lambda: head))
        previous_umask = audit.core.pin_umask()
        try:
            observation = case.reference(tmp_path / "bank-verify")
        finally:
            if previous_umask is not None:
                os.umask(previous_umask)

    newline = b"\r\n" if audit.core.WINDOWS else b"\n"
    assert observation["exit"] == code
    assert audit.core.decode(observation, "stdout") == (stdout + newline if stdout else b"")
    assert audit.core.decode(observation, "stderr") == (stderr + newline if stderr else b"")
    assert observation["files"] == {"cwd": "dir"}
    assert observation["modes"] == ({} if audit.core.WINDOWS else {"cwd": "0o755"})
    assert observation["db"] == "unchanged"
    if kind == "nolog":
        connect.assert_not_called()
    else:
        connect.assert_called_once_with(name)
        assert conn.execute.call_args_list[0].args == ("SET LOCAL extra_float_digits = 3",)
        assert conn.execute.call_args.args == (
            "SELECT seq,event,actor,principal,agent_id,recipient_agent_id,project,task,"
            "message_id,payload,created_at,hlc,prev_hash,hash,body,body_salt "
            "FROM coordination_events ORDER BY seq",
        )
        assert case.argv == ["board-audit", "verify", "--expect-head", head]


def test_audit_export_preserves_field_order_and_legacy_payload():
    from .rows import audit

    with cold_harness_imports():
        line = audit._export_line((
            7, "send", "agent", "alice", "a", "b", "p", "t", "m",
            r'{"odd":NaN,"text":"\ud800"}', 1500.0, "", "prev", "hash", None, None,
        ))
    assert line == (
        b'{"seq":7,"event":"send","actor":"agent","principal":"alice","agent_id":"a",'
        b'"recipient_agent_id":"b","project":"p","task":"t","message_id":"m",'
        b'"payload":{"odd":NaN,"text":"\\ud800"},"created_at":1500.0,"hlc":"",'
        b'"prev_hash":"prev","hash":"hash","body":null,"body_salt":null}\n'
    )


@pytest.mark.parametrize(("platform", "digest"), [
    ("linux", "5b9189e673014d9a990c13aedb153357ab16f7395e806cb65a1cdc369ea240a2"),
    ("windows", "f26d56b53d52eef92094496e9a1e26501823492307445680656c6db2b459ce1c"),
])
def test_audit_file_references_are_frozen_and_registered(monkeypatch, platform, digest):
    from .rows import audit

    # Frozen Python file observations at f1faebaa7, captured on each
    # platform. Normalize checkout line endings before hashing the file.
    fixture = Path(audit.__file__).with_name(f"audit.file.{platform}.json")
    raw = fixture.read_text(encoding="utf-8")
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() == digest, (
        "audit file references differ from the frozen capture")

    monkeypatch.setattr(audit.core, "PLATFORM", platform)
    monkeypatch.setattr(_bank, "url", lambda name: f"postgresql://localhost/{name}")
    hooked = {case.id for case in audit.cases() if platform in case.platforms
              and getattr(case.reference, "func", None) is audit._file_reference}
    assert hooked == set(json.loads(raw))


def test_audit_file_reference_resolves_the_supplied_archive_head(tmp_path):
    from .rows import audit

    digest = "d" * 64
    archive = json.dumps({"hash": digest, "created_at": 17.25}).encode() + b"\r\n\r\n"
    case = audit.file_case("reference-control", ["verify", "--input", "{HOME}/archive.jsonl"],
                           files={"archive.jsonl": archive})
    recorded = [1, '{"head_hash": "<archive-head-hash>", "head_created_at": <archive-head-created-at>}\n',
                "fixture diagnostic\n"]
    previous_umask = audit.core.pin_umask()
    try:
        want = audit._file_reference(case, recorded, tmp_path / "case")
    finally:
        if previous_umask is not None:
            os.umask(previous_umask)

    assert want["exit"] == 1
    assert audit.core.decode(want, "stdout") == (
        '{"head_hash": "' + digest + '", "head_created_at": 17.25}\n').encode()
    assert audit.core.decode(want, "stderr") == b"fixture diagnostic\n"
    assert want["files"] == {"cwd": "dir", "archive.jsonl": "file:" + base64.b64encode(archive).decode()}
    assert want["modes"] == ({} if audit.core.WINDOWS else {"cwd": "0o755", "archive.jsonl": "0o644"})


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_audit_file_reference_streams_match_shared_goldens(platform):
    from .rows import audit

    references = audit._file_references(platform)
    goldens = audit._recorded_cases(platform)
    shared = references.keys() & goldens.keys()
    assert shared == {"audit-reserved-number-key-duplicates", "input-empty", "input-missing"}
    for case_id in sorted(shared):
        record = goldens[case_id]
        assert references[case_id] == [record["exit"],
                                       audit.core.decode(record, "stdout").decode(),
                                       audit.core.decode(record, "stderr").decode()], case_id


def test_audit_rich_fixture_keeps_runtime_mail_unexpired(monkeypatch):
    from .rows import audit

    with cold_harness_imports():
        fixture = json.loads(audit._FIXTURE.read_text(encoding="utf-8"))
        variant = fixture["variants"]["rich"]
        rows = variant["rows"]
        events = rows["coordination_events"]
        events.sort(key=lambda row: row["seq"])
        anchor = fixture["clock_anchor"]
        fixed = [dict(row) for row in events if row["created_at"] < anchor]
        now = anchor + 123.125
        monkeypatch.setattr(audit, "time", SimpleNamespace(time=lambda: now))
        audit._shift_rich_clock(rows, anchor)
        assert events[:len(fixed)] == fixed
        assert audit.verify_audit_chain(events)["ok"]
        message_id = variant["ids"]["future"]
        [message] = [row for row in rows["coordination_messages"] if row["message_id"] == message_id]
        [send] = [row for row in events if row["message_id"] == message_id]
        assert message["created_at"] == send["created_at"] == now
        assert message["expires_at"] == json.loads(send["payload"])["expires_at"] == now + audit.DAY


@pytest.mark.parametrize("refusal, exit_code, stdout, diagnostic", [
    ("invalid_reason", 1, b'{"ok": false, "message_id": "sample-id", "reason": "invalid_reason"}',
     b"--reason must be 1-240 characters on one line, with no control, format or separator characters"),
    ("busy", 2, b"", b"the board is busy: a row or the audit chain stayed locked for more than 5 s. "
     b"Nothing was changed; retry"),
])
def test_audit_redact_refusal_reference_without_the_frozen_package(
        monkeypatch, tmp_path, refusal, exit_code, stdout, diagnostic):
    from . import compare

    seed = {"tables": {"coordination_events": [], "coordination_messages": []},
            "sequences": {"coordination_events_seq_seq": 3}}
    with _cold_audit(monkeypatch) as audit:
        name = audit._name("rich")
        monkeypatch.setattr(audit, "ensure", lambda _: name)
        monkeypatch.setattr(audit, "_BASELINE", {name: seed})
        monkeypatch.setattr(_bank, "url", lambda label: f"postgresql://localhost/{label}")
        case = audit.redact_case("redact-control", "rich", audit._Lazy(lambda: "sample-id"), "  ",
                                 hold=refusal == "busy", refusal=refusal)
        setup = MagicMock(side_effect=AssertionError("the reference must not clone or lock a bank"))
        monkeypatch.setattr(case, "setup", setup)
        previous_umask = audit.core.pin_umask()
        try:
            want = case.reference(tmp_path / "redact-refusal")
        finally:
            if previous_umask is not None:
                os.umask(previous_umask)

        newline = b"\r\n" if audit.core.WINDOWS else b"\n"
        assert want["exit"] == exit_code
        assert audit.core.decode(want, "stdout") == (stdout + newline if stdout else b"")
        assert audit.core.decode(want, "stderr") == b"board-audit: " + diagnostic + newline
        assert want["files"] == {"cwd": "dir"}
        assert want["modes"] == ({} if audit.core.WINDOWS else {"cwd": "0o755"})
        assert want["db"] == seed
        setup.assert_not_called()

        changed = dict(seed, sequences={"coordination_events_seq_seq": 4})
        assert compare.diff(want, dict(want, db=changed), case.rules)[0] == "db differ:"


def test_audit_redaction_diagnostics_match_frozen_source_by_ast():
    import ast
    from .rows import audit

    source = Path(__file__).resolve().parents[2] / "pseudolife_memory/board_audit_cli.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    table, = (node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "_REDACT_REFUSALS"
                      for target in node.targets))
    assert audit._REDACT_REFUSALS == ast.literal_eval(table)

    def diagnostic(function, prefix):
        body, = (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function)
        text, = (node.args[0].value for node in ast.walk(body)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "AuditCliError" and node.args
                 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                 and node.args[0].value.startswith(prefix))
        return text

    assert audit._REDACT_UNAVAILABLE == {
        "busy": diagnostic("_redact", "the board is busy"),
        "nolog": diagnostic("_bank", "this bank has no audit log yet"),
        "prev46": diagnostic("_bank", "this bank's audit log predates schema v46"),
    }


def test_audit_fixture_checks_the_database_before_inserts(monkeypatch):
    from .rows import audit

    name = audit._name("fixture_guard")
    other = audit._name("other_guard")
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (other,)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    with pytest.raises(ValueError, match="audit fixture connection did not reach"):
        audit._seed_captured(conn, name, "legacy")
    conn.execute.assert_called_once_with("SELECT current_database()")


@pytest.mark.parametrize("kind", ["rich", "stats", "empty", "nolog", "tampered", "prev49", "prev46"])
def test_audit_ensure_refuses_before_database_setup(monkeypatch, kind):
    from .rows import audit
    from rust.harness_support import fixture_rows

    name = audit._name(kind)
    monkeypatch.setenv("_PSEUDOLIFE_PRODUCTION_DB", name)
    monkeypatch.setattr(audit, "_BASELINE", {})
    monkeypatch.setattr(audit, "_CREATED", set())
    actions = (
        (_bank, "create"), (_bank, "connect"), (_bank, "drop"),
        (_bank, "_admin"), (_bank, "dump"), (fixture_rows, "checked_fixture_insert"),
    )
    stubs = []
    for module, action in actions:
        stub = MagicMock(side_effect=AssertionError(f"database action {action} ran before the audit guard"))
        monkeypatch.setattr(module, action, stub)
        stubs.append(stub)

    with pytest.raises(ProductionDatabaseError, match="it is a production bank"):
        audit.ensure(kind)
    for stub in stubs:
        stub.assert_not_called()
    assert audit._CREATED == set()
    assert audit._BASELINE == {}


def test_audit_captured_seed_runs_without_the_frozen_package(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    conn = MagicMock()
    cursor = conn.execute.return_value

    with _cold_audit(monkeypatch) as audit:
        name = audit._name("payload")
        cursor.fetchone.return_value = (name,)
        cursor.__enter__.return_value.description = [
            SimpleNamespace(name=column) for column in audit._EXPORT_FIELDS + audit._BODY_FIELDS
        ]
        ids = audit._seed_captured(conn, name, "payload")

    assert ids == {"legacy": "ef12ab34cd5600000000000000000001"}
    insert, (payload,) = conn.execute.call_args.args
    assert insert.as_string().startswith('INSERT INTO "public"."coordination_events" ')
    assert [row["event"] for row in payload.obj] == ["register", "send", "register"]
    assert payload.obj[1]["message_id"] == ids["legacy"]


@pytest.mark.parametrize("target_type,error,message", [
    ("unowned", ValueError, "not owned by this harness run"),
    ("non_audit", ValueError, "expected a disposable audit database"),
    ("production", ProductionDatabaseError, "it is a production bank"),
])
def test_audit_copy_checks_the_target_before_opening_a_connection(
        monkeypatch, target_type, error, message):
    from .rows import audit

    target = {
        "unowned": "pl_cf_w1c_audit_from_another_run",
        "non_audit": _bank.name("pl_cf_w1c_non_audit"),
        "production": audit._name("protected"),
    }[target_type]
    monkeypatch.setenv("_PSEUDOLIFE_PRODUCTION_DB", audit._name("protected"))
    connect = MagicMock()
    monkeypatch.setattr(_bank, "connect", connect)
    with pytest.raises(error, match=message):
        audit._copy(audit._name("legacy"), target)
    connect.assert_not_called()


def assert_refused(result, parameter):
    assert result.returncode == 2
    assert not result.stdout
    assert b"defer" not in result.stderr.lower()
    assert parameter.encode() in result.stderr
    assert SECRET.encode() not in (result.stdout or b"") + result.stderr


def test_agent_refusal_leaves_no_archive(candidate, bank, tmp_path):
    out = tmp_path / "out.jsonl"
    result = invoke(candidate, tmp_path, "export", "--agent", "ffffffff", "--out", out,
                    dsn=bank[1])
    assert result.returncode == 2
    assert not out.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    near = invoke(candidate, tmp_path, "export", "--agent", "example-agent", "--out", out,
                  dsn=bank[1])
    assert near.returncode == 0
    assert len(out.read_bytes().splitlines()) == 1


def test_successful_archive_bytes_match_baseline(candidate, bank, tmp_path):
    body, salt = "synthetic committed body 雪", "01" * 16
    commitment = hashlib.sha256(bytes.fromhex(salt) + body.encode()).hexdigest()
    sent = event(seq=2, event="send", message_id="message", body=body, body_salt=salt,
                 prev_hash=event()["hash"], payload=json.dumps({"text_commitment": commitment}))
    with _bank.connect(bank[0]) as conn:
        columns = list(sent)
        conn.execute(f"INSERT INTO coordination_events ({','.join(columns)}) "
                     f"VALUES ({','.join(['%s'] * len(columns))})", list(sent.values()))
        conn.commit()
    out = tmp_path / "out.jsonl"
    result = invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1])
    assert result.returncode == 0
    # The exporter orders payload before created_at, as the stored column order does.
    expected = b""
    for row in [event(), sent]:
        row["payload"] = json.loads(row["payload"])
        expected += (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    assert out.read_bytes() == expected
    baseline = os.environ.get("PSEUDOLIFE_AUDIT_BASELINE")
    if baseline:
        old = tmp_path / "baseline.jsonl"
        result = invoke(baseline, tmp_path, "export", "--out", old, dsn=bank[1])
        assert result.returncode == 0
        assert out.read_bytes() == old.read_bytes()


@pytest.mark.parametrize("mode", ["verify", "export", "redact", "stats"])
def test_bad_database_setting_has_actionable_secret_safe_refusal(candidate, tmp_path, mode):
    args = [mode]
    if mode == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    result = invoke(candidate, tmp_path, *args, dsn=f"invalid:{SECRET}")
    assert_refused(result, "PSEUDOLIFE_MCP_DATABASE_URL")


@pytest.mark.parametrize("mode", ["verify", "export", "redact", "stats"])
def test_nonunicode_database_setting_refuses_without_fallback(candidate, bank, tmp_path, mode):
    args = [mode]
    if mode == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    result = invoke(candidate, tmp_path, *args, dsn=SECRET + "\udcff")
    assert_refused(result, "PSEUDOLIFE_MCP_DATABASE_URL")
    near = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert near.returncode == (1 if mode == "redact" else 0)


@pytest.mark.parametrize("field,value,parameter", [
    ("seq", 2**63, "seq"),
    ("created_at", 1e309, "created_at"),
    ("principal", "surrogate\ud800" + SECRET, "principal"),
    ("body", "surrogate\ud800" + SECRET, "body"),
])
@pytest.mark.parametrize("mode", ["verify", "stats"])
def test_archive_value_refusals_do_not_disclose_values(candidate, tmp_path, field, value, parameter, mode):
    row = event()
    row["payload"] = json.loads(row["payload"])
    row[field] = value
    archive = tmp_path / "input.jsonl"
    archive.write_text(json.dumps(row), encoding="utf-8")
    result = invoke(candidate, tmp_path, mode, "--input", archive)
    assert_refused(result, parameter)
    row = event()
    row["payload"] = json.loads(row["payload"])
    archive.write_text(json.dumps(row), encoding="utf-8")
    near = invoke(candidate, tmp_path, mode, "--input", archive)
    assert near.returncode == 0


@pytest.mark.parametrize("reason", ["zero\u200bwidth", "private\ue000", "unassigned\u0378"])
def test_unclassified_reason_is_refused_without_redaction(candidate, bank, tmp_path, reason):
    with _bank.connect(bank[0]) as conn:
        before = conn.execute("SELECT * FROM coordination_events ORDER BY seq").fetchall()
    result = invoke(candidate, tmp_path, "redact", "--message-id", "example",
                    "--reason", reason + SECRET, dsn=bank[1])
    assert_refused(result, "--reason")
    with _bank.connect(bank[0]) as conn:
        assert conn.execute("SELECT * FROM coordination_events ORDER BY seq").fetchall() == before
    near = invoke(candidate, tmp_path, "redact", "--message-id", "example",
                  "--reason", "wrong paste 雪", dsn=bank[1])
    assert near.returncode == 1
    assert b"message_not_found" in near.stdout


def test_query_failure_names_operation_without_database_detail(candidate, bank, tmp_path):
    with _bank.connect(bank[0]) as conn:
        conn.execute("ALTER TABLE coordination_events DROP COLUMN hash")
        conn.commit()
    result = invoke(candidate, tmp_path, "verify", dsn=bank[1])
    assert_refused(result, "audit")
    with _bank.connect(bank[0]) as conn:
        conn.execute("ALTER TABLE coordination_events ADD COLUMN hash text")
        conn.execute("UPDATE coordination_events SET hash=%s", (event()["hash"],))
        conn.commit()
    assert invoke(candidate, tmp_path, "verify", dsn=bank[1]).returncode == 0


@pytest.mark.parametrize("mode", ["verify", "export", "redact"])
def test_connection_failure_has_no_deferral(candidate, tmp_path, mode):
    args = [mode]
    if mode == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    result = invoke(candidate, tmp_path, *args,
                    dsn=f"postgresql://example:{SECRET}@127.0.0.1:1/pl_cf_absent")
    assert_refused(result, "PSEUDOLIFE_MCP_DATABASE_URL")


def test_connection_options_are_native(candidate, bank, tmp_path):
    result = invoke(candidate, tmp_path, "verify",
                    dsn=bank[1] + "?application_name=audit&connect_timeout=5")
    assert result.returncode == 0
    assert json.loads(result.stdout)["ok"] is True


@pytest.mark.parametrize("mode", ["verify", "export", "stats", "redact"])
def test_ambient_control_is_named_and_not_echoed(candidate, bank, tmp_path, mode):
    args = [mode]
    if mode == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    result = invoke(candidate, tmp_path, *args, dsn=bank[1], extra={"PGOPTIONS": SECRET})
    assert_refused(result, "PGOPTIONS")
    near = invoke(candidate, tmp_path, "verify", dsn=bank[1])
    assert near.returncode == 0


@pytest.mark.parametrize("prefix", ["PGſSL", "PGTLſ"])
def test_unicode_folded_ambient_name_never_echoes_key(candidate, bank, tmp_path, prefix):
    result = invoke(candidate, tmp_path, "verify", dsn=bank[1],
                    extra={prefix + SECRET: SECRET})
    if os.name == "nt":
        assert_refused(result, prefix.upper() + "*")
    else:
        # These names are not PostgreSQL controls on a case-sensitive OS.
        assert result.returncode == 0
    assert SECRET.encode() not in result.stdout + result.stderr
    near = invoke(candidate, tmp_path, "verify", dsn=bank[1],
                  extra={prefix[:-1] + "X": SECRET})
    assert near.returncode == 0


def test_stats_surrogate_payload_is_refused_without_echo(candidate, tmp_path):
    row = event()
    row["payload"] = {"metadata": "\ud800" + SECRET}
    archive = tmp_path / "input.jsonl"
    archive.write_text(json.dumps(row), encoding="utf-8")
    assert_refused(invoke(candidate, tmp_path, "stats", "--input", archive), "payload")
    row["payload"] = {"metadata": "雪"}
    archive.write_text(json.dumps(row), encoding="utf-8")
    assert invoke(candidate, tmp_path, "stats", "--input", archive).returncode == 0


@pytest.mark.parametrize("mode", ["verify", "export"])
def test_nonfinite_bank_timestamp_is_refused(candidate, bank, tmp_path, mode):
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET created_at='Infinity'::float8, body=%s", (SECRET,))
        conn.commit()
    args = [mode]
    out = tmp_path / "out.jsonl"
    if mode == "export":
        args += ["--out", out]
    result = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert_refused(result, "created_at")
    assert not out.exists()
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET created_at=%s, body=NULL", (event()["created_at"],))
        conn.commit()
    assert invoke(candidate, tmp_path, *args, dsn=bank[1]).returncode == 0


def test_bank_payload_surrogate_stats_refusal(candidate, bank, tmp_path):
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET payload=%s", (json.dumps({"value": "\ud800" + SECRET}),))
        conn.commit()
    assert_refused(invoke(candidate, tmp_path, "stats", "--since", "0", dsn=bank[1]), "payload")
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET payload=%s", (event()["payload"],))
        conn.commit()
    assert invoke(candidate, tmp_path, "stats", "--since", "0", dsn=bank[1]).returncode == 0


def test_sequence_exhaustion_rolls_back_every_redaction_change(candidate, bank, tmp_path):
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET seq=%s", (2**63 - 1,))
        conn.execute("INSERT INTO coordination_messages VALUES (%s,%s,'original','sender','recipient','p','t',1e12)",
                     ("example", SECRET))
        conn.commit()
        before = conn.execute("SELECT * FROM coordination_messages").fetchall()
    result = invoke(candidate, tmp_path, "redact", "--message-id", "example", "--reason", "wrong paste", dsn=bank[1])
    assert_refused(result, "seq")
    with _bank.connect(bank[0]) as conn:
        assert conn.execute("SELECT * FROM coordination_messages").fetchall() == before
        assert conn.execute("SELECT count(*) FROM coordination_events").fetchone()[0] == 1
        conn.execute("UPDATE coordination_events SET seq=1")
        conn.commit()
    near = invoke(candidate, tmp_path, "redact", "--message-id", "example", "--reason", "wrong paste", dsn=bank[1])
    assert near.returncode == 0
    assert SECRET.encode() not in near.stdout + near.stderr


@pytest.mark.parametrize("operation", ["verify", "export", "redact"])
def test_database_column_decode_refusal_is_safe(candidate, bank, tmp_path, operation):
    with _bank.connect(bank[0]) as conn:
        conn.execute("ALTER TABLE coordination_events ALTER COLUMN seq TYPE text USING seq::text")
        conn.commit()
    args = [operation]
    if operation == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    assert_refused(invoke(candidate, tmp_path, *args, dsn=bank[1]), "audit")
    with _bank.connect(bank[0]) as conn:
        conn.execute("ALTER TABLE coordination_events ALTER COLUMN seq TYPE bigint USING seq::bigint")
        conn.commit()
    near = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert near.returncode == (1 if operation == "redact" else 0)


def test_agent_lookup_failure_leaves_no_archive(candidate, bank, tmp_path):
    with _bank.connect(bank[0]) as conn:
        conn.execute("DROP TABLE coordination_agents")
        conn.commit()
    out = tmp_path / "out.jsonl"
    args = ["export", "--agent", "ffffffff", "--out", out]
    result = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert_refused(result, "--agent")
    assert b"schema" in result.stderr
    assert list(tmp_path.iterdir()) == []
    with _bank.connect(bank[0]) as conn:
        conn.execute("CREATE TABLE coordination_agents (agent_id text)")
        conn.commit()
    near = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert near.returncode == 2
    assert b"no agent id starts with" in near.stderr


@pytest.mark.parametrize("name", ["postgresql.crt", "postgresql.key", "root.crl"])
def test_implicit_tls_file_refusal_names_the_file_and_fix(candidate, bank, tmp_path, name):
    directory = tmp_path / ("postgresql" if os.name == "nt" else ".postgresql")
    directory.mkdir()
    implicit = directory / name
    implicit.write_text(SECRET)
    result = invoke(candidate, tmp_path, "verify", dsn=bank[1])
    assert_refused(result, name)
    assert b"file" in result.stderr and b"move" in result.stderr
    implicit.unlink()
    assert invoke(candidate, tmp_path, "verify", dsn=bank[1]).returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask")
@pytest.mark.parametrize("failure", [False, True])
def test_write_only_umask_publishes_or_removes_staging(candidate, bank, tmp_path, failure):
    out = tmp_path / "out.jsonl"
    args = ["export", "--out", out]
    if failure:
        args += ["--agent", "ffffffff"]
    result = invoke(candidate, tmp_path, *args, dsn=bank[1], umask=0o400)
    assert result.returncode == (2 if failure else 0)
    assert not list(tmp_path.glob(".board-audit-*.tmp"))
    assert out.exists() != failure
    if not failure:
        assert out.stat().st_mode & 0o777 == 0o200
        out.chmod(0o600)
        assert len(out.read_bytes().splitlines()) == 1
    near = tmp_path / "near.jsonl"
    assert invoke(candidate, tmp_path, "export", "--out", near, dsn=bank[1], umask=0o077).returncode == 0


def test_staging_error_names_out_and_leaves_no_file(candidate, bank, tmp_path):
    missing = tmp_path / "missing"
    out = missing / "out.jsonl"
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET payload=%s", ("invalid " + SECRET,))
        conn.commit()
    result = invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1])
    assert_refused(result, "--out")
    assert b"parent directory does not exist" in result.stderr
    assert b"create the parent directory" in result.stderr
    assert not out.exists()
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET payload=%s", (event()["payload"],))
        conn.commit()
    missing.mkdir()
    assert invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1]).returncode == 0


@pytest.mark.skipif(os.name == "posix" and os.geteuid() == 0,
                    reason="root bypasses directory write permissions")
def test_unwritable_export_parent_has_permission_fix(candidate, bank, tmp_path):
    parent = tmp_path / "private"
    parent.mkdir()
    out = parent / "out.jsonl"
    if os.name == "nt":
        import csv
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                  check=True, capture_output=True, text=True)
        sid = next(csv.reader(identity.stdout.splitlines()))[1]
        deny = subprocess.run(["icacls", str(parent), "/deny", f"*{sid}:(WD,AD)"],
                              capture_output=True)
        assert deny.returncode == 0
    else:
        parent.chmod(0o500)
    try:
        result = invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1])
        assert_refused(result, "--out")
        assert b"permission denied" in result.stderr
        assert b"choose a destination writable by your user" in result.stderr
        assert not out.exists()
        assert not list(parent.iterdir())
    finally:
        if os.name == "nt":
            reset = subprocess.run(["icacls", str(parent), "/remove:d", f"*{sid}"],
                                   capture_output=True)
            assert reset.returncode == 0
        else:
            parent.chmod(0o700)
    assert invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1]).returncode == 0


def test_input_verify_closed_stdout_has_safe_refusal(candidate, tmp_path):
    row = event()
    row["payload"] = json.loads(row["payload"])
    archive = tmp_path / "input.jsonl"
    archive.write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = invoke(candidate, tmp_path, "verify", "--input", archive, closed_stdout=True)
    assert_refused(result, "stdout")
    assert b"writable destination" in result.stderr
    near = invoke(candidate, tmp_path, "verify", "--input", archive)
    assert near.returncode == 0
    assert json.loads(near.stdout)["ok"] is True


@pytest.mark.parametrize("mode", ["verify", "redact", "export"])
def test_closed_stdout_refuses_without_claiming_completion(candidate, bank, tmp_path, mode):
    args = [mode]
    if mode == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    result = invoke(candidate, tmp_path, *args, dsn=bank[1], closed_stdout=True)
    assert result.returncode == 2
    assert b"stdout" in result.stderr
    assert b"defer" not in result.stderr
    assert SECRET.encode() not in result.stderr
    if mode == "export":
        assert b"incomplete" in result.stderr
    near = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert near.returncode == (1 if mode == "redact" else 0)


@pytest.mark.parametrize("option", ["options", "sslmode", "connect_timeout"])
def test_invalid_connection_option_is_secret_safe(candidate, bank, tmp_path, option):
    result = invoke(candidate, tmp_path, "verify", dsn=bank[1] + f"?{option}={SECRET}")
    assert_refused(result, "PSEUDOLIFE_MCP_DATABASE_URL")
    near = invoke(candidate, tmp_path, "verify", dsn=bank[1] + "?sslmode=disable&connect_timeout=5")
    assert near.returncode == 0


@pytest.mark.parametrize("timestamp", [1e36, -1e36])
def test_retention_timestamp_outside_verified_range_is_refused(candidate, tmp_path, timestamp):
    row = event(seq=2, event="audit_prune", actor="daemon", created_at=timestamp,
                payload=json.dumps(dict(through_seq=1, through_hash="0" * 64,
                                        retention_days=1, cutoff=0), separators=(",", ":")))
    row["payload"] = json.loads(row["payload"])
    archive = tmp_path / "input.jsonl"
    archive.write_text(json.dumps(row), encoding="utf-8")
    assert_refused(invoke(candidate, tmp_path, "verify", "--input", archive), "created_at")
    now = 1700000000.0
    cutoff = int((now - 86400) // 86400 * 86400)
    row = event(seq=2, event="audit_prune", actor="daemon", created_at=now,
                payload=json.dumps(dict(through_seq=1, through_hash="0" * 64,
                                        retention_days=1, cutoff=cutoff), separators=(",", ":")))
    row["payload"] = json.loads(row["payload"])
    archive.write_text(json.dumps(row), encoding="utf-8")
    assert invoke(candidate, tmp_path, "verify", "--input", archive).returncode == 0


def test_nonunicode_argument_is_refused_without_echo(candidate, tmp_path):
    argument = "\ud800" if os.name == "nt" else b"\xff"
    assert_refused(invoke(candidate, tmp_path, "verify", argument), "arguments")
    assert invoke(candidate, tmp_path, "verify", "--help").returncode == 0


def test_first_retained_timestamp_outside_comparison_range_is_refused(candidate, tmp_path):
    archive = tmp_path / "input.jsonl"
    now = 1700000000.0
    for timestamp, refused in [(1e32, True), (now, False)]:
        first = event(seq=2, created_at=timestamp)
        prune = event(seq=3, event="audit_prune", actor="daemon", created_at=now,
                      prev_hash=first["hash"], payload=json.dumps(dict(
                          through_seq=1, through_hash="0" * 64, retention_days=1,
                          cutoff=int((now - 86400) // 86400 * 86400))))
        for row in [first, prune]:
            row["payload"] = json.loads(row["payload"])
        archive.write_text(json.dumps(first) + "\n" + json.dumps(prune), encoding="utf-8")
        result = invoke(candidate, tmp_path, "verify", "--input", archive)
        if refused:
            assert_refused(result, "created_at")
        else:
            assert result.returncode == 0


def test_late_payload_failure_keeps_stdout_prefix_but_never_publishes_file(candidate, bank, tmp_path):
    with _bank.connect(bank[0]) as conn:
        conn.execute("INSERT INTO coordination_events SELECT 2,event,actor,principal,agent_id,"
                     "recipient_agent_id,project,task,message_id,%s,created_at,hlc,prev_hash,hash,body,body_salt "
                     "FROM coordination_events WHERE seq=1", ('not-json-' + SECRET,))
        conn.commit()
    out = tmp_path / "out.jsonl"
    result = invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1])
    assert_refused(result, "payload")
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []
    streamed = invoke(candidate, tmp_path, "export", dsn=bank[1])
    assert streamed.returncode == 2
    assert len(streamed.stdout.splitlines()) == 1
    assert b"incomplete" in streamed.stderr
    assert SECRET.encode() not in streamed.stdout + streamed.stderr


def test_existing_and_locked_destinations_are_never_replaced(candidate, tmp_path):
    out = tmp_path / "out.jsonl"
    out.write_bytes(b"existing archive\n")
    handle = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        handle = create(str(out), 0x80000000, 0, None, 3, 0x80, None)
        assert handle != wintypes.HANDLE(-1).value
    try:
        result = invoke(candidate, tmp_path, "export", "--out", out, dsn="not-a-dsn")
        assert result.returncode == 2
        assert b"exists; export never replaces a file" in result.stderr
        assert b"choose an unused --out path" in result.stderr
    finally:
        if handle is not None:
            close = ctypes.WinDLL("kernel32").CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            assert close(handle)
    assert out.read_bytes() == b"existing archive\n"


@pytest.mark.parametrize("code,racing_destination", [(0, False), (23, False), (0, True)])
def test_container_export_is_invisible_until_success(candidate, tmp_path, code, racing_destination):
    import shlex
    import sys
    import threading
    import time

    script = tmp_path / "docker.py"
    script.write_text('''import os, sys, time
from pathlib import Path
home = Path(os.environ['HOME'])
if sys.argv[1] == 'inspect':
    print('true' if sys.argv[3] == '{{.State.Running}}' else 'rust')
    raise SystemExit(0)
sys.stdout.buffer.write(b'{"container":"fixture"}\\n')
sys.stdout.buffer.flush()
(home / 'ready').touch()
until = time.monotonic() + 15
while not (home / 'release').exists() and time.monotonic() < until:
    time.sleep(.01)
raise SystemExit(int(os.environ['FIXTURE_EXIT']))
''', encoding="utf-8")
    wrapper = tmp_path / ("docker.cmd" if os.name == "nt" else "docker.sh")
    wrapper.write_text((f'@echo off\n"{sys.executable}" "{script}" %*\nexit /b %errorlevel%\n'
                        if os.name == "nt" else
                        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'),
                       encoding="utf-8")
    wrapper.chmod(0o700)
    result = []
    out = tmp_path / "out.jsonl"
    worker = threading.Thread(target=lambda: result.append(invoke(
        candidate, tmp_path, "export", "--out", out,
        extra={"PSEUDOLIFE_DOCKER": str(wrapper), "FIXTURE_EXIT": str(code)})))
    worker.start()
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "ready").exists() and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        ready = (tmp_path / "ready").exists()
        premature = out.exists()
        if racing_destination and not premature:
            out.write_bytes(b"racing destination\n")
    finally:
        (tmp_path / "release").touch()
        worker.join(timeout=35)
    assert ready and result
    assert not premature, "container output was published before its exit status"
    assert result[0].returncode == (2 if racing_destination else code)
    assert out.exists() == (code == 0)
    if racing_destination:
        assert out.read_bytes() == b"racing destination\n"
        assert b"publication" in result[0].stderr
        assert b"destination already exists" in result[0].stderr
        assert b"choose an unused --out path" in result[0].stderr
    elif code == 0:
        assert out.read_bytes() == b'{"container":"fixture"}\n'
    assert not list(tmp_path.glob(".board-audit-*.tmp"))
