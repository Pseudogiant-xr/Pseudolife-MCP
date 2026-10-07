"""Equal image warmups retain independent episode setup outside the timer."""
from .test_cli_episode_measurement import episode_instrument, measure


def test_warm_episode_reopens_and_verifies_every_control_warmup_and_timed_start(tmp_path, monkeypatch):
    args, case, prepare, verify, calls, state = episode_instrument(tmp_path, monkeypatch)
    args.warm_images = True
    receipt = measure(args, case, prepare, verify)
    assert len(calls) == len(state["history"]) == 68
    assert len(receipt["warmups"]) == 6
    assert all(row["files_and_environment_match_control"] for row in receipt["warmups"])
    assert [row["timed"] for row in receipt["independent_state_checks"]].count(True) == 60
    assert all(len(rows) == 30 for rows in receipt["runs"].values())
    assert not state["open"]
