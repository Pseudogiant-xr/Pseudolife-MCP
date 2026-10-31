"""ops/selfhosted/runner_watch.py keeps the self-hosted lane's variable on the online runners."""

from __future__ import annotations

import json
import re

import pytest

from ops.selfhosted import runner_watch as watch

REPO = "owner/repo"
VARIABLE_PATH = f"/repos/{REPO}/actions/variables/{watch.VARIABLE}"


def runner(name: str, status: str, *labels: str) -> dict:
    return {"name": name, "status": status,
            "labels": [{"name": label} for label in ("self-hosted", "Linux", *labels)]}


class FakeGitHub:
    def __init__(self, runners: list[dict], value: str | None = None, fail: str | None = None,
                 variables: dict[str, object] | None = None):
        self.runners = runners
        self.value = value
        self.fail = fail
        # Other repository variables by name (the heartbeats); absent ones 404.
        self.variables = dict(variables or {})
        self.reads: list[str] = []
        self.writes: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, path: str, body: dict | None) -> tuple[int, object]:
        if self.fail and self.fail in f"{method} {path}":
            raise watch.ApiError(f"HTTP 500 for {method} {path}")
        if method == "GET" and "/actions/runners" in path:
            page = int(path.rsplit("page=", 1)[1])
            chunk = self.runners[(page - 1) * watch.PER_PAGE:page * watch.PER_PAGE]
            return 200, {"total_count": len(self.runners), "runners": chunk}
        if method == "GET" and path == VARIABLE_PATH:
            return (404, None) if self.value is None else (200, {"name": watch.VARIABLE, "value": self.value})
        if method == "GET" and path.startswith(f"/repos/{REPO}/actions/variables/"):
            self.reads.append(path)
            name = path.rsplit("/", 1)[1]
            return (200, {"name": name, "value": self.variables[name]}) if name in self.variables else (404, None)
        self.writes.append((method, path, body))
        if method == "DELETE":
            self.value = None
            return 204, None
        self.value = body["value"]
        return (201 if method == "POST" else 204), None


def run(fake: FakeGitHub, tmp_path, capsys, now: float = 1000.0, grace: float = 600.0) -> str:
    change = watch.reconcile(fake, REPO, tmp_path / "state.json", grace, now)
    return change or ""


def test_an_online_runner_sets_the_variable(tmp_path, capsys):
    fake = FakeGitHub([runner("parity-linux-1", "online", "pseudolife-parity-linux"),
                       runner("parity-linux-2", "offline", "pseudolife-parity-linux")])
    assert "set to" in run(fake, tmp_path, capsys)
    assert fake.writes == [("POST", f"/repos/{REPO}/actions/variables",
                            {"name": watch.VARIABLE, "value": '["linux"]'})]


def test_a_correct_variable_is_left_alone(tmp_path, capsys):
    fake = FakeGitHub([runner("a", "online", "pseudolife-parity-windows"),
                       runner("b", "online", "pseudolife-parity-linux")], value='["windows", "linux"]')
    assert run(fake, tmp_path, capsys) == ""
    assert fake.writes == []


def test_a_machine_offline_past_the_grace_is_dropped_and_the_last_one_deletes_it(tmp_path, capsys):
    fake = FakeGitHub([runner("a", "online", "pseudolife-parity-linux"),
                       runner("b", "online", "pseudolife-parity-windows")])
    run(fake, tmp_path, capsys, now=1000)
    assert json.loads(fake.value) == ["linux", "windows"]
    fake.runners[1]["status"] = "offline"
    # Within the grace a runner's own restart keeps its machine listed.
    assert run(fake, tmp_path, capsys, now=1300) == ""
    assert "changed from" in run(fake, tmp_path, capsys, now=1700)
    assert json.loads(fake.value) == ["linux"]
    fake.runners[0]["status"] = "offline"
    assert run(fake, tmp_path, capsys, now=2000) == ""
    assert "deleted" in run(fake, tmp_path, capsys, now=2400)
    assert fake.value is None and fake.writes[-1][0] == "DELETE"
    # Nothing online and nothing set: no write.
    count = len(fake.writes)
    assert run(fake, tmp_path, capsys, now=5000) == ""
    assert len(fake.writes) == count


