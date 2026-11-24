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
import sys
import tempfile
import threading
from urllib.parse import unquote, urlencode, urlsplit

from rust.daemon.harness import daemons, mcp_wire


SOURCE = "bench"
PULL_LIMIT = 100
CHAT_TIMEOUT_SECONDS = 600
DREAM_TIMEOUT_SECONDS = 90
DREAM_MAX_TOKENS = 2048
DREAM_TALLIES = ("claims", "inserted", "superseded", "literal_flagged", "literal_dropped", "quarantined")


class BenchError(RuntimeError):
    """An incomplete or invalid measurement."""


def redact(text, secrets=()):
    text = str(text)
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)\bpostgres(?:ql)?://[^\s\"'<>]+", "[redacted DSN]", text)
    return re.sub(r"(?i)\b(?:bearer\s+|password\s*=\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
                  "[redacted]", text)


def excerpt(value, secrets=()):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return " ".join(redact(value, secrets)[:320].split()) or "no error detail returned"


def report_incomplete(operation, error):
    print(f"{operation} incomplete ({type(error).__name__}): {excerpt(error)}", file=sys.stderr)
    return 2


def digest_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def request_json(origin, method, path, *, token=None, body=None, timeout=120, secrets=()):
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
            detail = excerpt(response.read(4096), (token, *secrets))
            raise BenchError(f"{method} {path.split('?')[0]} returned HTTP {response.status}: {detail}")
        result = json.loads(response.read())
    except (OSError, ValueError, http.client.HTTPException) as exc:
        detail = excerpt(exc, (token, *secrets))
        raise BenchError(f"{method} {path.split('?')[0]} failed ({type(exc).__name__}): {detail}") from None
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
                "profile": self.profile, "chat_timeout_seconds": CHAT_TIMEOUT_SECONDS}

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
                              timeout=CHAT_TIMEOUT_SECONDS,
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


def rpc_result(response, operation, *, secrets=()):
    try:
        if response["status"] != 200:
            detail = excerpt(response["body"], secrets)
            raise BenchError(f"{operation} returned HTTP {response['status']}: {detail}")
        if (mcp_wire.header(response, "content-type") or "").startswith("text/event-stream"):
            (data,) = mcp_wire.sse_data(response)
        else:
            data = response["body"]
        frame = json.loads(data)
        if "error" in frame:
            raise BenchError(f"{operation} returned an MCP request error: {excerpt(frame['error'], secrets)}")
        result = frame["result"]
        if not isinstance(result, dict):
            raise TypeError
        return result
    except (KeyError, TypeError, ValueError):
        raise BenchError(f"{operation} returned an invalid MCP response") from None


class Bank:
    def __init__(self, session, token, secrets=()):
        self.session = session
        self.token = token
        self.secrets = (token, *secrets)
        self.origin = urlsplit(f"http://127.0.0.1:{session.port}")

    def get(self, path, **query):
        return request_json(self.origin, "GET", f"{path}?{urlencode(query)}" if query else path,
                            token=self.token, secrets=self.secrets)

    def tool(self, name, **args):
        result = rpc_result(self.session.tool(name, args), name, secrets=self.secrets)
        if result.get("isError"):
            detail = result.get("structuredContent") or result.get("content", [])
            raise BenchError(f"{name} returned an MCP tool error: {excerpt(detail, self.secrets)}")
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
        served = status.get("primary_model_served")
        if (status.get("extractor_mode") != "primary" or status.get("fallback_url")
                or (status.get("primary_url") or "").rstrip("/") != endpoint.url.rstrip("/")
                or status.get("primary_model") != endpoint.model
                or served is not None and served != endpoint.expected_model):
            raise BenchError("the daemon did not report the pinned primary extractor identity")
        if served is None and endpoint.expected_model != endpoint.model:
            # Rust only resolves a served ID for its recognized request aliases.
            endpoint.verify()


def drain_dream(tool, *, stored):
    """Count acknowledged inputs, allowing only bounded recovery from an extraction hold."""
    tally = dict.fromkeys(DREAM_TALLIES, 0)
    consumed = holds = 0
    cursor = float("-inf")
    while True:
        result = tool("memory_dream", action="run", limit=PULL_LIMIT)
        for key in ("error", "skipped", "acknowledgement_failed", "events_pass_failed"):
            if result.get(key):
                raise BenchError(f"Rust dream reported {key}: {excerpt(result[key])}")
        if result.get("extractor") != "primary":
            raise BenchError(f"Rust dream expected the primary extractor, received {excerpt(result.get('extractor'))}")
        if result.get("extractor_failed"):
            holds += 1
            if result.get("hold_phase") != "extract" or holds >= 8:
                phase = excerpt(result.get("hold_phase"))
                error = excerpt(result.get("extractor_error") or result.get("last_error") or "")
                raise BenchError(f"Rust dream held at {phase} after {holds} attempts: {error}")
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
                  "known_facts_window": known_facts_window,
                  "extractor_timeout_seconds": DREAM_TIMEOUT_SECONDS,
                  "extractor_max_tokens": DREAM_MAX_TOKENS},
    }}


