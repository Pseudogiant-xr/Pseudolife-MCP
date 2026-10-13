"""Additive public prompt-arrival corpus; no daemon, bank, or hook routing."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .cli_process import BYTE_POLICY, reset_home, snapshot
from .harness import compare
from .harness import isolated_env, run_cli

THREAD = "00000000-0000-0000-0000-000000000001"
NONCE = "a" * 32


def child_binding_source():
    return '''import atexit,sys,pathlib,hashlib,json,types,os
@atexit.register
def bind():
 result={'executable':sys.executable,'prefix':sys.prefix,'modules':{}}
 for name in ('__main__','pseudolife_memory.codex_doorbell_state','pseudolife_memory.private_state','pseudolife_memory.codex_coordination','pseudolife_memory.coordination_identity','pseudolife_memory.os_lock'):
  module=sys.modules.get(name)
  if module is None or not getattr(module,'__file__',None):continue
  path=pathlib.Path(module.__file__);raw=path.read_bytes();compiled=compile(raw,str(path),'exec')
  expected={c.co_name:c for c in compiled.co_consts if isinstance(c,types.CodeType)}
  checks={n:expected[n]==v.__code__ for n,v in vars(module).items() if isinstance(v,types.FunctionType) and v.__module__==name and n in expected}
  for n,v in vars(module).items():
   if isinstance(v,type) and v.__module__==name and n in expected:
    methods={c.co_name:c for c in expected[n].co_consts if isinstance(c,types.CodeType)}
    for m,f in vars(v).items():
     if isinstance(f,staticmethod):f=f.__func__
     if isinstance(f,types.FunctionType) and m in methods:
      original=getattr(f,'__wrapped__',f)
      checks[n+'.'+m]=methods[m]==original.__code__
  result['modules'][name]={'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'bytecode':checks}
 pathlib.Path(os.environ['DOORBELL_BINDING']).write_text(json.dumps(result))
'''


def cases(notice_text):
    record = dict(version=2, thread_id=THREAD, nonce=NONCE, count=1,
                  recipient_state=None, expires_at=4102444800, expiry_basis="message",
                  text=notice_text(1, NONCE, version=2))
    payload = {"session_id": THREAD, "prompt": record["text"]}
    result = []

    def add(name, *, pending=record, value=payload, raw=None, receipt=None,
            writes=False, locks=True, argv=(), deltas=None, state_root="digests", digest_variant=None,
            lock_bytes=None, pending_raw=None, candidate=None, oracle_failure=None):
        result.append(dict(id=name, pending=copy.deepcopy(pending), pending_raw=pending_raw,
                           candidate_expectation=candidate or {}, oracle_failure=oracle_failure,
                           stdin_b64=base64.b64encode(raw if raw is not None else
                           json.dumps(value, separators=(",", ":")).encode()).decode(),
                           receipt_b64=None if receipt is None else base64.b64encode(receipt).decode(),
                           writes=writes, locks=locks, argv=list(argv), environment_deltas=deltas or {},
                           state_root=state_root, digest_variant=digest_variant,
                           lock_b64=None if lock_bytes is None else base64.b64encode(lock_bytes).decode()))

    add("version2-positive", writes=True)
    for version, count, state, maintainer in [(1, 1, None, 0), (2, 2, "unknown", 0),
                                             (3, 1, None, 1), (3, 2, "unknown", 2)]:
        p = dict(record, version=version, count=count, recipient_state=state)
        if version == 3:
            p["maintainer"] = maintainer
        p["text"] = notice_text(count, NONCE, version=version, recipient_state=state,
                               maintainer=maintainer)
        add(f"format-v{version}-{count}-{state}", pending=p,
            value=dict(payload, prompt=p["text"]), writes=True)
    add("expired-positive", pending=dict(record, expires_at=0), writes=True)
    add("existing-empty-lock", writes=True, lock_bytes=b"")
    add("existing-nonutf8-lock", writes=True, lock_bytes=b"\xffprior")
    add("overwrite-any-utf8-receipt", receipt=b"old nonce\r\n", writes=True)
    add("ignored-tail", writes=True, argv=("--help", "unexpected", "--bad"))
    add("ignored-opaque-tail", writes=True, argv=("\ud800" if os.name == "nt" else "\udcff",))
    add("default-digest-dir", writes=True, deltas={"PSEUDOLIFE_DIGEST_DIR": None},
        state_root=".pseudolife-mcp/digests")
    add("empty-digest-dir", writes=True, deltas={"PSEUDOLIFE_DIGEST_DIR": ""},
        state_root=".pseudolife-mcp/digests")
    add("tilde-digest-dir", writes=True, deltas={"PSEUDOLIFE_DIGEST_DIR": "~/digests"})
    add("relative-digest-dir", writes=True, digest_variant="relative")
    if os.name == "nt":
        add("tilde-opaque-user", writes=True, digest_variant="opaque-user",
            state_root="other\ud800/digests",
            deltas={"USERNAME": "home", "PSEUDOLIFE_DIGEST_DIR": "~other\ud800/digests"})
    add("extra-payload", value=dict(payload, extra=[float("nan"), "\ud800"]), writes=True)
    add("extra-record", pending=dict(record, extra=[float("inf"), "\ud800"]), writes=True)
    add("duplicate-last-wins", raw=(json.dumps(dict(payload, prompt="wrong"))[:-1] +
                                    ',"prompt":' + json.dumps(record["text"]) + '}').encode(), writes=True)
    add("nil-session", value=dict(payload, session_id="00000000-0000-0000-0000-000000000000"))
    for field, values in [("session_id", [None, 1, True, THREAD.upper().replace("001", "00A"),
                                         "urn:uuid:" + THREAD, "{" + THREAD + "}", THREAD.replace("-", "")]),
                          ("prompt", [None, 1, False, [], {}])]:
        add("missing-" + field, value={k: v for k, v in payload.items() if k != field}, locks=False)
        for index, value in enumerate(values):
            add(f"invalid-{field}-{index}", value=dict(payload, **{field: value}), locks=False)
    for suffix in [" ", "\n", "é", "\ud800"]:
        add("prompt-suffix-" + repr(suffix), value=dict(payload, prompt=record["text"] + suffix))
    add("ordinary-prompt", value=dict(payload, prompt="continue"))
    for index, raw in enumerate([b"", b"{", b"null", b"[]", b'"scalar"', b"\xef\xbb\xbf{}",
                                 b"\xff", b"{}junk", b'{"session_id":NaN}']):
        add(f"input-noop-{index}", raw=raw, locks=False)
    raw = json.dumps(payload).encode()
    add("stdin-65536", raw=raw + b" " * (65536-len(raw)), writes=True)
    add("stdin-65537", raw=raw + b" " * (65537-len(raw)), locks=False)
    for digits in (4300, 4301):
        add(f"python-integer-limit-{digits}", raw=raw[:-1]+b',"extra":'+b'9'*digits+b'}',
            writes=digits == 4300, locks=digits == 4300)
    for limit, digits, writes in [("640", 640, True), ("640", 641, False),
                                  ("0", 4301, True), ("0", 65000, True),
                                  ("5000", 4301, True), ("5000", 5000, True),
                                  ("5000", 5001, False)]:
        add(f"configured-integer-limit-{limit}-{digits}",
            raw=raw[:-1]+b',"extra":'+b'9'*digits+b'}', writes=writes, locks=writes,
            deltas={"PYTHONINTMAXSTRDIGITS": limit})
    add("unused-float-overflow", raw=raw[:-1]+b',"extra":1e99999}', writes=True)
    add("missing-pending", pending=None)
    for field, values in [("thread_id", ["wrong"]), ("count", [True, 0, -1, 1.0]),
                          ("nonce", ["A"*32, "a"*31, None]), ("version", [True, 0, 4, 2.0]),
                          ("recipient_state", [False, "busy", []]), ("text", ["wrong", None]),
                          ("maintainer", [0, 1, True]), ("expires_at", [None, True, -1, float("nan"), float("inf")]),
                          ("expiry_basis", [None, "other", "legacy_upper_bound"])]:
        for index, value in enumerate(values):
            add(f"pending-{field}-{index}", pending=dict(record, **{field: value}))
    add("missing-expiry", pending={k: v for k, v in record.items() if k != "expires_at"})
    p = dict(record, version=3, maintainer=1, text=notice_text(1, NONCE, version=3, maintainer=1))
    add("v3-missing-maintainer", pending={k: v for k, v in p.items() if k != "maintainer"})
    for invalid in (0, 2, True, 1.0):
        add("v3-invalid-maintainer-" + repr(invalid), pending=dict(p, maintainer=invalid))
    add("v1-rejects-unknown", pending=dict(record, version=1, recipient_state="unknown"))
    p = dict(record, expiry_basis="legacy_upper_bound", expires_at=86400, legacy_first_seen=0)
    add("legacy-basis-versioned", pending=p, writes=True)
    add("invalid-legacy-basis", pending=dict(p, expires_at=86401))
    p = dict(record, expiry_basis="legacy_upper_bound", legacy_first_seen=2**54,
             expires_at=2**54+86400)
    add("exact-large-integer-expiry", pending=p, writes=True)
    add("distinct-large-integer-expiry", pending=dict(p, expires_at=p["expires_at"]+1))
    add("large-mixed-float-expiry", pending=dict(p, expires_at=float(p["expires_at"])), writes=True)
    add("invalid-receipt-utf8", receipt=b"\xff")
    add("invalid-receipt-length", receipt=b"x"*8193)
    add("unicode-receipt-8192", receipt=("é"*8192).encode(), writes=True)
    add("unicode-receipt-8193", receipt=("é"*8193).encode())
    add("translated-receipt-8192", receipt=b"\r\n"*8192, writes=True)
    for target in (8192, 8193):
        p = dict(record, padding="")
        p["padding"] = "é"*(target-len(json.dumps(p, ensure_ascii=False, separators=(",", ":")))-1)
        add(f"unicode-pending-{target}", pending=p, writes=target == 8192)
    for digits in (20, 120):
        count = int("9"*digits)
        p = dict(record, count=count, text=notice_text(count, NONCE, version=2))
        add(f"unbounded-count-{digits}", pending=p, value=dict(payload, prompt=p["text"]), writes=True)
    # Each policy keeps the Python input and observation intact; only the native
    # expectation changes under the four explicitly approved producer domains.
    standard_input = dict(writes=False, locks=False, substitution="doorbell-standard-json")
    standard_pending = dict(writes=False, substitution="doorbell-standard-json")
    raw_payload = json.dumps(payload).encode()
    for token in [b"NaN", b"Infinity", b"-Infinity", b"1e99999", b'"\\ud800"', b'"\\udfff"']:
        suffix = token.decode()
        add("standard-input-" + suffix,
            raw=raw_payload[:-1] + b',"extra":' + token + b'}', writes=True,
            candidate=standard_input)
        add("standard-pending-" + suffix, writes=True, receipt=b"prior\n",
            pending_raw=json.dumps(record)[:-1] + ',"extra":' + suffix + '}',
            candidate=standard_pending)
    add("overwritten-float-overflow", raw=raw_payload[:-1] + b',"extra":1e99999,"extra":0}',
        writes=True, candidate=standard_input)
    add("surrogate-pair-extra", value=dict(payload, extra="😀"), writes=True)
    for nested_arrays in [126, 127, 128]:
        raw_extra = "[" * nested_arrays + "0" + "]" * nested_arrays
        supported = nested_arrays < 127  # Root object also consumes serde's budget.
        add(f"native-depth-input-{nested_arrays}", writes=True,
            raw=raw_payload[:-1] + b',"extra":' + raw_extra.encode() + b'}',
            candidate=dict(writes=supported, locks=supported, substitution="doorbell-native-nesting-bound"))
        add(f"native-depth-pending-{nested_arrays}", writes=True, receipt=b"prior\n",
            pending_raw=json.dumps(record)[:-1] + ',"extra":' + raw_extra + '}',
            candidate=dict(writes=supported, substitution="doorbell-native-nesting-bound"))
    for digits in [4301, 5001]:
        add(f"ignored-pending-integer-{digits}", receipt=b"prior\n",
            pending_raw=json.dumps(record)[:-1] + ',"extra":' + "9" * digits + '}',
            candidate=dict(writes=True, substitution="doorbell-ignore-python-digit-limit"))
    for count in [2**64 - 1, 2**64]:
        p = dict(record, count=count, text=notice_text(count, NONCE, version=2))
        add(f"count-u64-{count}", pending=p, value=dict(payload, prompt=p["text"]), writes=True,
            candidate=dict(writes=count < 2**64, substitution="doorbell-bounded-relevant-numbers"))
        p.update(version=3, maintainer=count, text=notice_text(count, NONCE, version=3, maintainer=count))
        add(f"maintainer-u64-{count}", pending=p, value=dict(payload, prompt=p["text"]), writes=True,
            candidate=dict(writes=count < 2**64, substitution="doorbell-bounded-relevant-numbers"))
    for expiry in [2**64 - 1, 2**64]:
        add(f"timestamp-u64-{expiry}", pending=dict(record, expires_at=expiry), writes=True,
            candidate=dict(writes=expiry < 2**64, substitution="doorbell-bounded-relevant-numbers"))
    p = dict(record, expiry_basis="legacy_upper_bound", legacy_first_seen=2**64 - 1,
             expires_at=2**64 - 1 + 86400)
    add("checked-day-overflow", pending=p, writes=True, receipt=b"prior\n",
        candidate=dict(writes=False, substitution="doorbell-bounded-relevant-numbers"))
    add("huge-timestamp-quiet-refusal", pending=dict(record, expires_at=10**400), receipt=b"prior\n",
        oracle_failure="OverflowError: int too large to convert to float",
        candidate=dict(writes=False, oracle_failure=None, substitution="doorbell-bounded-relevant-numbers"))
    add("private-number-key-extra", value=dict(payload, extra={"$serde_json::private::Number": "not a number"}), writes=True)
    for field in ["count", "version", "expires_at"]:
        add("object-cannot-impersonate-" + field,
            pending=dict(record, **{field: {"$serde_json::private::Number": "1"}}))
    return result


def candidate_case(case):
    """Native expectations for the four approved producer substitutions."""
    result = copy.deepcopy(case)
    name = case["id"]
    if name in {"extra-payload", "unused-float-overflow", "prompt-suffix-" + repr("\ud800")}:
        result.update(writes=False, locks=False, substitution="doorbell-standard-json")
    elif name == "extra-record":
        result.update(writes=False, substitution="doorbell-standard-json")
    elif name.startswith(("python-integer-limit-", "configured-integer-limit-")):
        result.update(writes=True, locks=True, substitution="doorbell-ignore-python-digit-limit")
    elif name.startswith("unbounded-count-"):
        result.update(writes=False, substitution="doorbell-bounded-relevant-numbers")
    result.update(case.get("candidate_expectation", {}))
    return result


def assert_effect(case, observation, key, pre):
    assert observation["stdout_b64"] == ""
    if case.get("oracle_failure"):
        assert observation["exit_code"] == 1
        assert base64.b64decode(observation["stderr_b64"]).decode().splitlines()[-1] == case["oracle_failure"]
    else:
        assert observation["exit_code"] == 0
        assert observation["stderr_b64"] == ""
    post = observation["post_files_b64"]
    root = case.get("state_root", "digests") + "/"
    pending = root + key + ".bell-pending"
    assert post.get(pending) == pre.get(pending)
    receipt = root + key + ".bell-prompt-seen"
    assert post.get(receipt) == (base64.b64encode((NONCE+"\n").encode()).decode()
                                if case["writes"] else pre.get(receipt))
    lock_key = hashlib.sha256(b"00000000-0000-0000-0000-000000000000").hexdigest() if case["id"] == "nil-session" else key
    lock = root + lock_key + ".bell-lock"
    assert post.get(lock) == ((case.get("lock_b64") or base64.b64encode(b"0").decode())
                              if case["locks"] else None)
    assert set(post) == set(pre) | ({lock} if case["locks"] else set()) | ({receipt} if case["writes"] else set())


def controls(observation):
    result = {}
    for field in ("exit_code", "stdout_b64", "stderr_b64", "post_files_b64"):
        changed = copy.deepcopy(observation)
        if field == "exit_code":
            changed[field] += 1
        elif field == "post_files_b64":
            name = next(iter(changed[field]), "mutation")
            changed[field][name] = base64.b64encode(b"mutation").decode()
        else:
            changed[field] = base64.b64encode(b"mutation").decode()
        differences = compare(observation, changed, BYTE_POLICY)
        rejected = bool(differences) and any(d["path"].startswith("/" + field) for d in differences)
        result[field] = dict(mutation_arm="candidate", rejected=rejected,
                             differences=differences, mutated_field=changed[field])
    assert all(control["rejected"] for control in result.values())
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oracle-python", required=True)
    parser.add_argument("--oracle-source", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--first-only", action="store_true")
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args()
    import sys
    sys.path.insert(0, str(args.oracle_source))
    from pseudolife_memory.codex_doorbell_state import PendingNotice, notice_text
    from pseudolife_memory.private_state import open_private
    parent = Path(tempfile.mkdtemp(prefix="doorbell-corpus-"))
    home = parent / "home"
    probe = parent / "probe"
    probe.mkdir()
    (probe / "sitecustomize.py").write_text(child_binding_source())
    key = hashlib.sha256(THREAD.encode()).hexdigest()
    records = []
    candidate_hash = hashlib.sha256(args.candidate.read_bytes()).hexdigest()
    try:
        selected = [case for case in cases(notice_text) if not args.case or case["id"] in args.case]
        for case in selected[:1 if args.first_only else None]:
            arms = {}
            for arm, prefix in [("oracle", [args.oracle_python, "-m", "pseudolife_memory.cli"]),
                                ("candidate", [str(args.candidate)])]:
                opaque = case["digest_variant"] == "opaque-user"
                home = parent/"users"/"home" if opaque else parent/"home"
                if opaque:
                    reset_home(home.parent)
                reset_home(home)
                capture = home.parent if opaque else home
                cwd = home if opaque else args.oracle_source
                env = isolated_env(home)
                env.update(PYTHONDONTWRITEBYTECODE="1", PSEUDOLIFE_DIGEST_DIR=str(home/"digests"))
                for name, value in case["environment_deltas"].items():
                    if value is None:
                        env.pop(name, None)
                    else:
                        env[name] = value
                if case["digest_variant"] == "relative":
                    env["PSEUDOLIFE_DIGEST_DIR"] = os.path.relpath(home/"digests", args.oracle_source)
                if arm == "oracle":
                    env.update(PYTHONPATH=os.pathsep.join([str(probe), str(args.oracle_source)]),
                               DOORBELL_BINDING=str(parent/"binding.json"))
                pending = PendingNotice(capture/case["state_root"], THREAD)
                pending.path.parent.mkdir(parents=True)
                if case["pending"] is not None:
                    text = case["pending_raw"] or json.dumps(case["pending"], ensure_ascii=False, separators=(",", ":"))
                    try:
                        text.encode("utf-8")
                    except UnicodeEncodeError:
                        text = json.dumps(case["pending"], separators=(",", ":"))
                    pending._write_atomic(pending.path, text)
                if case["receipt_b64"] is not None:
                    fd = open_private(pending.prompt_seen_path, os.O_WRONLY|os.O_CREAT)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(base64.b64decode(case["receipt_b64"]))
                if case["lock_b64"] is not None:
                    fd = open_private(pending.path.with_suffix(".bell-lock"), os.O_WRONLY|os.O_CREAT)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(base64.b64decode(case["lock_b64"]))
                pre = snapshot(capture)
                assert hashlib.sha256(args.candidate.read_bytes()).hexdigest() == candidate_hash
                observation = run_cli(prefix, ["doorbell-prompt-seen", *case["argv"]],
                                      cwd=cwd, env=env, timeout=10,
                                      stdin=base64.b64decode(case["stdin_b64"]))
                observation.update(post_files_b64=snapshot(capture), pre_files_b64=pre,
                                   argv=[*prefix, "doorbell-prompt-seen", *case["argv"]], environment=env,
                                   cwd=str(cwd), candidate_sha256=candidate_hash)
                assert hashlib.sha256(args.candidate.read_bytes()).hexdigest() == candidate_hash
                if arm == "oracle":
                    binding = json.loads((parent/"binding.json").read_text())
                    assert Path(binding["executable"]) == Path(args.oracle_python)
                    for module in binding["modules"].values():
                        relative = Path(module["path"]).relative_to(args.oracle_source)
                        assert hashlib.sha256((args.oracle_source/relative).read_bytes()).hexdigest() == module["sha256"]
                        assert all(module["bytecode"].values()), module
                    observation["source_binding"] = binding
                observation["metadata"] = {}
                for path in pending.path.parent.iterdir():
                    fd = open_private(path, os.O_RDONLY)
                    st = os.fstat(fd)
                    observation["metadata"][path.name] = dict(private=True, nlink=st.st_nlink,
                                                              mode=oct(st.st_mode & 0o777))
                    os.close(fd)
                expected = candidate_case(case) if arm == "candidate" else case
                assert_effect(expected, observation, key, pre)
                arms[arm] = observation
            fields = ["exit_code", "stdout_b64", "stderr_b64", "post_files_b64"]
            differences = compare({f: arms["oracle"][f] for f in fields},
                                  {f: arms["candidate"][f] for f in fields}, BYTE_POLICY)
            expected = candidate_case(case)
            if "substitution" not in expected:
                assert not differences, case["id"]
            records.append(dict(case=case, candidate_expectation=expected, raw_differences=differences,
                                **arms, controls=controls(arms["candidate"])))
            args.output.write_text(json.dumps(dict(records=records, complete=False), indent=2)+"\n")
    finally:
        reset_home(home)
        shutil.rmtree(parent)
        assert not parent.exists()
    args.output.write_text(json.dumps(dict(records=records, complete=True, cleanup=True,
                                         candidate_sha256=hashlib.sha256(args.candidate.read_bytes()).hexdigest()),
                                     indent=2)+"\n")
    print(json.dumps(dict(cases=len(records), expectations_checked=True, private_metadata=True, cleanup=True)))


if __name__ == "__main__":
    main()
