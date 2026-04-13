# Copyright 2026 Sakilabo Corporation Ltd.
# SPDX-License-Identifier: UPL-1.0

"""Tests for cancellable_http_client."""

from __future__ import annotations

import http.server
import socket
import socketserver
import threading
import time
import unittest

import cancellable_http_client as client


# ---------------------------------------------------------------------------
# Test servers
# ---------------------------------------------------------------------------

class _OKHandler(http.server.BaseHTTPRequestHandler):
    """Returns 200 with a fixed body."""

    def do_GET(self):
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silence request logs


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    """Waits 5 seconds before sending headers."""

    def do_GET(self):
        time.sleep(5)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


class _DisconnectBeforeResponseHandler(socketserver.BaseRequestHandler):
    """Reads the request, then closes the connection without responding."""

    def handle(self):
        try:
            self.request.recv(4096)
        except OSError:
            pass


class _DisconnectDuringBodyHandler(http.server.BaseHTTPRequestHandler):
    """Sends headers with Content-Length, then closes mid-body."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.end_headers()
        self.wfile.write(b"partial")
        self.wfile.flush()
        # Close without sending the remaining 993 bytes.

    def log_message(self, format, *args):
        pass


class _BlackholeHandler(socketserver.BaseRequestHandler):
    """Accepts the TCP connection, then does nothing."""

    def handle(self):
        # Block until the socket is closed or an error occurs.
        try:
            while self.request.recv(4096):
                pass
        except OSError:
            pass


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True


def _start_http_server(handler_class):
    """Start an HTTP server on a random port and return (server, port)."""
    server = _ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _start_tcp_server(handler_class):
    """Start a raw TCP server on a random port and return (server, port)."""
    server = _ThreadingTCPServer(("127.0.0.1", 0), handler_class)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNormalRequest(unittest.TestCase):
    """Happy-path tests against a working server."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_http_server(_OKHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_get_200(self):
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.start()
        req.wait()
        self.assertTrue(req.done)
        self.assertIsNone(req.error)
        self.assertIsNotNone(req.response)
        self.assertEqual(req.response.status, 200)
        self.assertEqual(req.response.body, b"hello")
        req.close()

    def test_context_manager(self):
        with client.Request(f"http://127.0.0.1:{self.port}/") as req:
            req.start()
            req.wait()
            self.assertEqual(req.response.status, 200)
        self.assertTrue(req.done)

    def test_response_headers(self):
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.start()
        req.wait()
        self.assertIsNotNone(req.response.getheader("Content-Length"))
        self.assertIsInstance(req.response.getheaders(), list)
        req.close()

    def test_repr_states(self):
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        self.assertIn("pending", repr(req))
        req.start()
        req.wait()
        self.assertIn("status=200", repr(req))
        req.close()


class TestCancel(unittest.TestCase):
    """Tests for close() aborting an in-flight request."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_tcp_server(_BlackholeHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_close_unblocks_wait(self):
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.start()
        # close from another thread after a short delay
        threading.Timer(0.2, req.close).start()
        start = time.monotonic()
        req.wait()
        elapsed = time.monotonic() - start
        self.assertTrue(req.done)
        self.assertIsNone(req.response)
        self.assertLess(elapsed, 5)

    def test_close_before_start(self):
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.close()
        self.assertTrue(req.done)
        # start after close is a no-op
        req.start()
        self.assertTrue(req.done)
        self.assertIsNone(req.response)

    def test_close_is_idempotent(self):
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.start()
        req.close()
        req.close()
        req.close()
        self.assertTrue(req.done)

    def test_close_sets_no_error(self):
        """close() should not populate error — it's an intentional abort."""
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.start()
        time.sleep(0.1)  # let the worker connect
        req.close()
        req.wait()
        self.assertIsNone(req.response)
        # error may or may not be set depending on timing, but response must be None