def test_a_runner_without_a_parity_label_lists_nothing(tmp_path, capsys):
    fake = FakeGitHub([runner("other", "online", "gpu"), runner("bare", "online", "pseudolife-parity-")])
    assert run(fake, tmp_path, capsys) == ""
    assert fake.writes == []


def test_an_unreadable_value_is_rewritten(tmp_path, capsys):
    fake = FakeGitHub([runner("a", "online", "pseudolife-parity-linux")], value="linux")
    assert "changed from linux" in run(fake, tmp_path, capsys)
    assert fake.value == '["linux"]'


def test_runners_past_the_first_page_count(tmp_path, capsys):
    others = [runner(f"r{index}", "offline", "pseudolife-parity-linux") for index in range(watch.PER_PAGE)]
    fake = FakeGitHub([*others, runner("late", "online", "pseudolife-parity-windows")])
    run(fake, tmp_path, capsys)
    assert fake.value == '["windows"]'


@pytest.mark.parametrize("failing", ["GET /repos/owner/repo/actions/runners", f"GET {VARIABLE_PATH}"])
def test_an_api_error_changes_nothing(tmp_path, capsys, failing):
    fake = FakeGitHub([runner("a", "online", "pseudolife-parity-linux")], value='["windows"]', fail=failing)
    assert watch.main(["--state-file", str(tmp_path / "state.json"), "--repository", REPO], call=fake) == 1
    assert fake.writes == [] and fake.value == '["windows"]'
    assert "nothing changed" in capsys.readouterr().err


def test_a_missing_token_file_says_so(tmp_path, capsys):
    assert watch.main(["--token-file", str(tmp_path / "absent"), "--state-file", str(tmp_path / "s.json")]) == 1
    assert "cannot read --token-file" in capsys.readouterr().err


def test_main_prints_one_line_per_change_and_nothing_otherwise(tmp_path, capsys):
    fake = FakeGitHub([runner("a", "online", "pseudolife-parity-linux")])
    argv = ["--state-file", str(tmp_path / "state.json"), "--repository", REPO]
    assert watch.main(argv, call=fake) == 0
    assert capsys.readouterr().out == f'runner_watch: {watch.VARIABLE} set to ["linux"]\n'
    assert watch.main(argv, call=fake) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("code, outcome", [(404, (404, None)), (403, watch.ApiError), (500, watch.ApiError)])
def test_the_client_reads_404_as_unset_and_raises_on_other_errors(monkeypatch, code, outcome):
    import urllib.error

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, code, "error", {}, None)

    monkeypatch.setattr(watch.urllib.request, "urlopen", refuse)
    call = watch.github("token", "https://api.example.com")
    if isinstance(outcome, tuple):
        assert call("GET", VARIABLE_PATH, None) == outcome
    else:
        with pytest.raises(outcome):
            call("GET", VARIABLE_PATH, None)


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_a_write_that_finds_no_variable_fails(monkeypatch, method):
    # Opus review of 14f086ca: a write that 404s changed nothing, so it must
    # not be reported as a change.
    import urllib.error

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    monkeypatch.setattr(watch.urllib.request, "urlopen", refuse)
    with pytest.raises(watch.ApiError):
        watch.github("token", "https://api.example.com")(method, VARIABLE_PATH, None)


# --- the allowlist: an online machine is listed only once it is allowed ---

def test_an_online_machine_outside_the_allowlist_is_never_listed(tmp_path, capsys):
    # A new machine's runner comes online for its trial dispatch before it
    # may take nightly jobs (docs/specs/2026-10-19-self-hosted-parity-lane.md).
    fake = FakeGitHub([runner("a", "online", "pseudolife-parity-linux"),
                       runner("b", "online", "pseudolife-parity-windows")], value='["linux", "windows"]')
    allowed = tmp_path / "machines"
    allowed.write_text("# machines the nightly may route to\nlinux\n\n", encoding="utf-8")
    argv = ["--state-file", str(tmp_path / "state.json"), "--repository", REPO, "--machines-file", str(allowed)]
    assert watch.main(argv, call=fake) == 0
    assert fake.value == '["linux"]'
    allowed.write_text("linux\nwindows\n", encoding="utf-8")
    assert watch.main(argv, call=fake) == 0
    assert json.loads(fake.value) == ["linux", "windows"]


