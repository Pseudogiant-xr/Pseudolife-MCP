"""Named wait-mail clock, suffix and traceback fields; other bytes stay exact."""
import base64
import copy
import math
import re
import time


POLICY = "nondeterministic-bytes-semantic"
PREFIX = b"wait-mail: the daemon rang for addressed mail at "
LEDGER = ".pseudolife-mcp/digests/ledger.log"
TRACEBACK_CASES = frozenset({"invalid-session-bytes", "inline-session-bytes", "environment-session-bytes",
                             "invalid-multiple-session-bytes", "inline-unicode-session-bytes"})
CLOCK_CASES = frozenset({"wait-mail-large-watermark", "wait-mail-non-ascii-space-body",
                         "wait-mail-delayed-ring", "wait-mail-delayed-digest", "wait-mail-cr-spaces-ring",
                         "unicode-session", "wait-mail-unicode-delivery", "seen-directory",
                         "wait-mail-positive-long-ring-watermark"})


def delivery_projection(response, window, ledger_path):
    """Validate each clock against its own arm's captured wall-time window."""
    start, end = window
    if not all(math.isfinite(value) for value in window) or end < start:
        raise ValueError("invalid invocation wall-time window")
    first, last = math.floor(start), math.floor(end)
    stderr = base64.b64decode(response["stderr_b64"], validate=True)
    match = re.match(re.escape(PREFIX) + rb"([0-9]{2}:[0-9]{2}:[0-9]{2})(?= \()", stderr)
    if response["exit_code"] != 0 or match is None:
        raise ValueError("not a successful wait-mail delivery")
    clock = match.group(1)
    clocks = {time.strftime("%H:%M:%S", time.localtime(epoch)).encode("ascii")
              for epoch in range(first, last + 1)}
    if clock not in clocks:
        raise ValueError("delivery clock lies outside its invocation window")
    ledger = base64.b64decode(response["post_files_b64"][ledger_path], validate=True)
    epoch, separator, rest = ledger.partition(b"\t")
    if not separator or re.fullmatch(rb"0|[1-9][0-9]*", epoch) is None \
            or not first <= int(epoch) <= last:
        raise ValueError("ledger clock lies outside its invocation window")
    # This projection admits one delivery into a freshly seeded home. Existing
    # ledger content needs its own captured prefix; never erase it implicitly.
    if not rest.startswith(b"wait\t") or len(ledger.splitlines()) != 1:
        raise ValueError("expected exactly one fresh wait ledger line")
    projected = copy.deepcopy(response)
    projected["stderr_b64"] = base64.b64encode(
        stderr[:match.start(1)] + b"<local-clock>" + stderr[match.end(1):]).decode("ascii")
    projected["post_files_b64"][ledger_path] = base64.b64encode(
        b"<epoch>\t" + rest).decode("ascii")
    instance = {"policy": POLICY, "fields": ["stderr delivery HH:MM:SS", "ledger column 1"],
                "wall_window": [start, end], "raw_clock": clock.decode("ascii"),
                "raw_epoch": int(epoch)}
    return projected, instance


def invocation_window(observation):
    execution = observation.get("execution", {})
    if "wall_window" in execution:
        return execution["wall_window"]
    # These names belong to the retained historical capture instruments, whose
    # raw bytes remain input to replay; they do not rebind a native executable.
    bounds = execution.get("observation_wall_bounds_ns", observation.get("wall_bounds_ns"))
    if bounds is None:
        raise ValueError("missing captured invocation window")
    return [bound / 1_000_000_000 for bound in bounds]


