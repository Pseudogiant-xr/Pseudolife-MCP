"""Measure where integration batches spend their time, from GitHub's record.

    python ops/batch_timeline.py collect --since 2026-10-19 --out <artifact.json>
    python ops/batch_timeline.py report <artifact.json> [--cutoff <ISO time>] [--json <out>]

`collect` reads every `integrate/` batch pull request opened since a date,
the Actions runs on each batch head (every attempt, every job), and the
timeline of each member, and writes them to one JSON artifact. It only
reads: no comment, label or merge. `report` works offline on that artifact
and splits each batch, and each member, into phases:

- awaiting admission: member opened (or marked ready) -> `ready-to-batch`
  label, split at the last commit (or force-push) before the label into
  revising and waiting after it. A timeline commit carries its commit
  date, not its push time. GitHub holds no review event here (reviews live in PR bodies),
  so review time is inside this phase, not separable from it.
- awaiting build: label -> the batch's first `Merge #N into` commit.
- rebuild repair: a member's first batch build -> the build of the batch
  that landed it, when a closed batch or a drop sent it round again.
- build: first to last merge commit of the batch.
- local validation: batch head commit -> batch PR opened (the local full
  suite, whose own duration the body's suite line records, plus the Rust
  harness run and `open`).
- CI first pass: batch PR opened -> the last job of every run's first
  attempt, split along the critical path into queue wait (queued ->
  started, which includes the account's concurrent-job cap as well as
  runner supply; a job no runner ever acquired is all wait), execution, and
  orchestration (event dispatch and the gaps between dependent jobs).
- failure repair: first pass done -> the last rerun job done, with each
  rerun's click latency, cause and whether its failed lane is required.
- awaiting merge: last job done -> merged.

The critical path is recovered without the workflow files: a dependent
job is created the second its last dependency completes, so the walk goes
back from the last job to finish through jobs that ended when it was
queued. When no job did, the report says so instead of guessing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import statistics
import sys
from pathlib import Path


def _sibling(name: str):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_batch_timeline_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # its dataclasses look themselves up here
    spec.loader.exec_module(module)
    return module


integrate = _sibling("integrate")
LABEL, PREFIX, REQUIRED_CHECKS = integrate.LABEL, integrate.PREFIX, integrate.REQUIRED_CHECKS
Refusal = integrate.Refusal

SCHEMA = 1
# `gh pr list` returns at most this many; a listing that reaches it may
# have cut older pull requests, so `collect` refuses instead.
LIST_LIMIT = 500
# Runs a batch's merge waits on. Code scanning's `dynamic` runs and
# scheduled or dispatched workflows are not the batch's CI.
CI_EVENTS = {"pull_request", "pull_request_target", "merge_group", "push"}
FAILED = {"failure", "cancelled", "timed_out", "startup_failure", "action_required"}
# A dependent job is created within a second or two of its dependency's
# completion (#892's jobs: 0 s); 5 s leaves room for clock rounding only.
CHAIN_TOLERANCE = 5
# The first jobs of a run are created within a second of the run itself.
RUN_START_TOLERANCE = 10
MERGE_RE = re.compile(r"^Merge #(\d+) into ")
MEMBER_ROW_RE = re.compile(r"^\| #(\d+) .*\| `([0-9a-f]{7,40})` \|.*\| ([^|]+) \|\s*$")
HAND_ROW_RE = re.compile(r"^\| #(\d+) \|")
DROPPED_RE = re.compile(r"^- #(\d+): (.*)$")
SUITE_RE = re.compile(r"in (\d+(?:\.\d+)?)s\b")
TIMELINE_EVENTS = ("labeled", "unlabeled", "committed", "head_ref_force_pushed",
                   "ready_for_review", "convert_to_draft", "reviewed")
BATCH_PHASES = ("build", "local_validation", "suite", "ci_first_pass", "ci_queue_wait",
                "ci_execution", "ci_orchestration", "failure_repair", "awaiting_merge",
                "batch_wall")
MEMBER_PHASES = ("admission", "revising", "after_last_commit", "awaiting_build",
                 "rebuild_repair", "lead")


def gh(*args: str) -> str:
    """Run gh and return its stdout (tests replace this function)."""
    return integrate._run(["gh", *args]).stdout


def _lines(text: str) -> list:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------- collect

def _when(value: str) -> dt.datetime:
    """An ISO date or time as UTC: a date is its midnight, a time without an
    offset is UTC."""
    try:
        when = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise Refusal(f"{value!r} is not an ISO date or time (e.g. 2026-10-21 or "
                      "2026-10-21T06:45:04Z)") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc)


def _since(value: str) -> str:
    return _when(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _listed(rows: list, what: str) -> list:
    if len(rows) >= LIST_LIMIT:
        raise Refusal(f"the {what} listing returned {LIST_LIMIT} pull requests, gh's limit "
                      "here, so older ones may be missing; collect a shorter window "
                      "(a later --since)")
    return rows


def _runs(sha: str) -> list[dict]:
    runs = _lines(gh("api", "--paginate",
                     f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={sha}&per_page=100",
                     "--jq", ".workflow_runs[] | {id, name, event, created_at, run_attempt}"))
    for run in runs:
        run["attempts"] = []
        for n in range(1, int(run.get("run_attempt") or 1) + 1):
            base = f"repos/{{owner}}/{{repo}}/actions/runs/{run['id']}/attempts/{n}"
            attempt = json.loads(gh("api", base, "--jq", "{run_started_at, conclusion}"))
            jobs = _lines(gh("api", "--paginate", f"{base}/jobs?per_page=100", "--jq",
                             ".jobs[] | {id, name, created_at, started_at, completed_at, "
                             "conclusion, steps: (.steps | length)}"))
            for job in jobs:
                job_id = job.pop("id", None)
                job["annotations"] = []
                if (job.get("conclusion") or "").lower() in FAILED and job_id is not None:
                    found = _lines(gh(
                        "api", f"repos/{{owner}}/{{repo}}/check-runs/{job_id}/annotations",
                        "--jq", '.[] | select(.annotation_level != "notice") '
                                "| {level: .annotation_level, message}"))
                    job["annotations"] = [{"level": a.get("level"),
                                           "message": str(a.get("message"))[:300]}
                                          for a in found]
            run["attempts"].append({"attempt": n, "run_started_at": attempt.get("run_started_at"),
                                    "conclusion": attempt.get("conclusion"), "jobs": jobs})
    return runs


def _member_numbers(batch: dict) -> set[int]:
    numbers = {int(m) for m, _, _ in _member_rows(batch.get("body") or "")}
    numbers |= {d["number"] for d in _dropped(batch.get("body") or "")}
    numbers |= {int(m.group(1)) for c in batch["commits"]
                if (m := MERGE_RE.match(c.get("messageHeadline") or ""))}
    return numbers


def _member(number: int) -> dict:
    view = json.loads(gh("pr", "view", str(number), "--json",
                         "number,title,state,createdAt,mergedAt,closedAt"))
    wanted = ", ".join(f'"{e}"' for e in TIMELINE_EVENTS)
    events = _lines(gh(
        "api", "--paginate", f"repos/{{owner}}/{{repo}}/issues/{number}/timeline?per_page=100",
        "--jq", f".[] | select(.event == ({wanted})) | "
                "{event, at: (.created_at // .submitted_at // .committer.date), "
                "label: .label.name}"))
    keep = ("number", "title", "state", "createdAt", "mergedAt", "closedAt")
    return {**{k: view.get(k) for k in keep},
            "events": [{k: e.get(k) for k in ("event", "at", "label") if e.get(k)}
                       for e in events]}


def collect(since: str) -> dict:
    """Read the batches opened since `since`, their CI and their members."""
    since = _since(since)
    rows = _listed(json.loads(gh("pr", "list", "--state", "all", "--limit", str(LIST_LIMIT),
                                 "--search", f"created:>={since[:10]}", "--json",
                                 "number,headRefName,isCrossRepository,createdAt") or "[]"),
                   "batch")
    rows = sorted((r for r in rows if r.get("headRefName", "").startswith(PREFIX)
                   and not r.get("isCrossRepository") and r.get("createdAt", "") >= since),
                  key=lambda r: r["createdAt"])
    batches, members = [], {}
    for row in rows:
        view = json.loads(gh("pr", "view", str(row["number"]), "--json",
                             "number,title,state,createdAt,mergedAt,closedAt,"
                             "headRefName,headRefOid,body,commits"))
        view["commits"] = [{k: c.get(k) for k in ("oid", "committedDate", "messageHeadline")}
                           for c in view.get("commits") or []]
        view["runs"] = _runs(view["headRefOid"])
        batches.append(view)
        for number in sorted(_member_numbers(view)):
            if str(number) not in members:
                members[str(number)] = _member(number)
    merged = _listed(json.loads(gh("pr", "list", "--state", "merged", "--base", integrate.BASE,
                                   "--limit", str(LIST_LIMIT), "--search",
                                   f"merged:>={since[:10]}", "--json",
                                   "number,title,mergedAt,headRefName") or "[]"), "merged")
    direct = [{k: r.get(k) for k in ("number", "title", "mergedAt", "headRefName")}
              for r in merged if (r.get("mergedAt") or "") >= since
              and not r.get("headRefName", "").startswith(PREFIX)]
    return {"schema": SCHEMA, "repository": "{owner}/{repo} of the checkout's gh",
            "label": LABEL, "since": since,
            "collected_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "batches": batches, "members": members,
            "direct_merges": sorted(direct, key=lambda r: r["mergedAt"])}


# ----------------------------------------------------------------- report

def _t(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _secs(start: dt.datetime | None, end: dt.datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds())


def _member_rows(body: str) -> list[tuple[str, str, str]]:
    """(number, head, CI at build) per member row of `integrate.py`'s table.
    A hand-built batch from before it (#850) has a `| #N | ... |` table with
    no head or CI column; its rows give the number only."""
    rows = [m.groups() for line in body.splitlines() if (m := MEMBER_ROW_RE.match(line))]
    if rows:
        return rows
    return [(m.group(1), "", "") for line in body.split("## Dropped")[0].splitlines()
            if (m := HAND_ROW_RE.match(line))]


def _annotations(job: dict) -> list[str]:
    """Annotation messages, failures before warnings: a failed job's
    failure is its cause, a deprecation warning beside it is not."""
    found = job.get("annotations") or []
    as_dicts = [a if isinstance(a, dict) else {"level": None, "message": a} for a in found]
    order = {"failure": 0, "warning": 1}
    return [a["message"] for a in sorted(as_dicts, key=lambda a: order.get(a["level"], 2))]


def _dropped(body: str) -> list[dict]:
    out, inside = [], False
    for line in body.splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## Dropped"
        elif inside and (m := DROPPED_RE.match(line)):
            out.append({"number": int(m.group(1)), "reason": m.group(2).strip()})
    return out


def _suite(body: str) -> float | None:
    for line in body.splitlines():
        if "Local full suite" in line and (m := SUITE_RE.search(line)):
            return float(m.group(1))
    return None


def _placeholder(job: dict) -> bool:
    """A matrix job GitHub never expanded: its name keeps the `${{ }}`, or it
    has no steps and ends before it was created."""
    c, e = _t(job.get("created_at")), _t(job.get("completed_at"))
    return "${{" in (job.get("name") or "") or (
        not job.get("steps") and c is not None and e is not None and e < c)


def _unfinished_jobs(batch: dict) -> int:
    """Jobs of the batch's CI with no completion yet (skipped and matrix
    placeholder jobs aside)."""
    return sum(1 for run in batch.get("runs") or [] if run.get("event") in CI_EVENTS
               for attempt in run["attempts"] for job in attempt["jobs"]
               if not job.get("completed_at") and not _placeholder(job)
               and (job.get("conclusion") or "").lower() != "skipped")


def _jobs(batch: dict) -> list[dict]:
    """Every job a batch's CI executed, attempt by attempt. A job a rerun
    retained from an earlier attempt (it started before the rerun did) and
    a skipped job never ran in that attempt, so neither appears."""
    out = []
    for run in batch.get("runs") or []:
        if run.get("event") not in CI_EVENTS:
            continue
        for attempt in run["attempts"]:
            start = _t(attempt.get("run_started_at"))
            for job in attempt["jobs"]:
                if (job.get("conclusion") or "").lower() == "skipped":
                    continue
                c, s, e = (_t(job.get(k)) for k in ("created_at", "started_at", "completed_at"))
                if c is None or e is None or _placeholder(job):
                    continue
                if attempt["attempt"] > 1 and s is not None and start is not None \
                        and _secs(s, start) > CHAIN_TOLERANCE:
                    continue
                never = not job.get("steps") or s is None
                out.append({
                    "name": f"{run['name']}: {job['name']}", "job": job["name"],
                    "run": run["name"], "run_id": run["id"], "attempt": attempt["attempt"],
                    "run_created": run.get("created_at"),
                    "created": c, "started": s, "completed": e,
                    "wait": max(0, _secs(c, e)) if never else max(0, _secs(c, s)),
                    "execution": 0 if never else max(0, _secs(s, e)),
                    "conclusion": (job.get("conclusion") or "").lower(),
                    "never_started": never, "required": job["name"] in REQUIRED_CHECKS,
                    "annotations": _annotations(job)})
    return out


def _critical_path(first: list[dict]) -> tuple[list[dict] | None, str]:
    if not first:
        return None, "no CI job ran"
    cur = max(first, key=lambda j: j["completed"])
    chain, notes = [cur], []
    while _secs(_t(cur["run_created"]), cur["created"]) > RUN_START_TOLERANCE:
        # Only a job queued before this one can be its dependency; instant
        # jobs ending in the same second would otherwise name each other.
        preds = [j for j in first if j["run_id"] == cur["run_id"]
                 and j["created"] < cur["created"] and not any(j is c for c in chain)
                 and abs(_secs(j["completed"], cur["created"])) <= CHAIN_TOLERANCE]
        if not preds:
            return None, (f"no job in {cur['run']} finished when {cur['job']} was queued "
                          f"({cur['created']:%H:%M:%S}Z), so its dependency is unknown")
        pick = max(preds, key=lambda j: (j["completed"], j["wait"] + j["execution"]))
        if len(preds) > 1:
            notes.append(f"{len(preds)} jobs finished when {cur['job']} was queued "
                         f"({', '.join(j['job'] for j in preds)}); the latest, {pick['job']}, "
                         "is taken as its dependency")
        cur = pick
        chain.insert(0, cur)
    return chain, "; ".join(notes)


def _reruns(batch: dict, jobs: list[dict]) -> list[dict]:
    out = []
    for run in batch.get("runs") or []:
        if run.get("event") not in CI_EVENTS:
            continue
        for attempt in run["attempts"][1:]:
            n = attempt["attempt"]
            prev = [j for j in jobs if j["run_id"] == run["id"] and j["attempt"] == n - 1]
            this = [j for j in jobs if j["run_id"] == run["id"] and j["attempt"] == n]
            prev_raw = run["attempts"][n - 2]["jobs"]
            failed = [j for j in prev if j["conclusion"] in FAILED]
            failed += [{"job": j["name"], "conclusion": j["conclusion"].lower(),
                        "required": j["name"] in REQUIRED_CHECKS, "never_started": True,
                        "annotations": _annotations(j)}
                       for j in prev_raw if (j.get("conclusion") or "").lower() in FAILED
                       and not any(p["job"] == j["name"] for p in failed)
                       and not _placeholder(j)]
            start = _t(attempt.get("run_started_at"))
            prev_end = max((j["completed"] for j in prev), default=None)
            end = max((j["completed"] for j in this), default=None)
            out.append({
                "run": run["name"], "attempt": n,
                "failed_jobs": [{"name": j["job"], "conclusion": j["conclusion"],
                                 "required": j["required"],
                                 "never_started": j["never_started"],
                                 "annotations": j["annotations"]} for j in failed],
                "wait": _secs(prev_end, start), "execution": _secs(start, end),
                "queue_wait": sum(j["wait"] for j in this)})
    return out


def _batch(b: dict, cutoff: dt.datetime | None) -> dict:
    body = b.get("body") or ""
    merges = [c for c in b["commits"] if MERGE_RE.match(c.get("messageHeadline") or "")]
    build_start = min((_t(c["committedDate"]) for c in merges), default=None)
    head = next((c for c in b["commits"] if c["oid"] == b.get("headRefOid")), None)
    build_end = _t(head["committedDate"]) if head and merges else None
    created, merged, closed = (_t(b.get(k)) for k in ("createdAt", "mergedAt", "closedAt"))
    rows = _member_rows(body)
    members = [int(n) for n, _, _ in rows] or [int(MERGE_RE.match(c["messageHeadline"]).group(1))
                                               for c in merges]
    jobs = _jobs(b)
    first = [j for j in jobs if j["attempt"] == 1]
    first_end = max((j["completed"] for j in first), default=None)
    final_end = max((j["completed"] for j in jobs), default=None)
    reruns = _reruns(b, jobs)
    chain, note = _critical_path(first)
    running = _unfinished_jobs(b)
    if running:
        # An open batch collected mid-run: the jobs that have finished say
        # nothing about when the pass will, so its CI phases stay unknown.
        first_end = final_end = chain = None
        note = (f"CI still running when collected ({running} job"
                f"{'s' if running != 1 else ''} unfinished); collect again once it is done")
    ci_first = _secs(created, first_end)
    wait = execution = orchestration = None
    if chain is not None and ci_first is not None:
        wait = sum(j["wait"] for j in chain)
        execution = sum(j["execution"] for j in chain)
        orchestration = ci_first - wait - execution
    return {
        "number": b["number"], "branch": b.get("headRefName"), "head": b.get("headRefOid"),
        "outcome": "merged" if merged else ("closed" if closed else "open"),
        "created": b.get("createdAt"), "merged": b.get("mergedAt"), "closed": b.get("closedAt"),
        "regime": None if cutoff is None else ("after" if created >= cutoff else "before"),
        "members": members,
        "pending_at_build": [int(n) for n, _, ci in rows if ci and ci.strip() != "green"],
        "dropped": _dropped(body),
        "build_start": build_start,
        "phases": {
            "build": _secs(build_start, build_end),
            "local_validation": _secs(build_end, created),
            "suite": _suite(body),
            "ci_first_pass": ci_first,
            "ci_queue_wait": wait, "ci_execution": execution,
            "ci_orchestration": orchestration,
            "failure_repair": (_secs(first_end, final_end) if reruns else 0)
            if first_end else None,
            "awaiting_merge": _secs(final_end, merged),
            "batch_wall": _secs(created, merged or closed),
        },
        "critical_path": [j["name"] for j in chain] if chain else None,
        "critical_path_note": note,
        "reruns": reruns,
        "jobs": [{k: j[k] for k in ("name", "attempt", "wait", "execution", "conclusion",
                                    "never_started", "required")} for j in jobs],
        "replaced_by": None,
    }


def _members(art: dict, batches: list[dict]) -> list[dict]:
    seen: dict[int, list[dict]] = {}
    for b in batches:
        for n in b["members"] + [d["number"] for d in b["dropped"]]:
            seen.setdefault(n, []).append(b)
    out = []
    for n, where in sorted(seen.items()):
        info = art["members"].get(str(n), {})
        where = sorted(where, key=lambda b: (b["build_start"] or _t(b["created"])))
        carried = [b for b in where if b["outcome"] == "merged" and n in b["members"]]
        own = _t(info.get("mergedAt"))
        # A member that took commits after a merged batch carried an older
        # head lands with a later batch: the one merged when GitHub marked
        # the member merged (#860: carried by #876, merged by #882).
        if own is not None:
            landed = next((b for b in carried
                           if abs(_secs(_t(b["merged"]), own)) <= 300), None)
        elif (info.get("state") or "").upper() == "CLOSED":
            # GitHub marked no merge (#850 squashed, so its members read
            # closed): the batch that carried it is the only record.
            landed = carried[0] if carried else None
        else:
            landed = None   # still open: a batch carried an older head only
        first_build = where[0]["build_start"] or _t(where[0]["created"])
        events = info.get("events") or []

        def at(e):
            return _t(e.get("at"))
        labels = [at(e) for e in events if e.get("event") == "labeled"
                  and e.get("label") == art.get("label", LABEL)
                  and _secs(at(e), first_build) >= -CHAIN_TOLERANCE]
        labeled = max(labels, default=None)
        opened = _t(info.get("createdAt"))
        ready = [at(e) for e in events if e.get("event") == "ready_for_review"
                 and labeled and at(e) <= labeled]
        if ready:
            opened = max(ready)
        pushes = [at(e) for e in events if e.get("event") in ("committed", "head_ref_force_pushed")
                  and labeled and at(e) <= labeled]
        last_push = max(pushes, default=None)
        anchor = max(opened, last_push) if opened and last_push else opened
        outcome = "merged" if landed else (
            "carried, merged outside a batch" if carried and own is not None else
            "carried, still open" if carried and (info.get("state") or "").upper() == "OPEN" else
            "dropped" if any(n in [d["number"] for d in b["dropped"]] for b in where)
            else where[-1]["outcome"])
        out.append({
            "number": n, "title": info.get("title"), "batches": [b["number"] for b in where],
            "batch": landed["number"] if landed else None, "outcome": outcome,
            "rebatched": bool(landed) and any(b is not landed for b in carried),
            "regime": (landed or where[0])["regime"],
            "admission": _secs(opened, labeled),
            "revising": None if labeled is None or opened is None else (
                max(0, _secs(opened, last_push)) if last_push else 0),
            "after_last_commit": _secs(anchor, labeled),
            "awaiting_build": _secs(labeled, first_build),
            "rebuild_repair": _secs(first_build, landed["build_start"]) if landed else None,
            "lead": _secs(labeled, _t(landed["merged"])) if landed else None,
        })
    return out


def _stat(values: list) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "median": None, "max": None, "total": None}
    return {"n": len(values), "median": statistics.median(values), "max": max(values),
            "total": sum(values)}


def report(art: dict, cutoff: str | None = None) -> dict:
    """Phase split of every batch and member in a collected artifact."""
    when = _when(cutoff) if cutoff else None
    batches = sorted((_batch(b, when) for b in art["batches"]),
                     key=lambda b: b["created"])
    for b in batches:
        if b["outcome"] == "closed":
            later = [o for o in batches if o["created"] > b["created"]
                     and set(o["members"]) & set(b["members"])]
            b["replaced_by"] = later[0]["number"] if later else None
    members = _members(art, batches)
    # A dropped member that later merged on its own is a direct merge (#884).
    in_batch = {m["number"] for m in members if m["outcome"] == "merged"}
    direct = [dict(d, regime=None if when is None else
                   ("after" if _t(d["mergedAt"]) >= when else "before"))
              for d in art.get("direct_merges") or [] if d["number"] not in in_batch]
    summary = {}
    for regime in (("before", "after") if when else (None,)):
        bs = [b for b in batches if b["regime"] == regime]
        landed = [b for b in bs if b["outcome"] == "merged"]
        ms = [m for m in members if m["regime"] == regime and m["outcome"] == "merged"]
        rebatched = sum(1 for m in ms if m["rebatched"])
        summary[regime or "all"] = {
            "batches": len(landed), "closed": sum(b["outcome"] == "closed" for b in bs),
            "members": len(ms),
            "dropped": sum(len(b["dropped"]) for b in bs),
            "rebatched": rebatched,
            "reruns": sum(len(b["reruns"]) for b in bs),
            "reruns_on_lanes_not_required": sum(
                1 for b in bs for r in b["reruns"]
                if r["failed_jobs"] and not any(j["required"] for j in r["failed_jobs"])),
            "direct_merges": sum(d["regime"] == regime for d in direct),
            "phases": {**{p: _stat([b["phases"][p] for b in landed]) for p in BATCH_PHASES},
                       **{p: _stat([m[p] for m in ms]) for p in MEMBER_PHASES}},
        }
    for b in batches:
        b["build_start"] = b["build_start"].strftime("%Y-%m-%dT%H:%M:%SZ") \
            if b["build_start"] else None
    return {"since": art.get("since"), "collected_at": art.get("collected_at"),
            "cutoff": cutoff, "batches": batches, "members": members,
            "direct_merges": direct, "summary": summary}


# ----------------------------------------------------------------- render

def _d(seconds) -> str:
    if seconds is None:
        return "-"
    seconds = round(seconds)
    h, rest = divmod(abs(seconds), 3600)
    m, s = divmod(rest, 60)
    sign = "-" if seconds < 0 else ""
    if h:
        return f"{sign}{h}h{m:02d}m"
    return f"{sign}{m}m{s:02d}s" if m else f"{sign}{s}s"


PHASE_NAMES = {
    "admission": "awaiting admission (opened -> label)",
    "revising": "  revising (opened -> last commit)",
    "after_last_commit": "  after the last commit (-> label)",
    "awaiting_build": "awaiting build (label -> first build)",
    "rebuild_repair": "rebuild repair (first build -> landing build)",
    "build": "build (merge commits)",
    "local_validation": "local validation (head -> batch PR)",
    "suite": "  of which the local full suite",
    "ci_first_pass": "CI first pass (batch PR -> last attempt-1 job)",
    "ci_queue_wait": "  critical path: queue wait (incl. the concurrent-job cap)",
    "ci_execution": "  critical path: execution",
    "ci_orchestration": "  critical path: dispatch and job gaps",
    "failure_repair": "failure repair (reruns after the first pass)",
    "awaiting_merge": "awaiting merge (last job -> merged)",
    "batch_wall": "batch PR wall (opened -> merged)",
    "lead": "member lead (label -> merged)",
}
ORDER = ("admission", "revising", "after_last_commit", "awaiting_build", "rebuild_repair",
         "build", "local_validation", "suite", "ci_first_pass", "ci_queue_wait",
         "ci_execution", "ci_orchestration", "failure_repair", "awaiting_merge",
         "batch_wall", "lead")


def render(rep: dict) -> str:
    out = [f"# Integration batch timeline", "",
           f"Batches opened since {rep['since']}, collected {rep['collected_at']}"
           + (f"; regimes split at {rep['cutoff']}." if rep["cutoff"] else "."), ""]
    for regime, s in rep["summary"].items():
        out += [f"## {regime}: n={s['batches']} merged batches, {s['members']} landed "
                f"members ({s['closed']} closed batches, {s['dropped']} drops, "
                f"{s['rebatched']} re-batched members, {s['reruns']} reruns, "
                f"{s['reruns_on_lanes_not_required']} of them for lanes the ruleset does not "
                f"require, {s['direct_merges']} direct merges)",
                "", "| Phase | n | median | max | total |", "| --- | --- | --- | --- | --- |"]
        for p in ORDER:
            st = s["phases"][p]
            out.append(f"| {PHASE_NAMES[p]} | {st['n']} | {_d(st['median'])} | "
                       f"{_d(st['max'])} | {_d(st['total'])} |")
        out.append("")
    out += ["## Batches", "",
            "| Batch | regime | members | outcome | build | local validation (suite) | "
            "CI first pass (queue wait / execution / dispatch) | failure repair | "
            "awaiting merge | wall |", "| --- " * 10 + "|"]
    for b in rep["batches"]:
        p = b["phases"]
        out.append(
            f"| #{b['number']} | {b['regime'] or '-'} | "
            f"{', '.join(f'#{n}' for n in b['members'])} | {b['outcome']}"
            + (f" (rebuilt as #{b['replaced_by']})" if b["replaced_by"] else "")
            + f" | {_d(p['build'])} | {_d(p['local_validation'])} ({_d(p['suite'])}) | "
            f"{_d(p['ci_first_pass'])} ({_d(p['ci_queue_wait'])} / {_d(p['ci_execution'])}"
            f" / {_d(p['ci_orchestration'])}) | {_d(p['failure_repair'])} | "
            f"{_d(p['awaiting_merge'])} | {_d(p['batch_wall'])} |")
    out += ["", "## Critical paths, reruns, drops", ""]
    for b in rep["batches"]:
        path = " -> ".join(b["critical_path"]) if b["critical_path"] \
            else f"none ({b['critical_path_note']})"
        out.append(f"- #{b['number']} critical path: {path}"
                   + (f" (note: {b['critical_path_note']})"
                      if b["critical_path"] and b["critical_path_note"] else ""))
        for r in b["reruns"]:
            causes = "; ".join(
                f"{j['name']} {j['conclusion']}"
                + (" (required)" if j["required"] else
                   " (not required; a required gate failed with it)"
                   if any(k["required"] for k in r["failed_jobs"]) else " (not a required lane)")
                + (" , no runner acquired" if j["never_started"] else "")
                + (f": {j['annotations'][0]}" if j["annotations"] else "")
                for j in r["failed_jobs"]) or "no failed job recorded"
            out.append(f"  - rerun {r['run']} attempt {r['attempt']}: clicked "
                       f"{_d(r['wait'])} after that run's failed attempt, ran "
                       f"{_d(r['execution'])} (queue wait {_d(r['queue_wait'])}); cause: {causes}")
        for d in b["dropped"]:
            out.append(f"  - dropped #{d['number']}: {d['reason']}")
        if b["pending_at_build"]:
            out.append("  - CI on the member's own head was not green at build: "
                       + ", ".join(f"#{n}" for n in b["pending_at_build"]))
    out += ["", "## Members", "",
            "| Member | batches | outcome | awaiting admission (revising / after last commit) | "
            "awaiting build | rebuild repair | lead |", "| --- " * 7 + "|"]
    for m in rep["members"]:
        out.append(f"| #{m['number']} | {', '.join(f'#{n}' for n in m['batches'])} | "
                   f"{m['outcome']}{' (re-batched)' if m['rebatched'] else ''} | {_d(m['admission'])} ({_d(m['revising'])} / "
                   f"{_d(m['after_last_commit'])}) | {_d(m['awaiting_build'])} | "
                   f"{_d(m['rebuild_repair'])} | {_d(m['lead'])} |")
    if rep["direct_merges"]:
        out += ["", "## Merged to master outside a batch", ""]
        out += [f"- #{d['number']} {d['mergedAt']} ({d['headRefName']}): {d['title']}"
                for d in rep["direct_merges"]]
    return "\n".join(out) + "\n"


def _default(value):
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    raise TypeError(type(value))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect", help="read batches, their CI and members from GitHub")
    c.add_argument("--since", required=True, help="YYYY-MM-DD or an ISO time (UTC)")
    c.add_argument("--out", required=True, help="where to write the artifact JSON")
    r = sub.add_parser("report", help="phase split of a collected artifact (offline)")
    r.add_argument("artifact")
    r.add_argument("--cutoff", help="ISO time splitting batches into before/after")
    r.add_argument("--json", help="also write the report as JSON here")
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            art = collect(args.since)
            Path(args.out).write_text(json.dumps(art, indent=1) + "\n", encoding="utf-8")
            print(f"wrote {len(art['batches'])} batches and {len(art['members'])} members "
                  f"to {args.out}")
            return 0
        art = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
        if art.get("schema") != SCHEMA:
            raise Refusal(f"{args.artifact} is not a batch timeline artifact (schema "
                          f"{art.get('schema')!r}, expected {SCHEMA}); collect it again")
        rep = report(art, args.cutoff)
        if args.json:
            Path(args.json).write_text(json.dumps(rep, indent=1, default=_default) + "\n",
                                       encoding="utf-8")
        sys.stdout.write(render(rep))
        return 0
    except Refusal as exc:
        print(f"batch_timeline: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
