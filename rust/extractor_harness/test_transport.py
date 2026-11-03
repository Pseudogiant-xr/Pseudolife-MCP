"""Fixture HTTP requests preserve wire bytes without a late body or retry."""
import io
import unittest
from unittest.mock import patch

import run


class Peer:
    def __init__(self, body, *, abort=False):
        self.body = body
        self.abort = abort
        self.writes = []
        self.closed = False

    def sendall(self, data):
        self.writes.append(data)
        # An early-refusal peer may close as soon as it sees headers. Reject
        # a partial request deterministically rather than racing the OS here.
        if self.abort or not data.endswith(b"\r\n\r\n" + self.body):
            raise ConnectionAbortedError("fixture peer closed after headers")

    def setsockopt(self, *args):
        pass

    def makefile(self, *args):
        return io.BytesIO(b"HTTP/1.0 501 fixture\r\nContent-Length: 7\r\n\r\nrefused")

    def close(self):
        self.closed = True


class Transport(unittest.TestCase):
    def test_early_refusal_receives_one_complete_request(self):
        peer = Peer(b"{}")
        with patch("socket.create_connection", return_value=peer) as connect:
            self.assertEqual(run.call(12345, "PUT", "/models", {}), (501, b"refused"))
        connect.assert_called_once()
        self.assertEqual(peer.writes, [
            b"PUT /models HTTP/1.1\r\nHost: 127.0.0.1:12345\r\n"
            b"Accept-Encoding: identity\r\nContent-Length: 2\r\n"
            b"Content-Type: application/json\r\n\r\n{}"
        ])
        self.assertTrue(peer.closed)

    def test_transport_failure_is_not_retried(self):
        peer = Peer(b"{}", abort=True)
        with patch("socket.create_connection", return_value=peer) as connect:
            with self.assertRaises(ConnectionAbortedError):
                run.call(12345, "PUT", "/models", {})
        connect.assert_called_once()
        self.assertEqual(len(peer.writes), 1)
        self.assertTrue(peer.closed)

    def test_absent_empty_and_invalid_bodies_are_preserved(self):
        for method, body in (("GET", None), ("POST", b""), ("POST", b"bad")):
            with self.subTest(method=method, body=body):
                peer = Peer(body or b"")
                with patch("socket.create_connection", return_value=peer):
                    self.assertEqual(run.call(12345, method, "/missing", body), (501, b"refused"))
                self.assertEqual(len(peer.writes), 1)
                headers, sent_body = peer.writes[0].split(b"\r\n\r\n", 1)
                self.assertEqual(sent_body, body or b"")
                if body is None:
                    self.assertNotIn(b"Content-Length", headers)
                else:
                    self.assertIn(f"Content-Length: {len(body)}".encode(), headers)


if __name__ == "__main__":
    unittest.main()
