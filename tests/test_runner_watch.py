"""ops/selfhosted/runner_watch.py keeps the self-hosted lane's variable on the online runners."""

from __future__ import annotations

import json

import pytest

from ops.selfhosted import runner_watch as watch

REPO = "owner/repo"
VARIABLE_PATH = f"/repos/{REPO}/actions/variables/{watch.VARIABLE}"


def runner(name: str, status: str, *labels: str) -> dict:
    return {"name": name, "status": status,
            "labels": [{"name": label} for label in ("self-hosted", "Linux", *labels)]}


class FakeGitHub:
    def __init__(self, runners: list[dict], value: str | None = None, fail: str | None = None):
        self.runners = runners
        self.value = value
        self.fail = fail
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
