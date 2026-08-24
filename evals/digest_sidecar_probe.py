"""Session-digest sidecar quality probe (spec 2026-08-24, resolved Q3).

Generates session digests against a configured OpenAI-compatible extractor
endpoint and persists them for human review. Production enablement of
``memory.dream.digest_enabled`` gates on this review: narrative prose is a
harder ask than slot extraction for a small CPU sidecar, so the digests it
would actually write get eyeballed before the flag flips.

Input is one "session" per BEAM batch (built with the same ``format_turn``
stamps the eval adapter stores, so the probe context matches what the
digest stage would see in a BEAM run), or arbitrary text files. The
map-reduce segmentation mirrors ``generate_digests_stage`` exactly.

Usage:
    python evals/digest_sidecar_probe.py --beam-root ../beam-harness \
        --tier 100K --chat 1 --sessions 1,2,3 --tag sidecar-0824
    python evals/digest_sidecar_probe.py --context-file ctx.txt --tag adhoc

Endpoint resolution honours the PSEUDOLIFE_DREAM_* env vars (the ops
contract, via ``resolve_endpoints``); ``--base-url``/``--model`` override.
Writes evals/results/digest-sidecar-probe-<tag>.json (persist by default —
a probe whose evidence lives only in a terminal was never really run).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pseudolife_memory.memory.dream import (OpenAICompatExtractor,
                                            resolve_endpoints,
                                            split_session_context)
from pseudolife_memory.utils.config import DreamConfig

from beam_adapter import format_turn, load_chat_turns


def build_session_contexts(beam_root: Path, tier: str, chat: str,
                           sessions: list[int] | None) -> list[dict]:
    """One context per BEAM batch, rendered the way the digest stage sees a
    session: a ``Session:`` header plus one line per stored turn. Layout
    matches beam_adapter.iter_chats (``<root>/chats/<tier>/<n>``)."""
    chat_dir = beam_root / "chats" / tier / str(chat)
    turns = load_chat_turns(chat_dir)
    by_batch: dict[int, list[str]] = {}
    ordinal = 0
    for turn in turns:
        ordinal += 1
        by_batch.setdefault(turn["batch"], []).append(
            f"- (beam) {format_turn(turn, ordinal)}")
    out = []
    for batch, lines in sorted(by_batch.items()):
        if sessions and batch not in sessions:
            continue
        title = f"BEAM {tier} chat {chat} session {batch}"
        out.append({"label": title,
                    "context": f"Session: {title}\n" + "\n".join(lines)})
    return out


def digest_one(extractor: OpenAICompatExtractor, context: str, *,
               context_chars: int, target_chars: int) -> dict:
    """The stage's exact generation path: single call under the cap,
    map-reduce over line-boundary segments above it."""
    t0 = time.time()
    parts = split_session_context(context, context_chars)
    calls = 0
    if len(parts) == 1:
        digest = extractor.summarize_session(parts[0],
                                             target_chars=target_chars)
        calls = 1
    else:
        seg_digests: list[str] = []
        digest = None
        for part in parts:
            seg = extractor.summarize_session(part,
                                              target_chars=target_chars)
            calls += 1
            if seg is None:
                seg_digests = []
                break
            seg_digests.append(seg)
        if seg_digests:
            digest = extractor.summarize_session("\n\n".join(seg_digests),
                                                 target_chars=target_chars)
            calls += 1
    return {"digest": digest, "malformed": digest is None,
            "segments": len(parts), "calls": calls,
            "context_chars": len(context),
            "digest_chars": len(digest) if digest else 0,
            "seconds": round(time.time() - t0, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--beam-root", type=Path,
                    help="BEAM harness checkout (data/<tier>/chat_<n>)")
    ap.add_argument("--tier", default="100K")
    ap.add_argument("--chat", default="1")
    ap.add_argument("--sessions",
                    help="comma-separated BEAM batch numbers (default: all)")
    ap.add_argument("--context-file", type=Path, action="append", default=[],
                    help="arbitrary session-context text file (repeatable)")
    ap.add_argument("--base-url", help="override the resolved endpoint")
    ap.add_argument("--model", help="override the resolved model")
    ap.add_argument("--target-chars", type=int, default=None,
                    help="default: DreamConfig.digest_target_chars")
    ap.add_argument("--context-chars", type=int, default=None,
                    help="default: DreamConfig.digest_context_chars")
    ap.add_argument("--tag", required=True,
                    help="artifact suffix: digest-sidecar-probe-<tag>.json")
    args = ap.parse_args()

    cfg = DreamConfig()
    target = args.target_chars or cfg.digest_target_chars
    cap = args.context_chars or cfg.digest_context_chars
    r = resolve_endpoints(cfg)
    base_url = args.base_url or r["primary_url"] or r["fallback_url"]
    model = args.model or r["primary_model"] or r["fallback_model"]
    if not (base_url and model):
        print("no extractor endpoint: set PSEUDOLIFE_DREAM_* env vars or "
              "pass --base-url/--model", file=sys.stderr)
        return 2
    extractor = OpenAICompatExtractor(
        base_url, model,
        max_tokens=max(cfg.extractor_max_tokens, target),
        # The eval-harness convention: pin the llama-server prompt cache
        # off so repeated probes are comparable (see DreamConfig notes).
        extra_body={"cache_prompt": False})

    contexts: list[dict] = []
    if args.beam_root:
        sessions = ([int(s) for s in args.sessions.split(",")]
                    if args.sessions else None)
        contexts += build_session_contexts(args.beam_root, args.tier,
                                           args.chat, sessions)
    for path in args.context_file:
        contexts.append({"label": path.name,
                         "context": path.read_text(encoding="utf-8")})
    if not contexts:
        print("nothing to digest: pass --beam-root or --context-file",
              file=sys.stderr)
        return 2

    from pseudolife_memory.memory.dream import ExtractorError

    rows = []
    for c in contexts:
        try:
            row = {"label": c["label"],
                   **digest_one(extractor, c["context"],
                                context_chars=cap, target_chars=target)}
        except ExtractorError as exc:
            # One transport blip must not discard the rows already
            # generated — record it and keep sweeping; the artifact is
            # the evidence either way.
            row = {"label": c["label"], "digest": None, "malformed": False,
                   "error": str(exc), "context_chars": len(c["context"]),
                   "digest_chars": 0, "segments": 0, "calls": 0,
                   "seconds": 0}
        rows.append(row)
        status = ("ERROR" if row.get("error")
                  else "MALFORMED" if row["malformed"]
                  else f"{row['digest_chars']}ch")
        print(f"{c['label']}: {status} "
              f"({row['segments']} seg, {row['calls']} calls, "
              f"{row['seconds']}s)")

    ok = [r_ for r_ in rows
          if not r_["malformed"] and not r_.get("error")]
    summary = {
        "probe": "digest-sidecar",
        "endpoint": {"base_url": base_url, "model": model},
        "target_chars": target, "context_chars_cap": cap,
        "n_sessions": len(rows),
        "n_malformed": sum(1 for r_ in rows if r_["malformed"]),
        "n_errors": sum(1 for r_ in rows if r_.get("error")),
        "mean_digest_chars": (round(sum(r_["digest_chars"] for r_ in ok)
                                    / len(ok)) if ok else 0),
        "mean_seconds": (round(sum(r_["seconds"] for r_ in ok) / len(ok), 2)
                         if ok else 0),
        "date": time.strftime("%Y-%m-%d"),
        "rows": rows,
    }
    out = (Path(__file__).parent / "results"
           / f"digest-sidecar-probe-{args.tag}.json")
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\nwrote {out} — review the digests before enabling "
          f"memory.dream.digest_enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