def test_an_empty_allowlist_switches_the_lane_off(tmp_path, capsys):
    fake = FakeGitHub([runner("a", "online", "pseudolife-parity-linux")], value='["linux"]')
    allowed = tmp_path / "machines"
    allowed.write_text("", encoding="utf-8")
    assert watch.main(["--state-file", str(tmp_path / "s.json"), "--repository", REPO,
                       "--machines-file", str(allowed)], call=fake) == 0
    assert fake.value is None
    assert "no allowed parity runner online" in capsys.readouterr().out


def test_without_an_allowlist_file_every_machine_counts(tmp_path, capsys):
    fake = FakeGitHub([runner("b", "online", "pseudolife-parity-windows")])
    assert watch.main(["--state-file", str(tmp_path / "s.json"), "--repository", REPO,
                       "--machines-file", str(tmp_path / "absent")], call=fake) == 0
    assert fake.value == '["windows"]'


def test_an_unreadable_allowlist_changes_nothing(tmp_path, capsys):
    fake = FakeGitHub([runner("a", "online", "pseudolife-parity-linux")], value='["linux"]')
    allowed = tmp_path / "machines"
    allowed.mkdir()
    assert watch.main(["--state-file", str(tmp_path / "s.json"), "--repository", REPO,
                       "--machines-file", str(allowed)], call=fake) == 1
    assert fake.writes == [] and "nothing changed" in capsys.readouterr().err


# --- on-demand machines: kept listed by their orchestrator's heartbeat ---

HEARTBEAT = "PARITY_HEARTBEAT_WINDOWS"
HEARTBEAT_PATH = f"/repos/{REPO}/actions/variables/{HEARTBEAT}"


def watch_at(fake: FakeGitHub, tmp_path, monkeypatch, now: float, machines: str, *extra: str) -> int:
    allowed = tmp_path / "machines"
    allowed.write_text(machines, encoding="utf-8")
    monkeypatch.setattr(watch.time, "time", lambda: now)
    return watch.main(["--state-file", str(tmp_path / "state.json"), "--repository", REPO,
                       "--machines-file", str(allowed), *extra], call=fake)


def test_the_heartbeat_variable_name_is_stable():
    # An orchestrator outside this repository writes it, so a rename breaks
    # every on-demand machine (docs/specs/2026-10-19-self-hosted-parity-lane.md).
    assert watch.heartbeat_variable("windows") == "PARITY_HEARTBEAT_WINDOWS"
    assert watch.heartbeat_variable("win-vm2") == "PARITY_HEARTBEAT_WIN_VM2"


def test_an_on_demand_machine_with_a_fresh_heartbeat_is_listed_without_a_runner(tmp_path, monkeypatch, capsys):
    fake = FakeGitHub([runner("w", "offline", "pseudolife-parity-windows")], variables={HEARTBEAT: "99500"})
    assert watch_at(fake, tmp_path, monkeypatch, 100000, "linux\nwindows  on-demand   # VMs started per job\n") == 0
    assert fake.value == '["windows"]'
    assert HEARTBEAT_PATH in fake.reads


def test_an_on_demand_machine_with_a_stale_heartbeat_drops_after_the_grace(tmp_path, monkeypatch, capsys):
    fake = FakeGitHub([], variables={HEARTBEAT: "100000"})
    machines = "windows on-demand\n"
    assert watch_at(fake, tmp_path, monkeypatch, 100000, machines) == 0
    assert fake.value == '["windows"]'
    # The heartbeat is 14 minutes old: fresh under the 15-minute default.
    assert watch_at(fake, tmp_path, monkeypatch, 100000 + 14 * 60, machines) == 0
    # At 16 minutes it is stale, but it was last fresh 2 minutes ago: within the grace.
    assert watch_at(fake, tmp_path, monkeypatch, 100000 + 16 * 60, machines) == 0
    assert fake.value == '["windows"]'
    # The 10-minute grace since it was last fresh has passed.
    assert watch_at(fake, tmp_path, monkeypatch, 100000 + 25 * 60, machines) == 0
    assert fake.value is None
    assert "deleted" in capsys.readouterr().out