class TestTimeout(unittest.TestCase):
    """Tests for the wall-clock timeout parameter."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_tcp_server(_BlackholeHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_timeout_closes_request(self):
        req = client.Request(
            f"http://127.0.0.1:{self.port}/", timeout=0.3
        )
        req.start()
        start = time.monotonic()
        req.wait()
        elapsed = time.monotonic() - start
        self.assertTrue(req.done)
        self.assertIsNone(req.response)
        self.assertLess(elapsed, 3)
        self.assertGreaterEqual(elapsed, 0.2)
        req.close()

    def test_no_timeout_by_default(self):
        """Without timeout, the request blocks until close()."""
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.start()
        completed = req.wait(timeout=0.3)
        self.assertFalse(completed)
        req.close()


class TestSlowServer(unittest.TestCase):
    """Tests against a server that delays its response."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_http_server(_SlowHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_cancel_during_slow_response(self):
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.start()
        threading.Timer(0.2, req.close).start()
        start = time.monotonic()
        req.wait()
        elapsed = time.monotonic() - start
        self.assertTrue(req.done)
        self.assertIsNone(req.response)
        self.assertLess(elapsed, 5)

    def test_timeout_during_slow_response(self):
        req = client.Request(
            f"http://127.0.0.1:{self.port}/", timeout=0.3
        )
        req.start()
        start = time.monotonic()
        req.wait()
        elapsed = time.monotonic() - start
        self.assertTrue(req.done)
        self.assertIsNone(req.response)
        self.assertLess(elapsed, 5)


class TestFinalizeCallback(unittest.TestCase):
    """Tests for add_finalize_callback()."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_http_server(_OKHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_callback_runs_before_done(self):
        results = []

        def cb(req):
            results.append(("cb", req.done))

        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.add_finalize_callback(cb)
        req.start()
        req.wait()
        self.assertEqual(results, [("cb", False)])
        self.assertTrue(req.done)
        req.close()

    def test_callback_on_close(self):
        called = threading.Event()

        def cb(req):
            called.set()

        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.add_finalize_callback(cb)
        req.close()
        self.assertTrue(called.is_set())

    def test_callback_exception_is_swallowed(self):
        def bad_cb(req):
            raise ValueError("boom")

        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.add_finalize_callback(bad_cb)
        req.start()
        req.wait()
        self.assertTrue(req.done)
        req.close()


class TestServerDisconnect(unittest.TestCase):
    """Tests for the server closing the connection unexpectedly."""

    def test_disconnect_before_response(self):
        server, port = _start_tcp_server(_DisconnectBeforeResponseHandler)
        try:
            req = client.Request(f"http://127.0.0.1:{port}/")
            req.start()
            req.wait()
            self.assertTrue(req.done)
            self.assertIsNone(req.response)
            self.assertIsNotNone(req.error)
            req.close()
        finally:
            server.shutdown()

    def test_disconnect_during_body(self):
        server, port = _start_http_server(_DisconnectDuringBodyHandler)
        try:
            req = client.Request(f"http://127.0.0.1:{port}/")
            req.start()
            req.wait()
            self.assertTrue(req.done)
            # resp.read() raises IncompleteRead when the connection
            # closes before Content-Length bytes have been received.
            self.assertIsNone(req.response)
            self.assertIsInstance(req.error, http.client.IncompleteRead)
            req.close()
        finally:
            server.shutdown()


class TestEdgeCases(unittest.TestCase):
    """Miscellaneous edge-case tests."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_http_server(_OKHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_start_is_idempotent(self):
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.start()
        req.start()  # should be no-op
        req.wait()
        self.assertEqual(req.response.status, 200)
        req.close()

    def test_wait_before_start_raises(self):
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        with self.assertRaises(RuntimeError):
            req.wait()
        req.close()

    def test_wait_timeout_returns_false(self):
        server, port = _start_tcp_server(_BlackholeHandler)
        try:
            req = client.Request(f"http://127.0.0.1:{port}/")
            req.start()
            result = req.wait(timeout=0.1)
            self.assertFalse(result)
            req.close()
        finally:
            server.shutdown()

    def test_connection_refused(self):
        # Find a port that nothing is listening on
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        req = client.Request(f"http://127.0.0.1:{port}/")
        req.start()
        req.wait()
        self.assertTrue(req.done)
        self.assertIsNone(req.response)
        self.assertIsNotNone(req.error)
        req.close()

    def test_post_with_body(self):
        req = client.Request(
            f"http://127.0.0.1:{self.port}/",
            method="POST",
            body=b'{"key": "value"}',
            headers={"Content-Type": "application/json"},
        )
        req.start()
        req.wait()
        self.assertTrue(req.done)
        # Server may return 501 or close the connection — either way, done is True
        self.assertTrue(req.response is not None or req.error is not None)
        req.close()


