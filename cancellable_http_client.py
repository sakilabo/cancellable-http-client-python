# Copyright 2026 Sakilabo Corporation Ltd.
# SPDX-License-Identifier: UPL-1.0
#
# Licensed under the Universal Permissive License v 1.0 as shown at
# https://oss.oracle.com/licenses/upl/

"""Cancellable HTTP client.

A tiny standard-library-only HTTP client whose in-flight request can be
aborted at any time by closing the underlying socket.

Usage
-----
    import time
    import cancellable_http_client as client

    req = client.Request("https://example.com/")
    req.start()           # the actual TCP connection happens here
    start = time.monotonic()
    while not req.done:
        if time.monotonic() - start > 5:
            print("taking too long, aborting...")
            req.close()   # this will interrupt the request if it's still in-flight
        req.wait(0.1)     # wait a bit before checking again (when done, wait() returns immediately)
    if req.error:
        print(f"failed: {req.error}")
    elif req.response and req.response.status == 200:
        print(req.response.body)
    req.close()           # safe to call any time, even mid-flight

The ``timeout`` parameter triggers ``close()`` automatically after the
given number of seconds, providing a hard wall-clock limit on the entire
request — unlike ``socket_timeout``, which only limits individual socket
operations and cannot bound the total elapsed time.

By default each Request spawns its own daemon thread. To share a pool,
assign a concurrent.futures.Executor to ``cancellable_http_client.executor``.
"""

from __future__ import annotations

import http.client
import logging
import select
import threading
import urllib.parse

from concurrent.futures import Executor
from typing import Callable

_logger = logging.getLogger(__name__)

_DEFAULT_MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5 MB
_SELECT_POLL_INTERVAL = 0.1  # seconds — for close() responsiveness


class ResponseTooLargeError(Exception):
    """Raised when the response body exceeds *max_response_size*."""


# If the user assigns an Executor here, requests are dispatched to it
# instead of spawning a fresh daemon thread per call.
executor: Executor | None = None


class Response:
    """A read-only HTTPResponse-compatible object.

    Exposes the same attributes as ``http.client.HTTPResponse``
    (``status``, ``reason``, ``version``, ``headers``, ``body``) but holds
    its body in memory, so it can be inspected freely after the underlying
    socket has been closed.
    """

    def __init__(self, resp: http.client.HTTPResponse, body: bytearray) -> None:
        self.status: int = resp.status
        self.reason: str = resp.reason
        self.version: int = resp.version
        self.headers: http.client.HTTPMessage = resp.msg
        self.body: bytearray = body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self.headers.items())

    def __repr__(self) -> str:
        return f"<Response status={self.status} reason={self.reason!r}>"


