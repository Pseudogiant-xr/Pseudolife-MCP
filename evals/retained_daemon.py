"""Public Rust daemon transport and disposable bank lifecycle for retained evals."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import http.client
import json
import os
from pathlib import Path
import re
from secrets import token_hex, token_urlsafe
import tempfile
from urllib.parse import urlencode, urlsplit

from rust.daemon.harness import daemons, mcp_wire


SOURCE = "bench"
PULL_LIMIT = 100
DREAM_TALLIES = ("claims", "inserted", "superseded", "literal_flagged", "literal_dropped")


class BenchError(RuntimeError):
    """An incomplete or invalid measurement."""


def digest_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def request_json(origin, method, path, *, token=None, body=None, timeout=120):
    cls = http.client.HTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        body = json.dumps(body, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    conn = cls(origin.hostname, origin.port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            raise BenchError(f"{method} {path.split('?')[0]} returned HTTP {response.status}")
        result = json.loads(response.read())
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise BenchError(f"{method} {path.split('?')[0]} failed ({type(exc).__name__})") from None
    finally:
        conn.close()
    if not isinstance(result, dict):
        raise BenchError(f"{method} {path.split('?')[0]} did not return an object")
    return result


@dataclass(frozen=True)
class Endpoint:
    url: str
    model: str
    profile: str
    served_model: str | None = None

    def __post_init__(self):
        origin = urlsplit(self.url)
        try:
            port = origin.port
        except ValueError:
            raise BenchError("model URL has an invalid port") from None
        if (origin.scheme not in {"http", "https"} or not origin.hostname or port == 0
                or origin.username is not None or origin.query or origin.fragment):
            raise BenchError("use an explicit HTTP(S) model base URL without credentials or query")
        if not self.model.strip() or not self.profile.strip() or self.served_model == "":
            raise BenchError("model and serving profile must be specified")

    @property
    def expected_model(self):
        return self.served_model or self.model

    def provenance(self):
        return {"request_model": self.model, "served_model": self.expected_model,
                "profile": self.profile}

    def verify(self):
        origin = urlsplit(self.url)
        result = request_json(origin, "GET", origin.path.rstrip("/") + "/models", timeout=5)
        models = result.get("data")
        if not isinstance(models, list) or not any(
                isinstance(row, dict) and row.get("id") == self.expected_model for row in models):
            raise BenchError("the model endpoint does not advertise the expected model")

    def chat(self, system, user, *, max_tokens):
        origin = urlsplit(self.url)
        result = request_json(origin, "POST", origin.path.rstrip("/") + "/chat/completions",
                              body={"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                                    "chat_template_kwargs": {"enable_thinking": False},
                                    "messages": [{"role": "system", "content": system},
                                                 {"role": "user", "content": user}]})
        if result.get("model") != self.expected_model:
            raise BenchError("chat completion was served by an unexpected model")
        try:
            text = result["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError):
            raise BenchError("model response has no completion") from None
        if not isinstance(text, str) or not text.strip():
            raise BenchError("model response has an empty completion")
        return text.strip()


def rpc_result(response, operation):
    try:
        if response["status"] != 200:
            raise BenchError(f"{operation} returned HTTP {response['status']}")
        if (mcp_wire.header(response, "content-type") or "").startswith("text/event-stream"):
            (data,) = mcp_wire.sse_data(response)
        else:
            data = response["body"]
        frame = json.loads(data)
        if "error" in frame:
            raise BenchError(f"{operation} returned an MCP request error")
        result = frame["result"]
        if not isinstance(result, dict):
            raise TypeError
        return result
    except (KeyError, TypeError, ValueError):
        raise BenchError(f"{operation} returned an invalid MCP response") from None


class Bank:
    def __init__(self, session, token):
        self.session = session
        self.token = token
        self.origin = urlsplit(f"http://127.0.0.1:{session.port}")

    def get(self, path, **query):
        return request_json(self.origin, "GET", f"{path}?{urlencode(query)}" if query else path,
                            token=self.token)

    def tool(self, name, **args):
        result = rpc_result(self.session.tool(name, args), name)
        if result.get("isError"):
            raise BenchError(f"{name} returned an MCP tool error")
        try:
            value = result.get("structuredContent")
            if value is None:
                value = json.loads(result["content"][0]["text"])
        except (IndexError, KeyError, TypeError, ValueError):
            raise BenchError(f"{name} returned invalid tool content") from None
        if not isinstance(value, dict):
            raise BenchError(f"{name} did not return an object")
        return value

    def store(self, text):
        if self.tool("memory_store", text=text, source=SOURCE).get("stored") is not True:
            raise BenchError("memory_store declined an input turn; the cell is incomplete")

    def search(self, query, top_k):
        result = self.get("/api/search", q=query, source=SOURCE, top_k=top_k,
                          disable_recency_boost="true")
        if not isinstance(result.get("entries"), list):
            raise BenchError("Rust search returned no entry list")
        return result

    def verify_extractor(self, endpoint):
        status = self.tool("memory_dream", action="status")
        model = status.get("primary_model_served") or status.get("primary_model")
        if (status.get("extractor_mode") != "primary" or status.get("fallback_url")
                or (status.get("primary_url") or "").rstrip("/") != endpoint.url.rstrip("/")
                or model != endpoint.expected_model):
            raise BenchError("the daemon did not report the pinned primary extractor identity")


def drain_dream(tool, *, stored):
    """Count acknowledged inputs, allowing only bounded recovery from an extraction hold."""
    tally = dict.fromkeys(DREAM_TALLIES, 0)
    consumed = holds = 0
    cursor = float("-inf")
    while True:
        result = tool("memory_dream", action="run", limit=PULL_LIMIT)
        if (result.get("error") or result.get("skipped") or result.get("acknowledgement_failed")
                or result.get("extractor") != "primary" or result.get("quarantined")
                or result.get("events_pass_failed")):
            raise BenchError("Rust dream did not finish on the pinned primary extractor")
        if result.get("extractor_failed"):
            holds += 1
            if result.get("hold_phase") != "extract" or holds >= 8:
                raise BenchError("Rust dream did not recover its held extraction batch")
            continue
        holds = 0
        pulled = result.get("pulled")
        if type(pulled) is not int or not 0 <= pulled <= min(PULL_LIMIT, stored - consumed):
            raise BenchError("Rust dream reported an unexpected number of inputs")
        if pulled == 0:
            if consumed != stored:
                raise BenchError("Rust dream drained before acknowledging the stored inputs")
            return tally
        next_cursor = result.get("cursor")
        if type(next_cursor) not in (int, float) or not cursor < next_cursor < float("inf"):
            raise BenchError("Rust dream cursor did not advance")
        cursor = next_cursor
        consumed += pulled
        for key in tally:
            count = result.get(key, 0)
            if type(count) is not int or count < 0:
                raise BenchError(f"Rust dream returned an invalid {key} tally")
            tally[key] += count


def bench_config(*, contiguity=0, timeline=False, floor=0.2, cortex_bm25=False,
                 known_facts_window=0, slot_match=None):
    # JSON is valid YAML and keeps external inputs from becoming YAML keys.
    cortex = {"auto_promote": False, "protect_provenance": False, "guard_min_score": floor}
    if slot_match is not None:
        cortex["dream_slot_match_threshold"] = slot_match
    return {"embedding": {"device": "cpu", "backend": "onnx"}, "memory": {
        "surprise_threshold": 0, "meta_filter": {"enabled": False}, "cortex": cortex,
        "search": {"contiguity_neighbors": contiguity, "timeline_channel": timeline},
        "bm25": {"cortex_enabled": cortex_bm25}, "reranker": {"enabled": False},
        "lessons": {"enabled": False, "infer_outcomes": False},
        "graph_insight": {"enabled": False}, "deep_dream": {"judges_enabled": False,
                                                           "auto_tick": False},
        "dream": {"enabled": False, "stall_notice": False, "extract_relations": False,
                  "chronicle": False, "eligible_sources": [SOURCE],
                  "known_facts_window": known_facts_window},
    }}


@dataclass(frozen=True)
class DaemonSetup:
    binary: Path
    revision: str
    onnx_dir: Path
    ort_library: Path
    scratch: Path

    def provenance(self):
        if not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise BenchError("provide the full revision of the checkout-built daemon")
        files = {p.relative_to(self.onnx_dir).as_posix(): digest_file(p)
                 for p in sorted(self.onnx_dir.rglob("*")) if p.is_file()}
        if not files:
            raise BenchError("provide the pinned CPU ONNX embedding export directory")
        return {"engine": "rust", "revision": self.revision, "binary_sha256": digest_file(self.binary),
                "embedding_files": files, "ort_sha256": digest_file(self.ort_library)}

    @contextmanager
    def bank(self, config, extractor=None):
        from rust.daemon.harness import pgdisposable

        host = os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT")
        login = os.environ.get("PSEUDOLIFE_TEST_PG_LOGIN_FILE")
        if (not host or not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", host)
                or not 0 < int(host.split(":")[1]) < 65536
                or host != pgdisposable.HOST_PORT or not login
                or Path(login).resolve() != pgdisposable.LOGIN_FILE.resolve()):
            raise BenchError("set an explicit loopback test PostgreSQL address and test login file")
        if (os.environ.get(pgdisposable.BASE_ENV)
                or any(os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGOPTIONS"))):
            raise BenchError("clear PostgreSQL routing overrides and the harness base-bank setting")
        if not self.binary.is_file() or not self.ort_library.is_file() or not self.onnx_dir.is_dir():
            raise BenchError("provide an existing Rust daemon binary, ONNX export and ORT library")
        pg = pgdisposable.Slice("w0e")
        name = pg.name(f"{pg.PREFIX}quality_{token_hex(6)}")
        self.scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="rust-quality-", dir=self.scratch) as scratch:
            home = daemons.make_home(Path(scratch), "daemon", json.dumps(config, allow_nan=False))
            dsn = pg.create(name)
            try:
                yield from self._serve(home, dsn, extractor)
            finally:
                pg.drop(name)

    def _serve(self, home, dsn, extractor):
        port, token = daemons.free_port(), token_urlsafe(32)
        env = daemons.base_env(home, {})
        env = {key: value for key, value in env.items() if not key.startswith("PSEUDOLIFE_")}
        env.update({"PSEUDOLIFE_MCP_HOST": "127.0.0.1", "PSEUDOLIFE_MCP_TOKEN": token,
                    "PSEUDOLIFE_MCP_DATA_DIR": str(home / "data"), "PSEUDOLIFE_MCP_DATABASE_URL": dsn,
                    "PSEUDOLIFE_RELEASE_CHECK": "0", "PSEUDOLIFE_DAEMON_ORT_THREADS": "1",
                    "PSEUDOLIFE_DAEMON_ONNX_DIR": str(self.onnx_dir.resolve()),
                    "ORT_DYLIB_PATH": str(self.ort_library.resolve()),
                    "APPDATA": str(home), "LOCALAPPDATA": str(home),
                    "XDG_CONFIG_HOME": str(home), "OMP_NUM_THREADS": "1"})
        if extractor is not None:
            env.update({"PSEUDOLIFE_DREAM_EXTRACTOR_MODE": "primary",
                        "PSEUDOLIFE_DREAM_BASE_URL": extractor.url,
                        "PSEUDOLIFE_DREAM_MODEL": extractor.model,
                        "PSEUDOLIFE_DREAM_TIMEOUT_SECONDS": "90"})
        daemon = daemons.rust_daemon(self.binary.resolve(), home, port, env)
        try:
            daemon.start()
            session = mcp_wire.Session(port, token)
            try:
                rpc_result(session.init, "initialize")
                if not session.sid or session.initialized["status"] != 202:
                    raise BenchError("Rust daemon did not open an authenticated MCP session")
                bank = Bank(session, token)
                if extractor is not None:
                    bank.verify_extractor(extractor)
                yield bank
            finally:
                if session.sid:
                    session.close()
        finally:
            daemon.stop()


def add_daemon_arguments(parser):
    parser.add_argument("--daemon", type=Path)
    parser.add_argument("--daemon-revision")
    parser.add_argument("--onnx-dir", type=Path)
    parser.add_argument("--ort-library", type=Path)
    parser.add_argument("--scratch", type=Path)


def daemon_setup(args):
    if not all((args.daemon, args.daemon_revision, args.onnx_dir, args.ort_library, args.scratch)):
        raise BenchError("provide --daemon, --daemon-revision, --onnx-dir, --ort-library and --scratch")
    return DaemonSetup(args.daemon, args.daemon_revision, args.onnx_dir, args.ort_library, args.scratch)


def add_endpoint_arguments(parser, role):
    for key in ("url", "model", "profile", "served-model"):
        parser.add_argument(f"--{role}-{key}")


def endpoint_args(args, role):
    values = [getattr(args, f"{role}_{key}") for key in ("url", "model", "profile", "served_model")]
    if not all(values[:3]):
        raise BenchError(f"provide --{role}-url, --{role}-model and --{role}-profile")
    return Endpoint(*values)