class _KeepAliveHandler(http.server.BaseHTTPRequestHandler):
    """Returns a small body over a keep-alive connection.

    The entire response (headers + body) fits in a single TCP segment,
    so getresponse() buffers the body inside Python's BufferedReader.
    Because the connection stays open, the OS socket buffer is empty
    after that — select() would never report it as readable.

    This is the exact scenario that caused an infinite loop in v1.1,
    where the read loop was gated on select().
    """

    def do_GET(self):
        body = b"buffered"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        # Keep the connection open — do NOT close.
        # Block until the client disconnects.
        try:
            while self.rfile.read(4096):
                pass
        except OSError:
            pass

    def log_message(self, format, *args):
        pass


class _LargeBodyHandler(http.server.BaseHTTPRequestHandler):
    """Returns a body larger than the default max_response_size."""

    BODY_SIZE = 6 * 1024 * 1024  # 6 MB

    def do_GET(self):
        body = b"x" * self.BODY_SIZE
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class TestMaxResponseSize(unittest.TestCase):
    """Tests for max_response_size limiting."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_http_server(_LargeBodyHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_response_too_large(self):
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.start()
        req.wait()
        self.assertTrue(req.done)
        self.assertIsNone(req.response)
        self.assertIsInstance(req.error, client.ResponseTooLargeError)
        req.close()

    def test_unlimited_with_none(self):
        req = client.Request(
            f"http://127.0.0.1:{self.port}/", max_response_size=None
        )
        req.start()
        req.wait()
        self.assertTrue(req.done)
        self.assertIsNone(req.error)
        self.assertIsNotNone(req.response)
        self.assertEqual(len(req.response.body), _LargeBodyHandler.BODY_SIZE)
        req.close()

    def test_unlimited_with_zero(self):
        req = client.Request(
            f"http://127.0.0.1:{self.port}/", max_response_size=0
        )
        req.start()
        req.wait()
        self.assertTrue(req.done)
        self.assertIsNone(req.error)
        self.assertIsNotNone(req.response)
        self.assertEqual(len(req.response.body), _LargeBodyHandler.BODY_SIZE)
        req.close()

    def test_custom_limit(self):
        req = client.Request(
            f"http://127.0.0.1:{self.port}/", max_response_size=100
        )
        req.start()
        req.wait()
        self.assertTrue(req.done)
        self.assertIsNone(req.response)
        self.assertIsInstance(req.error, client.ResponseTooLargeError)
        req.close()


class TestBufferedReaderRegression(unittest.TestCase):
    """Regression test for the v1.1 infinite-loop bug.

    When the server sends a small response over a keep-alive connection,
    getresponse() buffers the body inside Python's BufferedReader.  The
    OS socket buffer is then empty, so select() never reports readability.
    In v1.1 the read loop was gated on select(), causing it to spin
    forever without calling read1().  The fix (v1.2) replaced select()
    with a short socket timeout so read1() is always called.
    """

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_http_server(_KeepAliveHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_small_body_over_keepalive(self):
        req = client.Request(f"http://127.0.0.1:{self.port}/")
        req.start()
        completed = req.wait(timeout=3)
        self.assertTrue(completed, "request did not complete — likely stuck in read loop")
        self.assertIsNone(req.error)
        self.assertIsNotNone(req.response)
        self.assertEqual(req.response.status, 200)
        self.assertEqual(req.response.body, b"buffered")
        req.close()


if __name__ == "__main__":
    unittest.main()
