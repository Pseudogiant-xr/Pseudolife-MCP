"""Keep PARITY_SELFHOSTED_RUNNERS listing only the parity runners that are online.

The Rust workflow's self-hosted parity lane routes one job per listed machine
(``pseudolife-parity-<machine>``) and skips itself while the variable is
unset (docs/specs/2026-10-19-self-hosted-parity-lane.md). A job routed to a
machine with no online runner queues for a day, so the variable must follow
the runners. This compares the repository's registered runners with the
variable and corrects it:

- a machine is listed while at least one of its runners is online, and for
  ``--grace-minutes`` after its last one was seen online, so a runner's own
  update restart does not drop it;
- no machine listed means the variable is deleted, which switches the lane
  off (``[]`` would fail the matrix instead);
- with ``--machines-file``, only the machines it names (one per line, ``#``
  comments allowed) are listed, so a new machine's runner can be online for
  a trial dispatch before nightly jobs may route to it; a missing file
  allows every machine, an empty one none;
- a line ``<machine> on-demand`` names an on-demand machine, whose runners
  are VMs an orchestrator starts when a job is queued for its label, so
  while idle it has no runner online by design. It also counts as online
  while its heartbeat is fresh: the repository variable
  ``PARITY_HEARTBEAT_<MACHINE>`` (the name upper-cased, ``-`` as ``_``)
  holding the Unix time in seconds the orchestrator last wrote it, as
  ``date +%s`` prints it. Fresh means at most ``--heartbeat-minutes`` old
  and at most ``HEARTBEAT_SKEW_SECONDS`` ahead of this machine's clock; a
  missing, non-numeric or stale heartbeat is not fresh, and the machine
  then drops out after the grace like any other. Only on-demand machines'
  heartbeats are read, and a line with any other marker is refused;
- any API error, or an allowlist that cannot be read, changes nothing and
  exits 1 with one line saying why.

It prints one line when it changes the variable and nothing otherwise. Run it
every few minutes from a machine that is not itself a runner, since a machine
that goes down cannot take itself out. Its token is a fine-grained token for
this repository alone with "Administration: read" (to list runners) and
"Variables: read and write" (which also reads the heartbeats);
``--token-file`` names a file only its account can read.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

REPOSITORY = "Pseudogiant-xr/Pseudolife-MCP"
VARIABLE = "PARITY_SELFHOSTED_RUNNERS"
LABEL_PREFIX = "pseudolife-parity-"
API = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100
TIMEOUT_SECONDS = 30
HEARTBEAT_PREFIX = "PARITY_HEARTBEAT_"
ON_DEMAND = "on-demand"
# How far ahead of this machine's clock an orchestrator's heartbeat may be
# and still count: NTP-synced hosts agree to well under a second, so two
# minutes covers a drifting clock without letting a far-future value keep a
# machine listed long after its orchestrator stops.
HEARTBEAT_SKEW_SECONDS = 120
# A machine name that maps onto a valid variable name.
_ON_DEMAND_NAME = re.compile(r"[A-Za-z0-9-]+")
# Decimal seconds only: ASCII digits, an optional fraction, no sign or exponent.
_HEARTBEAT_VALUE = re.compile(r"[0-9]+(\.[0-9]+)?", re.ASCII)


class ApiError(Exception):
    """A GitHub API request that failed."""


# (method, path, body) -> (status, parsed JSON or None)
Call = Callable[[str, str, "dict | None"], "tuple[int, object]"]


def github(token: str, api_url: str = API) -> Call:
    base = api_url.rstrip("/")

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        })
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            # Only a read may find nothing; a write that finds nothing failed.
            if error.code == 404 and method == "GET":
                return 404, None
            raise ApiError(f"HTTP {error.code} for {method} {path}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ApiError(f"{type(error).__name__} for {method} {path}: {error}") from error

    return call


def online_machines(call: Call, repository: str) -> set[str]:
    """The machines (label suffixes) with at least one online runner."""
    machines: set[str] = set()
    page = 1
    while True:
        status, listing = call("GET", f"/repos/{repository}/actions/runners?per_page={PER_PAGE}&page={page}", None)
        if status != 200 or not isinstance(listing, dict) or not isinstance(listing.get("runners"), list):
            raise ApiError(f"unexpected runner listing (HTTP {status})")
        runners = listing["runners"]
        for runner in runners:
            if runner.get("status") != "online":
                continue
            for label in runner.get("labels", []):
                name = label.get("name", "")
                if name.startswith(LABEL_PREFIX) and len(name) > len(LABEL_PREFIX):
                    machines.add(name[len(LABEL_PREFIX):])
        if len(runners) < PER_PAGE:
            return machines
        page += 1


def current_value(call: Call, repository: str) -> str | None:
    """The variable's value, or None while it is unset."""
    status, variable = call("GET", f"/repos/{repository}/actions/variables/{VARIABLE}", None)
    if status == 404:
        return None
    if status != 200 or not isinstance(variable, dict) or not isinstance(variable.get("value"), str):
        raise ApiError(f"unexpected variable response (HTTP {status})")
    return variable["value"]


def heartbeat_variable(machine: str) -> str:
    """The repository variable an on-demand machine's orchestrator writes."""
    return HEARTBEAT_PREFIX + machine.upper().replace("-", "_")