@contextmanager
def database_errors(pg):
    try:
        yield
    except SystemExit as exc:
        detail = excerpt(exc)
        if "test-login create" in detail:
            raise BenchError(f"test PostgreSQL login invalid: {detail}") from None
        raise BenchError(f"test PostgreSQL database turn refused: {detail}. Wait for the turn holder.") from None
    except pg.psycopg.OperationalError as exc:
        raise BenchError(f"test PostgreSQL connection failed: {excerpt(exc)}. Start the test PostgreSQL "
                         "server at the declared loopback address and check the test login.") from None


_DATABASE_LOCK = threading.Lock()
_PORT_LOCK = threading.Lock()
_RESERVED_PORTS = set()


def _reserve_port():
    """A free loopback port no other daemon of this process has been handed yet.

    ``free_port`` closes its probe socket, so two cells starting together could
    otherwise both be given the same port before either daemon binds it.
    """
    with _PORT_LOCK:
        for _ in range(64):
            port = daemons.free_port()
            if port not in _RESERVED_PORTS:
                _RESERVED_PORTS.add(port)
                return port
    raise BenchError("no free loopback port for a Rust daemon")


def save_failure_log(source, destination, secrets):
    if destination is None or not source.is_file():
        return
    with source.open("rb") as handle:
        size = handle.seek(0, 2)
        start = max(0, size - 16384)
        handle.seek(start)
        if start:
            handle.readline(16384)  # discard a partial line before redacting secrets
        tail = redact(handle.read().decode("utf-8", errors="replace"), secrets)[-8192:]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                     prefix=destination.name + "-", suffix=".log", delete=False) as log:
        log.write(tail)
    print(f"Rust daemon log tail saved to {log.name}", file=sys.stderr)


