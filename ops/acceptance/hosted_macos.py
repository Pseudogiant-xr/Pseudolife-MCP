"""Run the published Mac client against a source-built test peer."""

import os
from pathlib import Path
import secrets
import shutil
import tempfile

from . import matrix
from .hosted import (
    CODEX_HOME_NOTE, INSTALL, PEER_NOTE, REDACTOR, CleanupFailed, Failed, Profile, Receipt, Unavailable,
    command, native_path, prepare_codex, published_release, write_json,
)
from .hosted_linux import (
    CLIENT_FLAGS, clean_install, diagnostic_note, failure, free_space, install,
    live_install, ordinary, source_recovery, upgrade,
)
from .hosted_source import BUDGETS, Budget, loopback_port, prepare, private_file, source_peer


def offline(receipt, release, repo, profile, scratch):
    token_file = private_file(scratch / "offline.token", REDACTOR.secret(secrets.token_hex(32)) + "\n")
    url = f"http://127.0.0.1:{loopback_port()}"
    result = live_install(None, repo, profile, ["--daemon-url", url, "--token-file", str(token_file)])
    receipt.observe(f"{INSTALL} <closed-loopback>", result,
                    result.code not in (0, 124) and b"client-only install: no healthy daemon answered at" in result.output
                    and not (profile.codex / "config.toml").is_file(),
                    "The published installer refused the closed endpoint without registering a client.")


def offline_faults(receipt, release, repo, profile, scratch):
    token = private_file(scratch / "offline.token", REDACTOR.secret(secrets.token_hex(32)) + "\n")
    url = f"http://127.0.0.1:{loopback_port()}"
    flags = CLIENT_FLAGS + ["--daemon-url", url, "--token-file", str(token)]
    for defect, diagnostic in (("corrupt", b"does not match the release"),
                               ("missing", b"although its SHA256SUMS lists it")):
        assets = scratch / defect
        assets.mkdir()
        shutil.copyfile(release.folder / "SHA256SUMS", assets / "SHA256SUMS")
        if defect == "corrupt":
            (assets / release.archive.name).write_bytes(b"not a release archive\n")
        result = install(repo, profile, flags, overrides={"PSEUDOLIFE_NATIVE_ARCHIVE_URL": str(assets)}, timeout=120)
        receipt.observe(f"{INSTALL} <{defect}-archive>", result,
                        result.code not in (0, 124) and diagnostic in result.output
                        and not (profile.codex / "config.toml").is_file(),
                        "The installer identified the archive fault before it registered a client.")


def mac_upgrade(row, release, repo, profile, peer, scratch):
    upgrade(row, release, repo, profile.path, peer, scratch)


PROBES = {
    "clean-install": clean_install,
    "ordinary-native-use": ordinary,
    "upgrade-and-rollback": mac_upgrade,
    "failure-and-retry": failure,
    "bank-recovery": source_recovery,
}
# Probes that need no daemon still run when the peer cannot come up.
OFFLINE = {"clean-install": offline, "ordinary-native-use": offline, "failure-and-retry": offline_faults}