def heartbeat_fresh(call: Call, repository: str, machine: str, now: float, heartbeat_seconds: float) -> bool:
    """Whether the machine's heartbeat is fresh; a missing or unreadable one is not.

    A failed request raises ApiError, so a transient API error changes
    nothing rather than starting the machine's grace.
    """
    status, variable = call("GET", f"/repos/{repository}/actions/variables/{heartbeat_variable(machine)}", None)
    if status != 200 or not isinstance(variable, dict) or not isinstance(variable.get("value"), str):
        return False
    text = variable["value"].strip()
    if not _HEARTBEAT_VALUE.fullmatch(text):
        return False
    beat = float(text)
    return math.isfinite(beat) and -HEARTBEAT_SKEW_SECONDS <= now - beat <= heartbeat_seconds


def listed(online: set[str], last_seen: dict[str, float], now: float, grace_seconds: float) -> list[str]:
    """The machines to list: online now, or seen online within the grace."""
    for machine in online:
        last_seen[machine] = now
    return sorted(machine for machine, seen in last_seen.items() if now - seen <= grace_seconds)


class Machines(NamedTuple):
    """The allowlist: every machine it names, and which of them are on-demand."""

    allowed: set[str]
    on_demand: set[str]


def allowed_machines(path: Path | None) -> Machines | None:
    """The allowlist, or None (every machine, none on-demand) when it does not exist."""
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise ApiError(f"cannot read --machines-file {path}: {getattr(error, 'strerror', None) or error}") from error
    machines = Machines(set(), set())
    for number, line in enumerate(text.splitlines(), start=1):
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        name, markers = words[0], words[1:]
        if markers and markers != [ON_DEMAND]:
            raise ApiError(f"--machines-file {path} line {number}: {line.strip()!r} is neither '<machine>' "
                           f"nor '<machine> {ON_DEMAND}'")
        if markers and not _ON_DEMAND_NAME.fullmatch(name):
            raise ApiError(f"--machines-file {path} line {number}: an {ON_DEMAND} machine's name takes only "
                           f"letters, digits and '-', since it names the variable {HEARTBEAT_PREFIX}<MACHINE>")
        machines.allowed.add(name)
        if markers:
            machines.on_demand.add(name)
    return machines


def reconcile(call: Call, repository: str, state_file: Path, grace_seconds: float, now: float,
              machines: Machines | None = None, heartbeat_seconds: float = 15 * 60) -> str | None:
    """Correct the variable. Returns the line describing the change, if any."""
    online = online_machines(call, repository)
    allowed = None if machines is None else machines.allowed
    if machines is not None:
        online &= machines.allowed
        # An idle on-demand machine has no runner online; its heartbeat stands in.
        online |= {machine for machine in sorted(machines.on_demand - online)
                   if heartbeat_fresh(call, repository, machine, now, heartbeat_seconds)}
    try:
        last_seen = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(last_seen, dict):
            last_seen = {}
    except (OSError, ValueError):
        last_seen = {}
    if allowed is not None:
        last_seen = {machine: seen for machine, seen in last_seen.items() if machine in allowed}
    wanted = listed(online, last_seen, now, grace_seconds)
    value = current_value(call, repository)
    change = None
    if not wanted:
        if value is not None:
            call("DELETE", f"/repos/{repository}/actions/variables/{VARIABLE}", None)
            reason = "no allowed parity runner online" if allowed is not None else "no parity runner online"
            if machines is not None and machines.on_demand:
                reason += " and no on-demand machine's heartbeat fresh"
            change = f"{VARIABLE} deleted (was {value}): {reason}"
    else:
        text = json.dumps(wanted)
        if value is None:
            call("POST", f"/repos/{repository}/actions/variables", {"name": VARIABLE, "value": text})
            change = f"{VARIABLE} set to {text}"
        elif _parsed(value) != wanted:
            call("PATCH", f"/repos/{repository}/actions/variables/{VARIABLE}", {"name": VARIABLE, "value": text})
            change = f"{VARIABLE} changed from {value} to {text}"
    # Machines past the grace are forgotten, so the file stays small.
    kept = {machine: seen for machine, seen in last_seen.items() if machine in wanted}
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(kept), encoding="utf-8")
    return change


def _parsed(value: str) -> object:
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return sorted(parsed) if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed) else None


def main(argv: list[str] | None = None, call: Call | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token-file", type=Path, required=call is None)
    parser.add_argument("--state-file", type=Path, required=True,
                        help="where each machine's last online time is kept")
    parser.add_argument("--grace-minutes", type=float, default=10.0)
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--machines-file", type=Path,
                        help="only the machines named here (one per line, '<machine>' or "
                             "'<machine> on-demand') are listed; a missing file allows every machine")
    parser.add_argument("--heartbeat-minutes", type=float, default=15.0,
                        help="how old an on-demand machine's heartbeat may be and still keep it listed")
    options = parser.parse_args(argv)
    if call is None:
        try:
            token = options.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            print(f"runner_watch: cannot read --token-file {options.token_file}: {error.strerror}; "
                  "nothing changed", file=sys.stderr)
            return 1
        if not token:
            print(f"runner_watch: {options.token_file} is empty; nothing changed", file=sys.stderr)
            return 1
        call = github(token)
    try:
        machines = allowed_machines(options.machines_file)
        change = reconcile(call, options.repository, options.state_file, options.grace_minutes * 60, time.time(),
                           machines, options.heartbeat_minutes * 60)
    except ApiError as error:
        print(f"runner_watch: {error}; nothing changed", file=sys.stderr)
        return 1
    if change:
        print(f"runner_watch: {change}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
