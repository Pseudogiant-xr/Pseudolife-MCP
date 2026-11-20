"""Write and check the installed acceptance ledger."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys


TARGETS = {
    "windows-x64": "x86_64-pc-windows-msvc",
    "linux-x64": "x86_64-unknown-linux-gnu",
    "linux-arm64": "aarch64-unknown-linux-gnu",
    "macos-x64": "x86_64-apple-darwin",
    "macos-arm64": "aarch64-apple-darwin",
}
MACHINE_ROLES = ("disposable VM", "disposable physical host", "ephemeral hosted runner")
STATUSES = ("pass", "fail", "not yet run")
FAILURE_OUTCOMES = (
    "A command failed before the scenario completed.",
    "A command succeeded where the scenario required a refusal.",
    "The observed state differed from the expected state.",
    "Cleanup could not be completed and verified.",
    "The scenario was interrupted before its outcome could be verified.",
)
VERSION = r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:(?:a|b|rc|\.dev)[0-9]{1,4})?"
ARCHIVE_NAMES = {
    target: re.compile(
        rf"pseudolife-mcp-(?P<version>{VERSION})-{target}"
        + (r"\.zip" if platform == "windows-x64" else r"\.tar\.gz")
    )
    for platform, target in TARGETS.items()
}
COMPOSE_NAME = re.compile(rf"pseudolife-compose-{VERSION}\.tar\.gz")
CHECKSUM_ROW = re.compile(r"([0-9a-fA-F]{64}) [ *](\S+)")
CELL_FIELDS = {"schema", "scenario", "platform", "status"}
RUN_FIELDS = {"commit", "archive", "sha256", "machine_role", "commands", "outcome", "cleanup"}


@dataclass(frozen=True)
class Step:
    command: str
    expect_refusal: bool = False


@dataclass(frozen=True)
class Scenario:
    steps: tuple[Step, ...]
    outcome: str


INSTALLER = "<native-installer>"
CLI = "pseudolife-mcp"
CLIENT = "<mcp-client>"
SCENARIOS = {
    "clean-install": Scenario(
        (Step(f"{INSTALLER} <clean-options>"), Step(f"{CLIENT} <first-session>")),
        "The native install registered the client and its first MCP session succeeded without Python or pip.",
    ),
    "upgrade-and-rollback": Scenario(
        (
            Step("<legacy-installer> <0.23.x-fixture>"),
            Step("<native-opt-in> <0.23.x-fixture>"),
            Step(f"{INSTALLER} <rerun-from-0.23.x>"),
            Step("<rollback> <0.23.x-fixture>"),
            Step("<legacy-installer> <0.24-python-fixture>"),
            Step("<native-opt-in> <0.24-python-fixture>"),
            Step(f"{INSTALLER} <rerun-from-0.24-python>"),
            Step("<rollback> <0.24-python-fixture>"),
        ),
        "Both legacy starting states moved to native, reran without changes, and rolled back to a working client.",
    ),
    "ordinary-native-use": Scenario(
        (
            Step(f"{CLIENT} <board-register-and-wake>"),
            Step(f"{CLI} connect <fixture-options>"),
            Step(f"{CLI} doctor <fixture-options>"),
            Step(f"{CLI} console <fixture-options>"),
            Step(f"{CLI} tunnel <fixture-options>"),
            Step(f"{CLI} update <fixture-options>"),
        ),
        "Board registration and wake, connect, doctor, Console, tunnel and update worked with the installed client.",
    ),
    "bank-recovery": Scenario(
        (
            Step(f"{CLI} backup <fixture-options>"),
            Step(f"{CLI} restore <fixture-options>"),
            Step("<recovery-probe> <fixture-options>"),
        ),
        "Backup, restore and the applicable recovery probes preserved the disposable bank.",
    ),
    "failure-and-retry": Scenario(
        (
            Step(f"{INSTALLER} <corrupt-archive>", expect_refusal=True),
            Step(f"{INSTALLER} <missing-archive>", expect_refusal=True),
            Step(f"{INSTALLER} <cancelled-install>", expect_refusal=True),
        ),
        "The injected failures were observed and the retry completed with the expected installed state.",
    ),
}
LINUX_RECOVERY = (
    Step(f"{CLI} move <interrupted-move>", expect_refusal=True),
    Step(f"{CLI} move --rollback <move-id>"),
    Step(f"{CLI} update <old-schema-bank>"),
    Step(f"{CLI} update <pending-counter-bank>"),
    Step(f"{CLI} update <failed-import-bank>", expect_refusal=True),
    Step(f"{CLI} update <already-imported-bank>"),
)
FAILURE_PROBES = {
    "windows": ("<locked-file>", "<interrupted-refresh>"),
    "linux": ("<locked-file>", "<child-interruption>", "<interrupted-publication>", "<interrupted-refresh>"),
    "macos": ("<locked-file>", "<cancelled-launch>", "<failed-launch>", "<interrupted-refresh>"),
}
LOCAL_BANK = (
    "A local bank needs its own Docker engine. The project, network, three container names and "
    "loopback ports 8765 and 5433 are fixed. PSEUDOLIFE_BANK_VOLUME and "
    "PSEUDOLIFE_STATE_VOLUME can override the default volume names."
)
USER_SERVICES = {
    "linux": "A non-client-only install changes the real user service manager.",
    "windows": (
        "A non-client-only install changes the real task scheduler. The installer also writes the real user PATH. "
        "Use a Windows host without the py launcher, which can resolve outside the staged search path."
    ),
    "macos": "Legacy tunnel migration reaches the real launchd login domain. Native unattended schedule setup is unavailable.",
}
LINUX_INSTALL_EFFECTS = (
    "A local install can add a ufw rule through sudo, offer Tailscale exposure and passkey setup, "
    "and open the browser for Console sign-in."
)
FIRST_LINUX_PROBE = (
    "--client-only --daemon-url <fixture-url> --client claude,codex,gemini "
    "--client-update-schedule no --claude-plugin skip --token-file <fixture-token-file>",
    "Use a local PSEUDOLIFE_NATIVE_ARCHIVE_URL, a disposable profile and endpoint, and put "
    "stand-in claude, codex and gemini programs on the Python-free search path.",
    "Covers real archive verification and launcher installation, fixture endpoint preflight "
    "and registration requests to the stand-ins.",
    "Real client registration and first MCP session, plugin, scheduling, local Docker, "
    "and the other cells remain not yet run.",
)


class RecordError(ValueError):
    """The record cannot be published or counted."""


@dataclass(frozen=True)
class Candidate:
    commit: str
    digests: dict[str, str]

    def verify(self, record: dict) -> None:
        if record["commit"] != self.commit:
            raise RecordError("record was made for a different commit")
        if self.digests.get(record["archive"]) != record["sha256"]:
            raise RecordError("archive SHA-256 does not match its SHA256SUMS row")


def cells():
    return ((scenario, platform) for scenario in SCENARIOS for platform in TARGETS)


def pending(scenario: str, platform: str) -> dict:
    return {"schema": 1, "scenario": scenario, "platform": platform, "status": "not yet run"}


def _steps(scenario: str, platform: str) -> tuple[Step, ...]:
    steps = SCENARIOS[scenario].steps
    if scenario == "bank-recovery" and platform.startswith("linux-"):
        steps += LINUX_RECOVERY
    if scenario == "failure-and-retry":
        family = platform.split("-", 1)[0]
        steps += tuple(Step(f"<fault-probe> {fault}", expect_refusal=True) for fault in FAILURE_PROBES[family])
        steps += (Step(f"{INSTALLER} <retry-options>"),)
    installer = (
        "powershell -NoProfile -File ops/install-native.ps1"
        if platform == "windows-x64"
        else "bash ops/install-native.sh"
    )
    return tuple(Step(step.command.replace(INSTALLER, installer), step.expect_refusal) for step in steps)


def commands(scenario: str, platform: str) -> tuple[str, ...]:
    """Return the publishable command templates for a matrix cell."""
    return tuple(step.command for step in _steps(scenario, platform))


def describe(scenario: str, platform: str) -> None:
    family = platform.split("-", 1)[0]
    print(f"{scenario} on {platform}")
    print(USER_SERVICES[family])
    print(LOCAL_BANK)
    if family == "linux" and scenario in ("clean-install", "upgrade-and-rollback", "failure-and-retry"):
        print(LINUX_INSTALL_EFFECTS)
    if family == "linux" and scenario == "clean-install":
        args, *details = FIRST_LINUX_PROBE
        print("First bounded Linux probe (not yet run)")
        print(f"bash ops/install-native.sh {args}")
        print("\n".join(details))
    print("Command templates")
    print("\n".join(commands(scenario, platform)))


def known_archive(name: str) -> bool:
    return any(pattern.fullmatch(name) for pattern in (*ARCHIVE_NAMES.values(), COMPOSE_NAME))


def read_manifest(path: Path) -> dict[str, str]:
    entries = {}
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        raise RecordError("cannot read ASCII SHA256SUMS") from None
    for line in lines:
        match = CHECKSUM_ROW.fullmatch(line)
        if not match or not known_archive(match[2]):
            raise RecordError("SHA256SUMS contains an invalid release asset row")
        digest, name = match.groups()
        if name in entries:
            raise RecordError("SHA256SUMS contains a duplicate asset name")
        entries[name] = digest.lower()
    if not entries:
        raise RecordError("SHA256SUMS has no release assets")
    return entries


def _digest(value: object, digits: int) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{digits}}}", value) is not None


def _validate_commands(record: dict) -> None:
    steps = _steps(record["scenario"], record["platform"])
    allowed = {step.command for step in steps}
    entries = record["commands"]
    if not isinstance(entries, list) or not entries:
        raise RecordError("commands must contain an observed command and exit code")
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or entry.keys() != {"command", "exit_code"}
            or not isinstance(entry["command"], str)
            or entry["command"] not in allowed
            or type(entry["exit_code"]) is not int
            or not -(2**31) <= entry["exit_code"] < 2**32
        ):
            raise RecordError("use a published command template and an integer exit code")
    if record["status"] == "pass":
        expected = [(step.command, step.expect_refusal) for step in steps]
        observed = [(entry["command"], entry["exit_code"] != 0) for entry in entries]
        if observed != expected:
            raise RecordError("a pass requires the scenario's ordered commands and expected exit results")


def _validate(record: object) -> tuple[str, str]:
    if not isinstance(record, dict) or not CELL_FIELDS <= record.keys():
        raise RecordError("record must include the schema, scenario, platform and status")
    if type(record["schema"]) is not int or record["schema"] != 1:
        raise RecordError("unsupported record schema")
    scenario, platform, status = record["scenario"], record["platform"], record["status"]
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        raise RecordError("unknown scenario")
    if not isinstance(platform, str) or platform not in TARGETS:
        raise RecordError("unknown platform")
    if status not in STATUSES:
        raise RecordError("unknown status")
    fields = CELL_FIELDS if status == "not yet run" else CELL_FIELDS | RUN_FIELDS
    if record.keys() != fields:
        raise RecordError("record fields do not match its status")
    if status == "not yet run":
        return scenario, platform

    if not _digest(record["commit"], 40) or not _digest(record["sha256"], 64):
        raise RecordError("use a full Git commit and SHA-256 digest in lowercase hex")
    archive = record["archive"]
    if not isinstance(archive, str) or not ARCHIVE_NAMES[TARGETS[platform]].fullmatch(archive):
        raise RecordError("archive must be a release basename for the recorded platform")
    if record["machine_role"] not in MACHINE_ROLES:
        raise RecordError("use a listed machine role")
    outcomes = (SCENARIOS[scenario].outcome,) if status == "pass" else FAILURE_OUTCOMES
    if record["outcome"] not in outcomes:
        raise RecordError("use the observed outcome sentence from the record vocabulary")
    cleanup = record["cleanup"]
    if (
        not isinstance(cleanup, dict)
        or cleanup.keys() != {"done", "verified"}
        or not all(type(value) is bool for value in cleanup.values())
        or (not cleanup["done"] and cleanup["verified"])
    ):
        raise RecordError("cleanup requires boolean done and verified fields, with verification only after completion")
    if status == "pass" and not cleanup["verified"]:
        raise RecordError("a pass requires completed and verified cleanup")
    _validate_commands(record)
    return scenario, platform


def _filename(scenario: str, platform: str) -> str:
    return f"{scenario}--{platform}.json"


def write_record(folder: Path, record: dict) -> Path:
    """Create one validated row. Keep earlier ledger snapshots intact."""
    path = folder / _filename(*_validate(record))
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, indent=2) + "\n")
    return path


def _unique_keys(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecordError("duplicate JSON key")
        result[key] = value
    return result


def read_ledger(folder: Path, candidate: Candidate | None = None) -> dict[tuple[str, str], str]:
    rows = {}
    versions = set()
    names = {_filename(*cell): cell for cell in cells()}
    for path in sorted(folder.iterdir()):
        if path.name not in names:
            raise RecordError(f"unexpected ledger filename {ascii(path.name)}; expected <scenario>--<platform>.json")
        if path.is_symlink() or not path.is_file():
            raise RecordError("record must be a regular JSON file")
        try:
            payload = path.read_text(encoding="utf-8")
            if payload.startswith("\ufeff"):
                raise RecordError("UTF-8 byte-order mark; save the row as UTF-8 without a BOM")
            record = json.loads(payload, object_pairs_hook=_unique_keys)
            cell = _validate(record)
            if record["status"] != "not yet run":
                if candidate is not None:
                    candidate.verify(record)
                target = TARGETS[cell[1]]
                versions.add(ARCHIVE_NAMES[target].fullmatch(record["archive"])["version"])
        except RecordError as error:
            raise RecordError(f"{path.name}: {error}") from None
        except (UnicodeError, ValueError, RecursionError):
            raise RecordError(f"{path.name}: invalid JSON") from None
        if cell != names[path.name]:
            raise RecordError(f"{path.name}: filename must match its scenario and platform")
        rows[cell] = record["status"]
    if len(versions) > 1:
        raise RecordError("observed rows use different archive versions")
    for scenario, platform in cells():
        if (scenario, platform) not in rows:
            raise RecordError(f"{scenario} / {platform} has no row")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("init").add_argument("folder", type=Path)
    check = actions.add_parser("check")
    check.add_argument("folder", type=Path)
    check.add_argument("--commit", help="full candidate commit SHA")
    check.add_argument("--manifest", type=Path, help="trusted candidate SHA256SUMS file")
    check.add_argument("--require-pass", action="store_true", help="require a passing record in every cell")
    scenario = actions.add_parser("describe")
    scenario.add_argument("scenario", choices=SCENARIOS)
    scenario.add_argument("platform", choices=TARGETS)
    args = parser.parse_args(argv)
    if args.action == "describe":
        describe(args.scenario, args.platform)
        return 0
    try:
        if args.action == "init":
            args.folder.mkdir()
            for cell in cells():
                write_record(args.folder, pending(*cell))
            return 0
        candidate = None
        if args.commit is not None or args.manifest is not None:
            if not _digest(args.commit, 40) or args.manifest is None:
                raise RecordError("candidate binding requires --commit with a full lowercase Git SHA and --manifest")
            candidate = Candidate(args.commit, read_manifest(args.manifest))
        rows = read_ledger(args.folder, candidate)
    except RecordError as error:
        print(f"installed acceptance: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("installed acceptance: cannot read or create the record folder", file=sys.stderr)
        return 1
    header = ("scenario", *TARGETS)
    print(" | ".join(f"{column:<20}" for column in header).rstrip())
    for scenario in SCENARIOS:
        columns = (scenario, *(rows[scenario, platform] for platform in TARGETS))
        print(" | ".join(f"{column:<20}" for column in columns).rstrip())
    counts = tuple(sum(value == status for value in rows.values()) for status in STATUSES)
    print("{} passed, {} failed, {} not yet run. Not yet run does not count as a pass.".format(*counts))
    if args.require_pass and counts[0] != len(rows):
        print("installed acceptance: --require-pass requires a passing record for every cell", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