@dataclass(frozen=True)
class DaemonSetup:
    binary: Path
    revision: str
    onnx_dir: Path
    ort_library: Path
    scratch: Path
    failure_log: Path | None = None

    def provenance(self):
        if not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise BenchError("provide the full revision of the checkout-built daemon")
        files = {p.relative_to(self.onnx_dir).as_posix(): digest_file(p)
                 for p in sorted(self.onnx_dir.rglob("*")) if p.is_file()}
        if not files:
            raise BenchError("provide the pinned CPU ONNX embedding export directory")
        return {"engine": "rust", "revision": self.revision, "binary_sha256": digest_file(self.binary),
                "embedding_files": files, "ort_sha256": digest_file(self.ort_library)}

    def _test_database(self):
        host = os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT")
        login = os.environ.get("PSEUDOLIFE_TEST_PG_LOGIN_FILE")
        if (not host or not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", host)
                or not 0 < int(host.split(":")[1]) < 65536):
            raise BenchError("set PSEUDOLIFE_TEST_PG_HOST_PORT to the loopback test PostgreSQL address")
        if not login or not Path(login).is_file():
            raise BenchError("test PostgreSQL login missing. Run `pseudolife-mcp test-login create`, "
                             "then set PSEUDOLIFE_TEST_PG_LOGIN_FILE to the generated file.")
        try:
            from rust.daemon.harness import pgdisposable
        except ImportError:
            raise BenchError("test PostgreSQL driver unavailable. Install psycopg in the eval environment.") from None
        if host != pgdisposable.HOST_PORT or Path(login).resolve() != pgdisposable.LOGIN_FILE.resolve():
            raise BenchError("test PostgreSQL settings changed after harness import. Restart the driver "
                             "with the intended test address and login already set.")
        if (os.environ.get(pgdisposable.BASE_ENV)
                or any(os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGOPTIONS"))):
            raise BenchError("clear PostgreSQL routing overrides and the harness base-bank setting")
        if not self.binary.is_file() or not self.ort_library.is_file() or not self.onnx_dir.is_dir():
            raise BenchError("provide an existing Rust daemon binary, ONNX export and ORT library")
        return pgdisposable.Slice("w0e")

    def preflight_database(self):
        pg = self._test_database()
        with database_errors(pg):
            with pg._admin() as conn:
                role = conn.execute("SELECT rolcreatedb OR rolsuper FROM pg_roles "
                                    "WHERE rolname = current_user").fetchone()
                if not role or not role[0]:
                    raise BenchError("the test PostgreSQL login cannot create a disposable database. "
                                     "Run `pseudolife-mcp test-login create` for the test server.")

    @contextmanager
    def bank(self, config, extractor=None):
        pg = self._test_database()
        # The harness's name registry is shared module state; parallel gate cells
        # allocate, create and drop their databases one at a time.
        with _DATABASE_LOCK:
            name = pg.name(f"{pg.PREFIX}quality_{token_hex(6)}")
        self.scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="rust-quality-", dir=self.scratch) as scratch:
            home = daemons.make_home(Path(scratch), "daemon", json.dumps(config, allow_nan=False))
            with database_errors(pg), _DATABASE_LOCK:
                dsn = pg.create(name)
            try:
                yield from self._serve(home, dsn, extractor)
            finally:
                with database_errors(pg), _DATABASE_LOCK:
                    pg.drop(name)

    def _serve(self, home, dsn, extractor):
        port, token = _reserve_port(), token_urlsafe(32)
        try:
            yield from self._serve_on(home, dsn, extractor, port, token)
        finally:
            with _PORT_LOCK:
                _RESERVED_PORTS.discard(port)

    def _serve_on(self, home, dsn, extractor, port, token):
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
                        "PSEUDOLIFE_DREAM_MODEL": extractor.model})
        daemon = daemons.rust_daemon(self.binary.resolve(), home, port, env)
        password = urlsplit(dsn).password or ""
        secrets = (dsn, password, unquote(password), token)
        try:
            try:
                daemon.start()
                session = mcp_wire.Session(port, token)
                try:
                    rpc_result(session.init, "initialize", secrets=secrets)
                    if not session.sid:
                        raise BenchError("Rust daemon initialize did not return an MCP session ID")
                    if session.initialized["status"] != 202:
                        status = session.initialized["status"]
                        detail = excerpt(session.initialized["body"], secrets)
                        raise BenchError(f"MCP initialized notification returned HTTP {status}: {detail}")
                    bank = Bank(session, token, secrets)
                    if extractor is not None:
                        bank.verify_extractor(extractor)
                    yield bank
                finally:
                    if session.sid:
                        failing = sys.exc_info()[0] is not None
                        try:
                            session.close()
                        except Exception as close_error:
                            if not failing:
                                raise
                            print(f"Could not close the failed MCP session: {excerpt(close_error, secrets)}",
                                  file=sys.stderr)
            finally:
                # The production Rust daemon calls extractors over HTTP and
                # does not spawn children here. The POSIX harness stops its PID.
                if daemon.proc is None and (handle := getattr(daemon, "_out", None)) is not None:
                    handle.close()  # Popen can fail after launch() opens the log.
                daemon.stop()
        except Exception as exc:
            try:
                save_failure_log(daemon.log, self.failure_log, secrets)
            except OSError as log_error:
                print(f"Could not save the Rust daemon log tail: {excerpt(log_error, secrets)}",
                      file=sys.stderr)
            raise BenchError(f"{type(exc).__name__}: {excerpt(exc, secrets)}") from None


def add_daemon_arguments(parser):
    parser.add_argument("--daemon", type=Path)
    parser.add_argument("--daemon-revision")
    parser.add_argument("--onnx-dir", type=Path)
    parser.add_argument("--ort-library", type=Path)
    parser.add_argument("--scratch", type=Path)


def daemon_setup(args, *, output_is_file=False):
    if not all((args.daemon, args.daemon_revision, args.onnx_dir, args.ort_library, args.scratch)):
        raise BenchError("provide --daemon, --daemon-revision, --onnx-dir, --ort-library and --scratch")
    log = args.out.with_name(args.out.name + ".daemon-error") if output_is_file else args.out / "daemon-error"
    return DaemonSetup(args.daemon, args.daemon_revision, args.onnx_dir, args.ort_library,
                       args.scratch, log)


def add_endpoint_arguments(parser, role):
    for key in ("url", "model", "profile", "served-model"):
        parser.add_argument(f"--{role}-{key}")


def endpoint_args(args, role):
    values = [getattr(args, f"{role}_{key}") for key in ("url", "model", "profile", "served_model")]
    if not all(values[:3]):
        raise BenchError(f"provide --{role}-url, --{role}-model and --{role}-profile")
    return Endpoint(*values)
