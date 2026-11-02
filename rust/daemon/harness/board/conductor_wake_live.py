"""Explicit live pytest adapter for the conductor board stress selection."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board.conductor_wake import SCENARIOS  # noqa: E402


def test_live_conductor_wake(tmp_path, monkeypatch):
    """Explicit pytest stress adapter; both engines use an offline embedder.

    Select this node directly: the ordinary runner unit suite does not
    require a built daemon. The existing stress lane supplies the binary.
    Missing configuration fails instead of silently skipping coverage.
    """
    import os
    from pathlib import Path
    import yaml
    from board import run
    from board.boundary import runtime_library
    from embedding_fixture import create

    binary = Path(os.environ["PL_SECURITY_RELEASE_MUTANTS_BIN"]).resolve()
    assert binary.is_file()
    model = tmp_path / "model"
    create(model)
    env = {"ORT_DYLIB_PATH": str(runtime_library()),
           "PSEUDOLIFE_DAEMON_ONNX_DIR": str(model),
           "PSEUDOLIFE_DAEMON_ORT_THREADS": "1", "OMP_NUM_THREADS": "1"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PSEUDOLIFE_DAEMON_MUTANT", raising=False)
    results = []
    for cls in SCENARIOS.values():
        scenario = cls()
        config = yaml.safe_load(scenario.config_yaml)
        config["embedding"] = {"model_name": str(model), "backend": "onnx",
                               "device": "cpu", "cpu_dtype": "fp32"}
        config["memory"] = {"embedding_dim": 4, "miras": {"preset": "flat"}}
        scenario.config_yaml = yaml.safe_dump(config)
        scenario.env = {**scenario.env, **env}
        results.append(run.run_scenario(scenario, binary, tmp_path, "live", False))
    steps, differences, state_differences, problems = run.summarize(results)
    assert steps > 0
    assert differences == state_differences == problems == 0, results