def run(tag, platform, output):
    rows = [Receipt(scenario, platform) for scenario in matrix.SCENARIOS]
    release = None
    cleaned = False
    budget = Budget(BUDGETS[platform])
    setup = {"platform": platform, "total_budget_seconds": BUDGETS[platform], "peer_note": PEER_NOTE,
             "steps": budget.steps}
    try:
        machine = "x86_64" if platform == "macos-x64" else "arm64"
        require_runner = (os.environ.get("GITHUB_ACTIONS") == "true" and os.uname().sysname == "Darwin"
                          and os.uname().machine == machine)
        if not require_runner:
            raise Failed("The job ran on the selected disposable macOS runner.")
        with tempfile.TemporaryDirectory(prefix="pla-", dir="/tmp") as temporary:
            scratch = Path(temporary).resolve()
            REDACTOR.path(scratch, "<scratch>")
            free_space(scratch)
            release = published_release(tag, platform, scratch / "release")
            repo = release.checkout(scratch / "release-checkout")
            path = native_path(scratch, prepare_codex(platform, scratch))
            console = command(["stat", "-f", "%u", "/dev/console"])
            launchd = command(["launchctl", "print", f"gui/{os.getuid()}"])
            gui = console.code == launchd.code == 0 and console.output.strip() == str(os.getuid()).encode()
            peer_folder = scratch / "peer"
            peer_folder.mkdir(mode=0o700)
            pending_reason = None
            peer_cleaned = True
            try:
                build = prepare(release, repo, peer_folder, budget, platform)
                with source_peer(build, release, repo, path, peer_folder, budget) as peer:
                    setup["assertion"] = "The test peer became ready."
                    for row in rows:
                        observe(row, release, repo, path, peer, scratch, (launchd, gui), PROBES[row.scenario])
                        row.checkpoint(output, release)
            except Unavailable as error:
                pending_reason = setup["assertion"] = str(error)
                if error.failure is not None:
                    setup["failure"] = error.failure
                for row in rows:
                    if row.status == "not yet run" and row.scenario not in OFFLINE:
                        row.note = pending_reason
                    if row.scenario in OFFLINE and not row.steps:
                        observe(row, release, repo, path, None, scratch, (launchd, gui),
                                OFFLINE[row.scenario], pending_reason)
                    row.checkpoint(output, release)
            except CleanupFailed as error:
                # The rows' own results stand; none of them can count as a
                # pass with a fixture left running.
                peer_cleaned = False
                setup["cleanup"] = error.failure
                for row in rows:
                    row.cleanup_failed = True
                    if row.status != "fail":
                        row.status, row.note = "fail", "The disposable environment could not be cleaned up and verified."
            except Failed as error:
                setup["assertion"] = str(error)
                for row in rows:
                    if row.status != "fail":
                        row.status, row.note = "fail", str(error)
                        row.explain(error)
            cleaned = peer_cleaned
    except Failed as error:
        for row in rows:
            if row.status != "fail":
                row.status, row.note = "fail", str(error)
                row.explain(error)
    except (OSError, ValueError, KeyError, IndexError, StopIteration) as error:
        for row in rows:
            if row.status != "fail":
                row.status, row.note = "fail", diagnostic_note(error)
    except Unavailable as error:
        for row in rows:
            if row.note == "The scenario was not selected.":
                row.note = str(error)
                row.explain(error)
    except Exception as error:  # noqa: BLE001 - a traceback would print unfiltered output
        setup["assertion"] = diagnostic_note(error)
        for row in rows:
            if row.status != "fail":
                row.status, row.note = "fail", diagnostic_note(error)
    finally:
        # As on Linux: every receipt and the setup record are written, and
        # only redacted text reaches the job log.
        write_json(output / "peer-setup.json", setup, replace=True)
        for row in rows:
            if row.status == "pass" and not cleaned:
                row.status, row.note = "fail", "The disposable environment could not be cleaned up and verified."
                row.cleanup_failed = True
            row.write(output, release, cleaned)
            print(REDACTOR.text(f"{row.scenario}: {row.status}. {row.note}"))
    return int(any(row.status == "fail" for row in rows))


def observe(row, release, repo, path, peer, scratch, launchd, probe, pending_reason=None):
    scope = scratch / row.scenario
    scope.mkdir(exist_ok=True)
    profile = Profile(scope / ("home" if peer is not None else "offline-home"), path)
    launchd_result, gui = launchd
    row.steps.append({"command": "launchctl print gui/<uid>", "exit_code": launchd_result.code,
                      "assertion": "A launchd GUI domain was available.", "observed": gui})
    try:
        if peer is None:
            probe(row, release, repo, profile, scope)
            row.note = pending_reason
        else:
            row.daemon = peer.kind
            row.daemon_engine = peer.engine
            row.fixture_notes.append(CODEX_HOME_NOTE)
            probe(row, release, repo, profile, peer, scope)
            row.status, row.note = "pass", matrix.SCENARIOS[row.scenario].outcome
    except Failed as error:
        row.status, row.note = "fail", str(error)
        row.explain(error)
    except Unavailable as error:
        row.note = str(error)
        row.explain(error)
    except (OSError, ValueError, KeyError, IndexError) as error:
        row.status, row.note = "fail", diagnostic_note(error)