def test_heartbeat_minutes_sets_the_freshness_window(tmp_path, monkeypatch, capsys):
    fake = FakeGitHub([], variables={HEARTBEAT: "100000"})
    assert watch_at(fake, tmp_path, monkeypatch, 100000 + 6 * 60, "windows on-demand\n",
                    "--heartbeat-minutes", "5") == 0
    assert fake.value is None and fake.writes == []
    assert watch_at(fake, tmp_path, monkeypatch, 100000 + 6 * 60, "windows on-demand\n",
                    "--heartbeat-minutes", "7") == 0
    assert fake.value == '["windows"]'


@pytest.mark.parametrize("heartbeat", [None, "", "soon", "1e5", "nan", "inf", "-100000", "0x186a0",
                                       "100000 100000", "１００000", 100000, ["100000"]])
def test_a_missing_or_unreadable_heartbeat_is_stale(tmp_path, monkeypatch, capsys, heartbeat):
    variables = {} if heartbeat is None else {HEARTBEAT: heartbeat}
    fake = FakeGitHub([], value='["windows"]', variables=variables)
    assert watch_at(fake, tmp_path, monkeypatch, 100000, "windows on-demand\n") == 0
    assert fake.value is None


@pytest.mark.parametrize("heartbeat", ["100000", " 100000\n", "100000.75"])
def test_a_heartbeat_as_date_prints_it_is_fresh(tmp_path, monkeypatch, capsys, heartbeat):
    fake = FakeGitHub([], variables={HEARTBEAT: heartbeat})
    assert watch_at(fake, tmp_path, monkeypatch, 100000, "windows on-demand\n") == 0
    assert fake.value == '["windows"]'


def test_a_plain_machine_is_not_kept_by_a_heartbeat(tmp_path, monkeypatch, capsys):
    # The near-miss: the variable exists and is fresh, but without the marker
    # only a runner keeps the machine listed.
    fake = FakeGitHub([runner("w", "online", "pseudolife-parity-windows")], variables={HEARTBEAT: "100000"})
    assert watch_at(fake, tmp_path, monkeypatch, 100000, "windows\n") == 0
    assert fake.value == '["windows"]'
    fake.runners[0]["status"] = "offline"
    fake.variables[HEARTBEAT] = str(100000 + 11 * 60)
    assert watch_at(fake, tmp_path, monkeypatch, 100000 + 11 * 60, "windows\n") == 0
    assert fake.value is None
    assert fake.reads == []


def test_without_a_machines_file_no_heartbeat_is_read(tmp_path, capsys):
    fake = FakeGitHub([], value='["windows"]', variables={HEARTBEAT: "1000"})
    assert watch.main(["--state-file", str(tmp_path / "s.json"), "--repository", REPO], call=fake) == 0
    assert fake.value is None and fake.reads == []


def test_a_heartbeat_from_the_future_beyond_the_skew_is_stale(tmp_path, monkeypatch, capsys):
    ahead = 100000 + watch.HEARTBEAT_SKEW_SECONDS
    fake = FakeGitHub([], variables={HEARTBEAT: str(ahead + 1)})
    assert watch_at(fake, tmp_path, monkeypatch, 100000, "windows on-demand\n") == 0
    assert fake.value is None
    # Within the skew it counts: the orchestrator's clock may run a little ahead.
    fake.variables[HEARTBEAT] = str(ahead)
    assert watch_at(fake, tmp_path, monkeypatch, 100000, "windows on-demand\n") == 0
    assert fake.value == '["windows"]'


def test_an_on_demand_machine_with_a_runner_online_needs_no_heartbeat(tmp_path, monkeypatch, capsys):
    fake = FakeGitHub([runner("w", "online", "pseudolife-parity-windows")])
    assert watch_at(fake, tmp_path, monkeypatch, 100000, "windows on-demand\n") == 0
    assert fake.value == '["windows"]'
    # Its heartbeat is not read, so a heartbeat API error cannot fail the run.
    assert fake.reads == []


@pytest.mark.parametrize("heartbeat, why", [(None, "missing"), ("soon", "unreadable"),
                                            ("100000", "stale"), (str(100000 + 26 * 60 + 121), "ahead")])
