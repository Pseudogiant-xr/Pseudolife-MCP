"""OpenAI-compatible shim for DiffusionGemma via the patched llama-diffusion-cli.

llama.cpp PR #24423 gives DiffusionGemma a CLI but no llama-server support, so
the bench harness can't talk to it directly. This shim holds ONE persistent
llama-diffusion-cli process in conversation mode (model loaded once) and maps
each POST /v1/chat/completions onto a single turn:

    request messages -> one escaped stdin line -> reply read up to the "\\n> "
    prompt marker -> "/clear" to reset history -> OpenAI-shaped JSON response.

Requires the two local patches in the llama.cpp-diffusion checkout:
  * DG_NO_THINK=1  — chat template applied with enable_thinking=false (else the
    thought channel eats the whole token budget before any JSON appears)
  * DG_ESCAPES=1   — "\\n" in a piped stdin line becomes a real newline, so
    multi-line extraction prompts survive the line-oriented getline protocol

Endpoints: POST /v1/chat/completions, GET /health, GET /v1/models.
Turns are serialized with a lock — the bench calls sequentially anyway.

Usage:
    python evals/dg_shim.py --model evals/models/diffusiongemma-26B-A4B-it-Q4_K_M.gguf \
        [--port 8082] [--ngl 99] [--n-predict 1024] [-- extra llama-diffusion-cli args]
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_CLI = Path(r"C:\Users\HAMO9\ClaudeCode\llama.cpp-diffusion\build\bin\Release\llama-diffusion-cli.exe")
PROMPT_MARKER = "\n> "
# Empty thought stub the template still emits with thinking disabled.
_THOUGHT_RE = re.compile(r"^\s*<\|channel>thought\s*.*?<channel\|>", re.DOTALL)
# Per-turn timing summary the CLI prints to stdout after the reply.
_TIMING_RE = re.compile(r"^(total time:|throughput:).*$", re.MULTILINE)


class DiffusionCli:
    """One resident llama-diffusion-cli process driven over stdin/stdout."""

    def __init__(self, cli: Path, model: Path, ngl: int, n_predict: int,
                 extra: list[str], turn_timeout: float):
        self.turn_timeout = turn_timeout
        self.lock = threading.Lock()
        self.ready = False
        env = dict(os.environ, DG_NO_THINK="1", DG_ESCAPES="1")
        cmd = [str(cli), "-m", str(model), "-ngl", str(ngl),
               "-n", str(n_predict), "--temp", "0", "-cnv", *extra]
        self.stderr_log = open(Path(model).parent / "dg_shim.stderr.log",
                               "ab", buffering=0)
        self.proc = subprocess.Popen(
            cmd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr_log, bufsize=0)
        self._q: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        self._read_until_marker(timeout=600.0)  # model load + first "> "
        self.ready = True
        print(f"dg_shim: model loaded, cli pid={self.proc.pid}", flush=True)

    def _pump_stdout(self):
        while True:
            # bufsize=0 -> raw FileIO; read() returns as soon as bytes arrive
            chunk = self.proc.stdout.read(4096)
            if not chunk:
                self._q.put(b"")  # EOF sentinel
                return
            self._q.put(chunk)

    def _read_until_marker(self, timeout: float) -> str:
        buf = b""
        deadline = time.monotonic() + timeout
        while not buf.decode("utf-8", "replace").endswith(PROMPT_MARKER):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"no prompt marker within {timeout}s "
                    f"(got {len(buf)} bytes: {buf[-200:]!r})")
            try:
                chunk = self._q.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"llama-diffusion-cli exited rc={self.proc.returncode}")
                continue
            if chunk == b"":
                raise RuntimeError(
                    f"llama-diffusion-cli stdout closed rc={self.proc.poll()}")
            buf += chunk
        return buf.decode("utf-8", "replace")[: -len(PROMPT_MARKER)]

    def _send_line(self, line: str):
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def chat(self, content: str) -> str:
        escaped = (content.replace("\\", "\\\\")
                          .replace("\r", "")
                          .replace("\n", "\\n"))
        with self.lock:
            self._send_line(escaped)
            raw = self._read_until_marker(self.turn_timeout)
            self._send_line("/clear")
            self._read_until_marker(30.0)
        reply = _THOUGHT_RE.sub("", raw)
        reply = _TIMING_RE.sub("", reply)
        return reply.strip()


def make_handler(cli: DiffusionCli):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet the per-request noise
            pass

        def _json(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._json(200 if cli.ready else 503,
                           {"status": "ok" if cli.ready else "loading"})
            elif self.path in ("/v1/models", "/models"):
                self._json(200, {"object": "list", "data": [
                    {"id": "diffusiongemma", "object": "model"}]})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/v1/chat/completions", "/chat/completions"):
                self._json(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("content-length", 0))
                req = json.loads(self.rfile.read(n))
                content = "\n\n".join(
                    m.get("content", "") for m in req.get("messages", [])
                    if m.get("content"))
                reply = cli.chat(content)
                self._json(200, {
                    "id": f"dg-{int(time.time() * 1000)}",
                    "object": "chat.completion",
                    "model": req.get("model", "diffusiongemma"),
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0},
                })
            except Exception as e:  # noqa: BLE001 - surface anything as a 500
                print(f"dg_shim: request failed: {e}", file=sys.stderr,
                      flush=True)
                self._json(500, {"error": str(e)})

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True, type=Path)
    ap.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--ngl", type=int, default=99)
    ap.add_argument("--n-predict", type=int, default=1024)
    ap.add_argument("--turn-timeout", type=float, default=600.0)
    ap.add_argument("extra", nargs="*",
                    help="extra llama-diffusion-cli args (after --)")
    args, unknown = ap.parse_known_args()
    args.extra = [*unknown, *args.extra]  # unknown flags go to the CLI

    cli = DiffusionCli(args.cli, args.model, args.ngl, args.n_predict,
                       args.extra, args.turn_timeout)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(cli))
    print(f"dg_shim: serving on http://127.0.0.1:{args.port}/v1", flush=True)
    try:
        srv.serve_forever()
    finally:
        try:
            cli.proc.kill()
        except OSError:
            pass


if __name__ == "__main__":
    main()
