"""Coordination check-in bench: does the served check-in text change WHEN an
agent messages a peer?

The memory-policy bench (``evals/memory_policy_bench.py``) runs whole agent
sessions against a disposable daemon. This one is deliberately smaller: the
question is a single decision, so each run is one tool-free model call
(``claude -p --tools "" --setting-sources ""`` under its own system prompt,
no settings files, no MCP servers) that reads the check-in text an
arm serves, a team, a board and a situation, and answers whether it sends a
message now, only updates its status, or does nothing. Forty situations
(``evals/coordination_checkin_scenarios.py``): four teams that share
something, and for each "when to send" rule one situation where a message
is due and one where it is not.

Arms are check-in texts: ``none`` (no check-in), ``old`` (the text served
before the rules, pinned in the fixtures) and ``new`` (the served constant
now). ``label@suffix`` is an A/A copy for the noise floor.

Isolation: a throwaway ``CLAUDE_CONFIG_DIR`` and home, a working directory
outside the user's home and the repository (an ancestor CLAUDE.md would
reach every run), no MCP servers, no settings, no tools, and the current
access token only (the child cannot refresh the login). The scrubbed child
environment and those guards come from the memory-policy bench.

Usage::

    python -m evals.coordination_checkin_bench run --tag <tag> --replicates 3
    python -m evals.coordination_checkin_bench report evals/results/coordination-checkin-bench-<tag>.json

Every run writes ``evals/results/coordination-checkin-bench-<tag>.json`` and
refuses to overwrite one. Raw model output stays in the artifact (scrubbed of
the home path); nothing else leaves the work directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import coordination_checkin_scenarios as fx  # noqa: E402
from evals import memory_policy_bench as mb  # noqa: E402

BENCH_VERSION = 2   # v2: --frame (task by default); v1 was the board frame only
RESULTS_DIR = ROOT / "evals" / "results"
ARTIFACT_PREFIX = "coordination-checkin-bench-"
DEFAULT_MODEL = mb.DEFAULT_CLAUDE_MODEL
DEFAULT_EFFORT = mb.DEFAULT_EFFORT
ACTIONS = ("message", "status", "none")

# Constant across arms: what the model is, and the answer shape.
SYSTEM_PROMPT = (
    "You are an autonomous software and analysis agent working alongside other "
    "agents. Answer with a single JSON object and nothing else.")


# ── arms ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Arm:
    label: str
    variant: str
    text: str


def arm_texts() -> dict[str, str]:
    from pseudolife_memory.coordination import CHECKIN_TEXT
    return {"none": "", "old": fx.OLD_CHECKIN_TEXT, "new": CHECKIN_TEXT}


def parse_arms(spec: str, extra: dict[str, str] | None = None) -> list[Arm]:
    """Arms by label. ``extra`` adds ablation texts by name (``--arm-file
    name=path``); a name may not shadow a built-in arm."""
    texts = arm_texts()
    for name, text in (extra or {}).items():
        if name in texts or "@" in name:
            raise SystemExit(f"--arm-file name {name!r} is taken or contains '@'")
        texts[name] = text
    arms = []
    for label in (s.strip() for s in spec.split(",") if s.strip()):
        variant = label.split("@", 1)[0]
        if variant not in texts:
            raise SystemExit(f"unknown arm {variant!r} in {label!r}; known: {', '.join(texts)}")
        arms.append(Arm(label, variant, texts[variant]))
    if len({a.label for a in arms}) != len(arms):
        raise SystemExit("arm labels must be unique (use label@suffix for an A/A copy)")
    return arms


def read_arm_files(specs: list[str] | None) -> dict[str, str]:
    out = {}
    for spec in specs or ():
        name, sep, path = spec.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise SystemExit(f"--arm-file wants name=path, got {spec!r}")
        out[name.strip()] = Path(path.strip()).read_text(encoding="utf-8").strip()
    return out


def plan(arms: list[Arm], scenarios: list[str], replicates: int, seed: int) -> list[dict]:
    """Every (arm, scenario, replicate); arms interleave within each
    (replicate, scenario) block in a seeded order."""
    rng = random.Random(seed)
    out = []
    for rep in range(replicates):
        for sid in scenarios:
            order = list(arms)
            rng.shuffle(order)
            out.extend({"arm": a.label, "variant": a.variant, "scenario": sid,
                        "replicate": rep} for a in order)
    return out


def run_id(tag: str, item: dict) -> str:
    raw = f"{tag}-{item['arm']}-{item['scenario']}-r{item['replicate']}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)


# ── the prompt ─────────────────────────────────────────────────────────────

# How the question is put. ``board`` (bench v1, the 2026-09-28 first run)
# asks for a board decision outright; every arm, even no check-in at all,
# then sent a message whenever a peer was named in the situation, so four
# of the five rules sat at ceiling and nothing could move them. ``task``
# asks for the agent's next step in its own work, with the board action as
# one field of it, which is closer to where the six reviewed sessions went
# wrong (they kept working and never sent).
FRAMES = ("task", "board")


def build_prompt(sc: fx.Scenario, checkin: str, frame: str = "task") -> str:
    if frame not in FRAMES:
        raise SystemExit(f"unknown frame {frame!r}; known: {', '.join(FRAMES)}")
    persona = fx.PERSONAS[sc.persona]
    peers = "\n".join(f"- {label}: task = {task}; status = {status}"
                      for label, task, status in fx.board(sc))
    context = (
        "You work alongside other agents that share a coordination board. The "
        "board has three verbs: update your own status line (memory_agents "
        "update), send an addressed message to one peer or to all active peers "
        "(memory_message send), and read your mail (memory_message receive).\n\n"
        "Instructions you were given at session start about the board:\n"
        f"{checkin.strip() or '(none)'}\n\n"
        f"Your team: {persona.team}\n\n"
        f"Peers on the board right now:\n{peers}\n\n"
        f"Your own status line: {sc.status}\n\n"
        f"What just happened:\n{sc.situation}\n\n")
    if frame == "board":
        return context + (
            "Decide what you do on the board right now, before you continue your "
            "work. Answer with one JSON object and nothing else:\n"
            '{"action": "message" | "status" | "none", "to": ["<peer label>" or "all"], '
            '"why": "<one sentence>"}\n'
            '"message" means you send a message now (you may also update your status); '
            '"status" means you only update your own status line; "none" means you do '
            "neither and continue.")
    return context + (
        "You are in the middle of your own task. What is your next step? Answer "
        "with one JSON object and nothing else:\n"
        '{"next": "<your next step in your own work, one sentence>", '
        '"action": "message" | "status" | "none", "to": ["<peer label>" or "all"], '
        '"why": "<one sentence>"}\n'
        '"action" is what you also do on the board as part of that step: "message" '
        'if you send a message, "status" if you only update your own status line, '
        '"none" if neither.')


# ── decisions and scoring ──────────────────────────────────────────────────

_OBJECT = re.compile(r"\{.*\}", re.S)


def parse_decision(text: str | None) -> dict | None:
    """The model's JSON object, from bare JSON or a fenced block; ``None``
    when there is none or the action is not one of the three verbs."""
    if not text:
        return None
    m = _OBJECT.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    action = str(obj.get("action", "")).strip().lower()
    if action not in ACTIONS:
        return None
    to = obj.get("to")
    if isinstance(to, str):
        to = [to]
    to = [str(t).strip() for t in (to or []) if str(t).strip()]
    return {"action": action, "to": to, "why": str(obj.get("why", ""))[:400]}


def score(sc: fx.Scenario, decision: dict | None) -> dict:
    """``correct``: a ``send`` scenario answered with a message, a ``no_send``
    one with status or nothing. ``addressed`` (send scenarios only): the
    message went where the rule says, everyone or the named peer."""
    if decision is None:
        return {"correct": 0.0, "addressed": None if sc.expect != "send" else 0.0,
                "action": None}
    action = decision["action"]
    correct = (action == "message") if sc.expect == "send" else (action != "message")
    addressed = None
    if sc.expect == "send":
        to = [t.lower() for t in decision["to"]]
        labels = {p.label.lower() for p in fx.PERSONAS[sc.persona].peers}
        if action != "message":
            addressed = 0.0
        elif sc.to == "all":
            addressed = float(any(t in ("all", "everyone", "*") for t in to) or labels <= set(to))
        else:
            addressed = float(any(sc.to.lower() in t for t in to))
    return {"correct": float(correct), "addressed": addressed, "action": action}


# ── one model call ─────────────────────────────────────────────────────────

def run_claude(run_dir: Path, project: Path, prompt: str, *, model: str, effort: str,
               timeout: float) -> dict:
    config_dir = run_dir / "claude-config"
    home = run_dir / "home"
    for d in (config_dir, home, project):
        d.mkdir(parents=True, exist_ok=True)
    mcp_path = run_dir / "mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    env = mb.scrubbed_env({
        "CLAUDE_CONFIG_DIR": str(config_dir), "HOME": str(home), "USERPROFILE": str(home),
        "CLAUDE_CODE_OAUTH_TOKEN": mb.claude_access_token(),
        "ANTHROPIC_SMALL_FAST_MODEL": model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
    cli = shutil.which("claude") or "claude"
    # Not ``--bare``: it ignores CLAUDE_CODE_OAUTH_TOKEN and answers "Not
    # logged in" (probed 2026-09-28, Claude Code 2.1.280).
    cmd = [cli, "-p", "--tools", "", "--setting-sources", "",
           "--strict-mcp-config", "--mcp-config", str(mcp_path),
           "--system-prompt", SYSTEM_PROMPT, "--model", model, "--effort", effort,
           "--output-format", "json", "--no-session-persistence", "--max-turns", "1"]
    started = time.time()
    timed_out = False
    with (run_dir / "client.err").open("wb") as err:
        proc = subprocess.Popen(cmd, cwd=project, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=err)
        try:
            out, _ = proc.communicate(prompt.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            mb.kill_tree(proc)
            out = b""
    raw = out.decode("utf-8", "replace")
    (run_dir / "client.out").write_text(raw, encoding="utf-8")
    result = None
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        pass
    text = result.get("result") if isinstance(result, dict) else None
    usage = (result or {}).get("usage") or {} if isinstance(result, dict) else {}
    return {"started": started, "ended": time.time(), "rc": proc.returncode,
            "timed_out": timed_out, "text": text if isinstance(text, str) else None,
            "is_error": bool((result or {}).get("is_error")) if isinstance(result, dict) else True,
            "usage": usage, "usd": (result or {}).get("total_cost_usd") if isinstance(result, dict) else None,
            "models": sorted(((result or {}).get("modelUsage") or {}).keys())
            if isinstance(result, dict) else []}


# ── statistics ─────────────────────────────────────────────────────────────

def valid(rec: dict) -> bool:
    return bool(rec.get("decision")) and not rec.get("timed_out") and not rec.get("errors")


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def summarize(records: list[dict], arms: list[str], seed: int = 20260927) -> dict:
    rng = random.Random(seed)
    per_arm = {}
    for arm in arms:
        rows = [r for r in records if r["arm"] == arm and valid(r)]
        by_scenario = defaultdict(list)
        for r in rows:
            by_scenario[r["scenario"]].append(float(r["grade"]["correct"]))
        acc, ci = mb.cluster_bootstrap(by_scenario, rng)
        per_rule = {rule: _mean([r["grade"]["correct"] for r in rows if r["rule"] == rule])
                    for rule in fx.RULES}
        per_persona = {p: _mean([r["grade"]["correct"] for r in rows if r["persona"] == p])
                       for p in fx.PERSONAS}
        sends = [r for r in rows if r["expect"] == "send"]
        holds = [r for r in rows if r["expect"] == "no_send"]
        per_arm[arm] = {
            "runs": len(rows),
            "invalid": sum(1 for r in records if r["arm"] == arm and not valid(r)),
            "accuracy": acc, "accuracy_ci95": ci,
            "send_recall": _mean([r["grade"]["correct"] for r in sends]),
            "no_send_specificity": _mean([r["grade"]["correct"] for r in holds]),
            "addressed": _mean([r["grade"]["addressed"] for r in sends]),
            "per_rule": per_rule, "per_persona": per_persona,
            "actions": {a: sum(1 for r in rows if r["grade"]["action"] == a) for a in ACTIONS},
        }
    return {"per_arm": per_arm, "valid_runs": sum(1 for r in records if valid(r)),
            "invalid_runs": sum(1 for r in records if not valid(r))}


def paired(records: list[dict], a: str, b: str, seed: int = 20260927) -> dict:
    """b minus a, paired on (scenario, replicate): overall, and per rule
    with each rule's two scenarios split by expectation, so a rule that does
    not move its own scenarios is visible."""
    rng = random.Random(seed)
    index = {(r["arm"], r["scenario"], r["replicate"]): r for r in records if valid(r)}
    deltas = defaultdict(list)
    for (arm, sid, rep), ra in index.items():
        if arm != a or (b, sid, rep) not in index:
            continue
        rb = index[(b, sid, rep)]
        deltas[sid].append(rb["grade"]["correct"] - ra["grade"]["correct"])
    delta, ci = mb.cluster_bootstrap(deltas, rng)
    out = {"delta": delta, "ci95": ci, "pairs": sum(len(v) for v in deltas.values()),
           "scenarios": sum(1 for v in deltas.values() if v), "per_rule": {}}
    for rule in fx.RULES:
        sids = [s.id for s in fx.SCENARIOS if s.rule == rule]
        rule_deltas = {sid: deltas[sid] for sid in sids if deltas.get(sid)}
        d, c = mb.cluster_bootstrap(rule_deltas, rng)
        out["per_rule"][rule] = {
            "delta": d, "ci95": c, "pairs": sum(len(v) for v in rule_deltas.values()),
            "send": _mean([x for sid, v in rule_deltas.items() for x in v
                           if fx.scenario(sid).expect == "send"]),
            "no_send": _mean([x for sid, v in rule_deltas.items() for x in v
                              if fx.scenario(sid).expect == "no_send"]),
        }
    return out


def scenario_digest() -> str:
    """The fixture file's content hash: a scenario edit changes it, so two
    artifacts compare only when their digests match."""
    data = (ROOT / "evals" / "coordination_checkin_scenarios.py").read_bytes()
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:12]


# ── orchestration ──────────────────────────────────────────────────────────

def build_artifact(records: list[dict], arms: list[Arm], args, tag: str) -> dict:
    labels = [a.label for a in arms]
    comparisons = {}
    for i, a in enumerate(arms):
        for b in arms[i + 1:]:
            comparisons[f"{b.label} - {a.label}"] = paired(records, a.label, b.label)
    aa = [(a, b) for i, a in enumerate(arms) for b in arms[i + 1:] if a.variant == b.variant]
    noise = None
    if aa:
        comp = comparisons[f"{aa[0][1].label} - {aa[0][0].label}"]
        lo, hi = comp["ci95"]
        noise = max(abs(lo), abs(hi)) if lo is not None and hi is not None else None
    usd = sum((r.get("cost") or {}).get("usd") or 0.0 for r in records)
    return {
        "bench": "coordination-checkin", "bench_version": BENCH_VERSION, "tag": tag,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_head": mb.git_head(), "client": "claude",
        "client_version": mb.client_version("claude"), "model": args.model,
        "effort": args.effort,
        "arms": [{"label": a.label, "variant": a.variant,
                  "text_sha256": hashlib.sha256(a.text.encode("utf-8")).hexdigest(),
                  "text": a.text} for a in arms],
        "system_prompt": SYSTEM_PROMPT,
        "scenarios": sorted({r["scenario"] for r in records}), "replicates": args.replicates,
        "seed": args.seed, "frame": getattr(args, "frame", "board"),
        "scenario_digest": scenario_digest(),
        "summary": summarize(records, labels),
        "comparisons": comparisons, "aa_noise": noise, "total_usd": round(usd, 4),
        "runs": records,
    }


class Bench:
    def __init__(self, args):
        self.args = args
        self.tag = args.tag
        self.work = mb.check_work_root(args.work_root) / f"checkin-{self.tag}"
        self.work.mkdir(parents=True, exist_ok=True)
        self.runs_path = self.work / "runs.jsonl"
        self.log_path = self.work / "progress.log"

    def log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def records(self) -> dict[str, dict]:
        out = {}
        if self.runs_path.exists():
            for line in self.runs_path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out[rec["run_id"]] = rec
        return out

    def one(self, item: dict, text: str) -> dict:
        sc = fx.scenario(item["scenario"])
        rid = run_id(self.tag, item)
        run_dir = self.work / "runs" / rid
        shutil.rmtree(run_dir, ignore_errors=True)
        project = run_dir / "project"
        project.mkdir(parents=True)
        mb.check_work_root(project)
        prompt = build_prompt(sc, text, self.args.frame)
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        rec = {"run_id": rid, **item, "persona": sc.persona, "rule": sc.rule,
               "expect": sc.expect, "to": sc.to, "model": self.args.model,
               "effort": self.args.effort, "frame": self.args.frame,
               "bench_version": BENCH_VERSION, "errors": []}
        try:
            client = run_claude(run_dir, project, prompt, model=self.args.model,
                                effort=self.args.effort, timeout=self.args.run_timeout)
            rec["timed_out"] = client["timed_out"]
            rec["client_rc"] = client["rc"]
            rec["wall_s"] = round(client["ended"] - client["started"], 1)
            rec["raw"] = client["text"]
            rec["decision"] = parse_decision(client["text"])
            rec["grade"] = score(sc, rec["decision"])
            u = client["usage"] or {}
            rec["cost"] = {"usd": client["usd"], "input_tokens": u.get("input_tokens"),
                           "output_tokens": u.get("output_tokens"),
                           "cache_read": u.get("cache_read_input_tokens"),
                           "cache_creation": u.get("cache_creation_input_tokens")}
            if client["is_error"] or client["rc"] != 0:
                rec["errors"].append(f"client rc={client['rc']} is_error={client['is_error']}")
            if any(m != self.args.model for m in client["models"]):
                rec["errors"].append(f"another model answered: {client['models']}")
        except Exception as exc:  # noqa: BLE001
            rec["errors"].append(f"{type(exc).__name__}: {str(exc)[:300]}")
            rec.setdefault("decision", None)
            rec.setdefault("grade", score(sc, None))
        mb.scrub_record(rec, None)
        with self.runs_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        g = rec.get("grade") or {}
        self.log(f"{rid}: action={g.get('action')} correct={g.get('correct')} "
                 f"addressed={g.get('addressed')} wall={rec.get('wall_s')}s "
                 f"errors={rec['errors']}")
        return rec

    def run(self) -> Path:
        args = self.args
        out_path = args.out or RESULTS_DIR / f"{ARTIFACT_PREFIX}{self.tag}.json"
        if out_path.exists():
            raise SystemExit(f"{out_path.name} exists; tags are single-use")
        arms = parse_arms(args.arms, read_arm_files(args.arm_file))
        texts = {a.label: a.text for a in arms}
        scenarios = list(fx.SCENARIO_IDS) if args.scenarios == "all" else [
            fx.scenario(s).id for s in args.scenarios.split(",")]
        items = plan(arms, scenarios, args.replicates, args.seed)
        done = {rid for rid, rec in self.records().items() if valid(rec)}
        todo = [i for i in items if run_id(self.tag, i) not in done]
        self.log(f"plan: {len(items)} runs ({len(todo)} to go), arms={args.arms}, "
                 f"scenarios={len(scenarios)}, replicates={args.replicates}, "
                 f"model={args.model}, effort={args.effort}, parallel={args.parallel}")
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            for _ in pool.map(lambda i: self.one(i, texts[i["arm"]]), todo):
                pass
        latest = self.records()
        records = [latest[run_id(self.tag, i)] for i in items if run_id(self.tag, i) in latest]
        artifact = build_artifact(records, arms, args, self.tag)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("x", encoding="utf-8") as f:
            json.dump(artifact, f, indent=2)
        self.log(f"wrote {out_path.name}")
        print(render(artifact))
        return out_path


# ── report ─────────────────────────────────────────────────────────────────

def _cell(x):
    return "  n/a" if x is None else f"{x:5.2f}"


def render(artifact: dict) -> str:
    arms = [a["label"] for a in artifact["arms"]]
    s = artifact["summary"]
    lines = [f"coordination check-in bench {artifact['tag']} - {artifact['model']} "
             f"(effort {artifact['effort']}, frame {artifact.get('frame', 'board')}); valid runs {s['valid_runs']}, invalid "
             f"{s['invalid_runs']}; ${artifact.get('total_usd', 0):.2f} list price", ""]
    lines.append(f"{'metric':<28}" + "".join(f"{a:>12}" for a in arms))
    for key in ("accuracy", "send_recall", "no_send_specificity", "addressed"):
        lines.append(f"{key:<28}" + "".join(
            f"{_cell(s['per_arm'].get(a, {}).get(key)):>12}" for a in arms))
    lines.append("")
    lines.append(f"{'per rule':<28}" + "".join(f"{a:>12}" for a in arms))
    for rule in fx.RULES:
        lines.append(f"{rule:<28}" + "".join(
            f"{_cell(s['per_arm'].get(a, {}).get('per_rule', {}).get(rule)):>12}" for a in arms))
    lines.append("")
    for key, comp in artifact["comparisons"].items():
        if not comp.get("pairs"):
            continue
        d, ci = comp["delta"], comp["ci95"]
        noise = artifact.get("aa_noise")
        flag = "" if noise is None or d is None else (
            " beyond A/A noise" if abs(d) > noise else " within A/A noise")
        lines.append(f"paired {key}: {d:+.3f} [{ci[0]:+.3f}, {ci[1]:+.3f}] "
                     f"n={comp['pairs']}{flag}")
        for rule, r in comp["per_rule"].items():
            if r["delta"] is None:
                continue
            lines.append(f"  {rule:<22}{r['delta']:+.3f} [{r['ci95'][0]:+.3f}, "
                         f"{r['ci95'][1]:+.3f}]  send {_cell(r['send'])}  "
                         f"no_send {_cell(r['no_send'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a plan and write a tagged artifact")
    r.add_argument("--tag", required=True)
    r.add_argument("--arms", default="none,old,new")
    r.add_argument("--arm-file", action="append", default=[], metavar="NAME=PATH",
                   help="an extra arm whose check-in text is read from PATH (ablations); "
                        "the artifact records its text and hash")
    r.add_argument("--scenarios", default="all")
    r.add_argument("--replicates", type=int, default=3)
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--effort", default=DEFAULT_EFFORT)
    r.add_argument("--seed", type=int, default=20260927)
    r.add_argument("--frame", choices=FRAMES, default="task",
                   help="how the question is put (see FRAMES)")
    r.add_argument("--parallel", type=int, default=4)
    r.add_argument("--run-timeout", type=float, default=300.0)
    r.add_argument("--work-root", type=Path, default=mb.default_work_root())
    r.add_argument("--out", type=Path, default=None)
    p = sub.add_parser("report", help="re-render an artifact")
    p.add_argument("artifact", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "run":
        if "haiku" in args.model.lower():
            raise SystemExit("the bench never runs Haiku; pass an explicit model")
        Bench(args).run()
    else:
        print(render(json.loads(args.artifact.read_text(encoding="utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