def test_a_dropped_on_demand_machine_names_its_heartbeat_and_why(tmp_path, monkeypatch, capsys, heartbeat, why):
    # The orchestrator lives outside this repository, so the change line
    # says which variable to look at.
    fake = FakeGitHub([runner("a", "online", "pseudolife-parity-linux")], variables={HEARTBEAT: "100000"})
    machines = "linux\nwindows on-demand\n"
    assert watch_at(fake, tmp_path, monkeypatch, 100000, machines) == 0
    assert json.loads(fake.value) == ["linux", "windows"]
    capsys.readouterr()
    fake.variables.pop(HEARTBEAT)
    if heartbeat is not None:
        fake.variables[HEARTBEAT] = heartbeat
    assert watch_at(fake, tmp_path, monkeypatch, 100000 + 26 * 60, machines) == 0
    assert fake.value == '["linux"]'
    assert capsys.readouterr().out == (f'runner_watch: {watch.VARIABLE} changed from ["linux", "windows"] to '
                                       f'["linux"] (windows: {HEARTBEAT} {why})\n')


def test_a_dropped_on_demand_machine_names_its_heartbeat_when_the_lane_goes_off(tmp_path, monkeypatch, capsys):
    fake = FakeGitHub([], value='["windows"]')
    assert watch_at(fake, tmp_path, monkeypatch, 100000, "windows on-demand\n") == 0
    assert fake.value is None
    assert f"(windows: {HEARTBEAT} missing)" in capsys.readouterr().out


def test_a_machine_named_both_plain_and_on_demand_is_refused(tmp_path, monkeypatch, capsys):
    fake = FakeGitHub([runner("a", "online", "pseudolife-parity-linux")], value='["windows"]')
    assert watch_at(fake, tmp_path, monkeypatch, 100000, "linux\nwindows\nwindows on-demand\n") == 1
    assert fake.writes == [] and fake.value == '["windows"]'
    err = capsys.readouterr().err
    assert "line 3" in err and "windows" in err and "nothing changed" in err and err.count("\n") == 1
    # The same line twice is harmless and still allowed.
    assert watch_at(fake, tmp_path, monkeypatch, 100000, "linux\nlinux\n") == 0
    assert fake.value == '["linux"]'


@pytest.mark.parametrize("line", ["windows on_demand", "windows ondemand on-demand", "windows on-demand extra",
                                  "win.vm on-demand"])
def test_an_unknown_marker_is_refused_and_changes_nothing(tmp_path, monkeypatch, capsys, line):
    fake = FakeGitHub([runner("a", "online", "pseudolife-parity-linux")], value='["windows"]',
                      variables={HEARTBEAT: "100000"})
    assert watch_at(fake, tmp_path, monkeypatch, 100000, f"linux\n{line}\n") == 1
    assert fake.writes == [] and fake.value == '["windows"]'
    err = capsys.readouterr().err
    assert "line 2" in err and "on-demand" in err and "nothing changed" in err and err.count("\n") == 1


def test_a_heartbeat_api_error_changes_nothing(tmp_path, monkeypatch, capsys):
    # A transient API failure must not start the grace clock on the machine.
    fake = FakeGitHub([], value='["windows"]', fail=f"GET {HEARTBEAT_PATH}")
    state = tmp_path / "state.json"
    state.write_text('{"windows": 99000}', encoding="utf-8")
    assert watch_at(fake, tmp_path, monkeypatch, 100000, "windows on-demand\n") == 1
    assert fake.writes == [] and fake.value == '["windows"]'
    assert state.read_text(encoding="utf-8") == '{"windows": 99000}'
    assert "nothing changed" in capsys.readouterr().err


def test_the_setup_script_recognises_a_watcher_that_knows_on_demand():
    # setup-runner-watch.sh refuses :on-demand while the installed watcher
    # predates it, by finding this line in the installed copy.
    from pathlib import Path

    selfhosted = Path(watch.__file__).parent
    probe = "^ON_DEMAND = \"on-demand\"$"
    assert f"grep -qs '{probe}'" in (selfhosted / "setup-runner-watch.sh").read_text(encoding="utf-8")
    assert re.search(probe, (selfhosted / "runner_watch.py").read_text(encoding="utf-8"), re.MULTILINE)