class Request:
    """A single cancellable HTTP request.

    Parameters
    ----------
    url     : Target URL (scheme://host[:port]/path?query).
    method  : HTTP method.
    headers : Request headers.
    body    : Request body bytes.
    socket_timeout : Socket timeout in seconds (per socket operation).
    timeout : Total request timeout in seconds. If the request does not
        complete within this time after ``start()``, it is automatically
        closed. ``None`` means no limit.

    Attributes
    ----------
    done     : True once the request has finished (success, failure, or close).
    response : Response object on success, otherwise None.
    error    : Exception raised during the request, or None on success.
    """

    def __init__(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        socket_timeout: float = 30,
        timeout: float | None = None,
        max_response_size: int | None = _DEFAULT_MAX_RESPONSE_SIZE,
    ) -> None:
        self.response: Response | None = None
        self.error: Exception | None = None

        self._event = threading.Event()
        self._lock = threading.Lock()
        self._closed: bool = False
        self._started: bool = False
        self._conn: http.client.HTTPConnection | None = None
        self._callbacks: list[Callable[["Request"], None]] = []
        self._timeout: float | None = timeout
        self._timer: threading.Timer | None = None
        self._max_response_size: int | None = max_response_size or None

        self._url: str = url
        parsed = urllib.parse.urlparse(url)
        self._method: str = method
        self._headers: dict[str, str] = headers or {}
        self._req_body: bytes | None = body
        self._path: str = parsed.path or "/"
        if parsed.query:
            self._path = f"{self._path}?{parsed.query}"

        try:
            if parsed.scheme == "https":
                self._conn = http.client.HTTPSConnection(
                    parsed.hostname or "", parsed.port, timeout=socket_timeout
                )
            else:
                self._conn = http.client.HTTPConnection(
                    parsed.hostname or "", parsed.port, timeout=socket_timeout
                )
        except Exception as e:
            self.error = e
            self._event.set()

    def __enter__(self) -> "Request":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __repr__(self) -> str:
        if self.response is not None:
            state = f"status={self.response.status}"
        elif self.error is not None:
            state = f"error={self.error!r}"
        elif self._closed:
            state = "closed"
        elif self._started:
            state = "running"
        else:
            state = "pending"
        return f"<Request {self._method} {self._url} {state}>"

    @property
    def done(self) -> bool:
        """True once the request has finished (success, failure, or close).

        When ``done`` is True, all finalize callbacks have already run.
        """
        return self._event.is_set()

    def start(self) -> None:
        """Start the request.

        Calling start() more than once, or after close(), is a no-op.
        If scheduling the worker fails, ``error`` is set and the request
        finishes immediately.
        """
        with self._lock:
            if self._started or self._closed:
                return
            self._started = True

        if self._timeout is not None:
            self._timer = threading.Timer(self._timeout, self.close)
            self._timer.daemon = True
            self._timer.start()

        try:
            if executor is not None:
                executor.submit(self._run)
            else:
                threading.Thread(
                    target=self._run, daemon=True, name="cancellable_http_client"
                ).start()
        except Exception as e:
            self.error = e
            with self._lock:
                conn, self._conn = self._conn, None
            self._finalize(conn)

    def add_finalize_callback(self, fn: Callable[["Request"], None]) -> None:
        """Register ``fn`` to be invoked just before the request finishes.

        Unlike ``concurrent.futures.Future.add_done_callback``, the callback
        runs *before* ``done`` becomes True and before ``wait()`` returns.
        This means an observer that sees ``done == True`` is guaranteed
        that every registered callback has already completed — useful for
        callbacks that populate fields the observer wants to read.

        The callback receives this Request as its only argument and runs
        on the worker thread that completes the request. Registering a
        callback on a Request that has already finished is a no-op.

        Exceptions raised by the callback are logged and swallowed.
        """
        with self._lock:
            self._callbacks.append(fn)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until done. Returns True if completed, False on timeout.

        Raises RuntimeError if called before ``start()`` on a request that
        has not already finished (e.g. due to an initialisation failure).
        """
        if not self._started and not self.done:
            raise RuntimeError("Request has not been started")
        return self._event.wait(timeout)

    def close(self) -> None:
        """Discard everything and finish.

        Safe to call at any time, including from another thread while the
        request is in flight. After close(), ``done`` is True and any
        ``wait()`` call returns immediately.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conn, self._conn = self._conn, None
        self._finalize(conn)

    def _run(self) -> None:
        try:
            conn = self._conn
            if conn is None:
                raise RuntimeError("connection is not available")
            conn.request(
                self._method, self._path, body=self._req_body, headers=self._headers
            )
            # If close() was called while conn.request() was in progress,
            # the socket may not have been cleaned up — bail out here.
            with self._lock:
                closed = self._closed
            if closed:
                conn.close()
                return

            sock = conn.sock
            raw_resp = conn.getresponse()
            try:
                use_select = sock is not None and sock.fileno() >= 0
            except OSError:
                use_select = False
            content_length = raw_resp.getheader("Content-Length")
            expected_size = int(content_length) if content_length else None
            body = bytearray()

            while True:
                with self._lock:
                    if self._closed:
                        break
                if use_select and sock.fileno() >= 0:
                    ready, _, _ = select.select(
                        [sock], [], [], _SELECT_POLL_INTERVAL
                    )
                else:
                    # Socket was closed by getresponse() (HTTP/1.0,
                    # will_close=True) or by a previous read1() that
                    # consumed the last chunk.  The remaining data is
                    # already in the HTTPResponse internal buffer, so
                    # read1() will return immediately without blocking.
                    ready = True
                if ready:
                    chunk = raw_resp.read1(8192)
                    if not chunk:
                        break  # EOF
                    body.extend(chunk)
                    if (
                        self._max_response_size is not None
                        and len(body) > self._max_response_size
                    ):
                        raise ResponseTooLargeError(
                            f"response body exceeds {self._max_response_size} bytes"
                        )

            # Check for incomplete body
            if expected_size is not None and len(body) < expected_size:
                raise http.client.IncompleteRead(
                    bytes(body), expected_size - len(body)
                )

            resp = Response(raw_resp, body)
            with self._lock:
                if not self._closed:
                    self.response = resp
        except Exception as e:
            # interruption by close() also lands here
            with self._lock:
                closed = self._closed
            if not closed:
                self.error = e
        finally:
            self._conn = None
            self._finalize(conn)

    def _finalize(self, conn: http.client.HTTPConnection | None) -> None:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        with self._lock:
            callbacks, self._callbacks = self._callbacks, []
        for fn in callbacks:
            try:
                fn(self)
            except Exception:
                _logger.exception("finalize callback raised")
        self._event.set()