def traceback_projection(response, *, oracle):
    raw = base64.b64decode(response["stderr_b64"], validate=True)
    lines = raw.splitlines(keepends=True)
    if response["exit_code"] != 1 or not lines \
            or re.fullmatch(rb"UnicodeEncodeError: [^\r\n]+\n", lines[-1]) is None:
        raise ValueError("expected exit 1 and exact terminal exception line with LF")
    prefix = lines[:-1]
    if oracle or prefix:
        if not prefix or prefix[0] != b"Traceback (most recent call last):\n":
            raise ValueError("only traceback header and frames may be deferred")
        frame_seen = False
        for line in prefix[1:]:
            if re.fullmatch(rb'  File "[^\r\n]+", line [0-9]+, in [^\r\n]+\n', line):
                frame_seen = True
            elif not frame_seen or not line.startswith(b"    ") or not line.endswith(b"\n") \
                    or b"\r" in line:
                raise ValueError("non-frame stderr is contractual")
        if not frame_seen:
            raise ValueError("traceback has no frames")
    projected = copy.deepcopy(response)
    projected["stderr_b64"] = base64.b64encode(lines[-1]).decode("ascii")
    return projected, {"policy": "python-traceback-not-contract", "field": "stderr header/frames",
                       "terminal_b64": projected["stderr_b64"]}


def suffix_projection(response, platform):
    raw = base64.b64decode(response["stderr_b64"], validate=True)
    error = b"[WinError 5] Access is denied" if platform == "windows" else b"[Errno 21] Is a directory"
    prefix = b"wait-mail: could not advance the .seen marker (" + error + b": "
    matches = list(re.finditer(re.escape(prefix)
        + rb"(?P<quote>['\"])(?P<path>[^\r\n]*?[/\\]\.tmp-(?P<suffix>[a-z0-9_]{8})\.seen)(?P=quote) -> "
        + rb"(?P<destination_quote>['\"])[^\r\n]*?(?P=destination_quote)\); "
        + rb"the prompt hook or tool-result hint may show this mail again\.(?:\r\r\n|\r\n|\n)\Z", raw))
    if len(matches) != 1 or len(re.findall(rb"\.tmp-[^\r\n]*?\.seen", raw)) != 1:
        raise ValueError("expected exactly one quoted source temporary basename")
    match = matches[0]
    projected = copy.deepcopy(response)
    projected["stderr_b64"] = base64.b64encode(
        raw[:match.start("suffix")] + b"<suffix>" + raw[match.end("suffix"):]).decode("ascii")
    return projected, {"policy": POLICY, "field": "stderr quoted source basename",
                       "raw_suffix": match.group("suffix").decode("ascii")}


def compare_record(record, platform):
    """Compare responses under named case policies, retaining inputs unchanged."""
    from .harness import Policy, compare
    if platform not in ("linux", "windows"):
        raise ValueError("wait-mail policies require a recorded Linux or Windows platform")
    from .wait_mail_candidate_contract import EXPECTATIONS, compare_candidate_contract
    if record["id"] in EXPECTATIONS:
        return compare_candidate_contract(record, platform)
    projected = {}
    instances = []
    for arm in ("oracle", "candidate"):
        observation = record[arm]
        response = copy.deepcopy(observation["response"])
        try:
            if record["id"] in TRACEBACK_CASES and platform == "linux":
                response, instance = traceback_projection(response, oracle=arm == "oracle")
                instances.append({"case": record["id"], "arm": arm, **instance})
            if record["id"] in CLOCK_CASES and response["exit_code"] == 0:
                response, instance = delivery_projection(response, invocation_window(observation), LEDGER)
                instances.append({"case": record["id"], "arm": arm, **instance})
            if record["id"] == "seen-directory":
                response, instance = suffix_projection(response, platform)
                instances.append({"case": record["id"], "arm": arm, **instance})
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            field = "/post_files_b64/" + LEDGER.replace("/", "~1") if "ledger" in str(error) else "/stderr_b64"
            raw_differences = compare(record["oracle"]["response"], record["candidate"]["response"],
                                      Policy(source_text_paths=(), ignored_values=()))
            return {"passed": False, "policy_instances": instances,
                    "differences": [{"path": field, "arm": arm,
                                     "reason": "named wait-mail policy refused: " + str(error)}, *raw_differences]}
        projected[arm] = response
    differences = compare(projected["oracle"], projected["candidate"],
                          Policy(source_text_paths=(), ignored_values=()))
    return {"passed": not differences, "differences": differences, "policy_instances": instances}
