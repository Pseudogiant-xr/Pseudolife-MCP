"""Explicit offline live adapter for lease recovery on both engines."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_live_lease_recovery(tmp_path, monkeypatch):
    import yaml
    import json
    from board import run
    from board.boundary import runtime_library
    from board.lease_recovery import SCENARIOS
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
    _startup_recovery(run, scenario, binary, tmp_path)
    steps, differences, state_differences, problems = run.summarize(results)
    assert steps > 0
    assert differences == state_differences == problems == 0, json.dumps(results, indent=2)


def _startup_recovery(run, scenario, binary, root):
    """A committed grant must produce mail on restart without a board request."""
    from contextlib import ExitStack
    import time
    import psycopg
    from board.scenarios import Ctx, seed_bank

    for kind in ("python", "rust"):
        with ExitStack() as cleanup:
            name = run.pg.name(f"pl_cf_w2f_grant_startup_{kind}")
            cleanup.callback(run.pg.drop, name)
            dsn = run.pg.create(name)
            initial = run.start(kind, scenario, binary, root, dsn, f"grant-init-{kind}")
            try:
                run.wait_ready(initial.port)
            finally:
                initial.stop()
            ctx = Ctx()
            def committed(store, ctx):
                for who in ("A", "B"):
                    ctx.agents[who] = store.register("default")
                    store.acquire_lease("default", ctx.id(who), ctx.agents[who]["credential"],
                                        name="startup-resource", ttl=600)
                store.release_lease("default", ctx.id("A"), ctx.agents["A"]["credential"],
                                    name="startup-resource")
            seed_bank(dsn, committed, ctx)
            restarted = run.start(kind, scenario, binary, root, dsn, f"grant-restart-{kind}")
            cleanup.callback(restarted.stop)
            run.wait_ready(restarted.port)
            deadline = time.monotonic() + 15
            with psycopg.connect(dsn, autocommit=True) as conn:
                while True:
                    count = conn.execute("SELECT count(*) FROM coordination_messages WHERE "
                        "recipient_agent_id=%s AND sender_principal='daemon'", (ctx.id("B"),)).fetchone()[0]
                    if count or time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
            assert count == 1, f"{kind}: startup did not drain committed grant without a board request"
