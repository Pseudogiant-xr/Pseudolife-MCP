"""mcp_wire.request reads a refusal that arrives while the body is still sending.

The Rust daemon refuses an MCP body over its 4 MiB budget after reading the
budget, then closes with the overshoot unread, so its stack resets the
connection. These servers do the same over real sockets.
"""
import http.client
import os
import socket
import struct
import threading
import time

import pytest

import mcp_wire

REFUSAL = b'{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"too large"}}'


def budget_server(budget, answer="full"):
    """Read ``budget`` body bytes, then answer per ``answer`` and close unread.

    ``full`` sends a complete 413; ``none`` closes without answering;
    ``partial`` sends headers promising more body than it sends; ``drain``
    reads the whole body first and answers 200."""
    listener = socket.create_server(("127.0.0.1", 0))
    seen = []

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                head, _, got = data.partition(b"\r\n\r\n")
                length = int(next(line.split(b":", 1)[1] for line in head.split(b"\r\n")
                                  if line.lower().startswith(b"content-length:")))
                want = length if answer == "drain" else min(budget, length)
                got = len(got)
                while got < want:
                    chunk = conn.recv(min(65536, want - got))
                    if not chunk:
                        break
                    got += len(chunk)
                seen.append(got)
                if answer == "full":
                    conn.sendall(b"HTTP/1.1 413 Payload Too Large\r\ncontent-type: application/json\r\n"
                                 b"content-length: %d\r\nconnection: close\r\n\r\n" % len(REFUSAL)
                                 + REFUSAL)
                elif answer == "partial":
                    conn.sendall(b"HTTP/1.1 413 Payload Too Large\r\ncontent-type: application/json\r\n"
                                 b"content-length: %d\r\n\r\n" % len(REFUSAL) + REFUSAL[:10])
                elif answer == "drain":
                    conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                                 b"content-length: 2\r\n\r\n{}")

    threading.Thread(target=serve, daemon=True).start()
    return listener, seen


def post(listener, body, **kwargs):
    return mcp_wire.request(listener.getsockname()[1], "POST", body,
                            [("Content-Type", "application/json")], timeout=30, **kwargs)


class UnbufferedSend(http.client.HTTPConnection):
    """A zero send buffer: the case that makes the reset race deterministic on Windows."""

    def connect(self):
        super().connect()
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 0)
        except OSError:  # macOS and the BSDs refuse 0; Linux clamps it up
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1)


def test_refusal_with_most_of_the_body_unread_is_read():
    # The daemon answers and resets with 31.75 MiB unread. On Windows 11 the
    # send returns at once and read-after-send lost the 413 in 20 of 20 calls
    # (2026-10-18); a host that blocks the send instead fails it. Either way
    # the complete response counts, and a failed send is recorded beside it.
    listener, seen = budget_server(256 * 1024)
    with listener:
        c = UnbufferedSend("127.0.0.1", listener.getsockname()[1], timeout=30)
        r = post(listener, b"a" * (32 * 1024 * 1024), connection=c)
    assert (r["status"], r["body"]) == (413, REFUSAL)
    assert r.get("send_error", "-").split(":")[0] in {"-", "ConnectionResetError", "BrokenPipeError",
                                                      "ConnectionAbortedError"}
    assert seen == [256 * 1024]


@pytest.mark.parametrize("overshoot", [47, 1])
def test_body_limit_refusal_survives_the_reset_repeatedly(overshoot):
    # The live body_limit shapes: a 4 MiB budget and a 47- or 1-byte
    # overshoot. Read after send lost the 413 to WinError 10054 in about 1 of
    # 100 calls on an idle Windows 11 host, and a reader waiting under a
    # CPython timeout in 21-30 of 3000; this reader lost 0 of 6000 (2026-10-18).
    listener, seen = budget_server(4 * 1024 * 1024)
    body = b"a" * (4 * 1024 * 1024 + overshoot)
    with listener:
        for _ in range(300):
            r = post(listener, body)
            assert (r["status"], r["body"]) == (413, REFUSAL)
    assert seen == [4 * 1024 * 1024] * 300


@pytest.mark.skipif(os.name != "nt", reason="only Windows discards received data on a reset")
def test_windows_receive_is_posted_in_the_kernel_with_the_timeout_kept():
    # The near miss that looks fixed: a reader waiting under a CPython timeout
    # sits in select() with no receive posted, and lost 22 of 3000 refusals.
    assert mcp_wire.KERNEL_TIMEOUTS
    sock = socket.socket()
    with sock:
        sock.settimeout(2.5)
        mcp_wire._kernel_timeouts(sock)
        assert sock.gettimeout() is None
        for option in (socket.SO_RCVTIMEO, socket.SO_SNDTIMEO):
            assert struct.unpack("I", sock.getsockopt(socket.SOL_SOCKET, option, 4)) == (2500,)
        sock.settimeout(None)
        mcp_wire._kernel_timeouts(sock)
        assert struct.unpack("I", sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, 4)) == (0,)


@pytest.mark.parametrize("size", [10, 8 * 1024 * 1024])
def test_daemon_that_never_answers_still_times_out(size):
    # The kernel timeout must keep the caller's bound, for the read and for a
    # send the daemon never reads.
    listener = socket.create_server(("127.0.0.1", 0))
    held = []

    def hold():
        while True:
            try:
                held.append(listener.accept()[0])
            except OSError:
                return

    threading.Thread(target=hold, daemon=True).start()
    started = time.monotonic()
    with listener, pytest.raises(TimeoutError):
        mcp_wire.request(listener.getsockname()[1], "POST", b"a" * size, [], timeout=1)
    assert time.monotonic() - started < 10
    for conn in held:
        conn.close()


@pytest.mark.parametrize("answer", ["none", "partial"])
@pytest.mark.parametrize("size, unbuffered", [(32 * 1024 * 1024, True), (4 * 1024 * 1024 + 47, False)])
def test_daemon_that_closes_without_a_complete_answer_still_fails(answer, size, unbuffered):
    # The near miss: the same reset, but nothing (or half a response) was sent.
    listener, _ = budget_server(256 * 1024, answer=answer)
    with listener, pytest.raises((ConnectionError, http.client.HTTPException)):
        c = UnbufferedSend("127.0.0.1", listener.getsockname()[1], timeout=30) if unbuffered else None
        post(listener, b"a" * size, connection=c)


def test_drained_request_carries_no_send_error_and_leaves_no_thread():
    listener, seen = budget_server(0, answer="drain")
    with listener:
        r = post(listener, b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
    assert (r["status"], r["body"], "send_error" in r) == (200, b"{}", False)
    assert seen == [40]
    assert not [t for t in threading.enumerate() if t.name == "mcp_wire-read"]


def test_supplied_connection_is_reused_across_body_exchanges():
    listener, seen = budget_server(0, answer="drain")
    with listener:
        c = http.client.HTTPConnection("127.0.0.1", listener.getsockname()[1], timeout=30)
        for _ in range(3):
            assert post(listener, b"{}", connection=c)["status"] == 200
            assert c.sock is None
    assert seen == [2, 2, 2]
