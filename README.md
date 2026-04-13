# cancellable_http_client

A tiny, dependency-free HTTP client for Python with **cancellable in-flight requests** and **hard wall-clock timeout**.

- Standard library only — no `requests`, no `httpx`, no `urllib3`.
- Single file, ~350 lines (~180 lines of code).
- Synchronous API that plays well with `threading`-based workers.
- Safe `close()` from any thread, at any time, including mid-transfer.
- Hard wall-clock `timeout` that bounds the entire request.

## Why it exists

Python has no clean way to interrupt a thread that is blocked on a socket read. `concurrent.futures.Future.cancel()` does nothing once the task has started, and `requests` / `urllib.urlopen()` give you no handle to abort an in-flight request.

The one primitive that *does* work is closing the underlying socket: any pending `recv()` immediately unblocks with an error. This module wraps that trick behind a tiny, boring API so you don't have to reinvent it — or worry about the lifecycle edge cases — every time you need it.

If your codebase is built around `asyncio`, you don't need this; use `httpx` and `task.cancel()` instead. This module targets the very real case where you have existing threaded code and you want one HTTP call in the middle of it to be cancellable, without rewriting everything to be async.

## Usage

```python
import time
import cancellable_http_client as client

req = client.Request("https://example.com/")
req.start()           # the actual TCP connection happens here
start = time.monotonic()
while not req.done:
    if time.monotonic() - start > 5:
        print("taking too long, aborting...")
        req.close()   # interrupts the request if it's still in-flight
    req.wait(0.1)     # wait a bit before checking again
if req.error:
    print(f"failed: {req.error}")
elif req.response and req.response.status == 200:
    print(req.response.body)
req.close()           # safe to call any time, even mid-flight
```

You can also use it as a context manager:

```python
with client.Request("https://example.com/") as req:
    req.start()
    req.wait(timeout=5)
    ...
# close() is called automatically on exit
```

### Sharing a thread pool

By default each `Request` spawns its own daemon thread. To reuse a pool instead, assign an `Executor` to the module-level attribute:

```python
from concurrent.futures import ThreadPoolExecutor
client.executor = ThreadPoolExecutor(max_workers=8)
```

## API

### `Request(url, method="GET", headers=None, body=b"", socket_timeout=30, timeout=None, max_response_size=5*1024*1024)`

Construct a request. No network I/O happens here — connection failures are reported via `error` after `start()`.

- **`socket_timeout`** — per-socket-operation timeout in seconds, passed to `http.client.HTTPConnection`. Must be a positive number — `None` or `0` may cause the worker thread to block indefinitely.
- **`timeout`** — wall-clock limit in seconds for the entire request. Triggers `close()` automatically if the request is not done in time. `None` disables.
- **`max_response_size`** — maximum response body size in bytes. If the body exceeds this limit, the request fails with `ResponseTooLargeError`. Defaults to 5 MB. Set to `None` or `0` for unlimited.
- **`start()`** — kick off the request. Non-blocking.
- **`wait(timeout=None) -> bool`** — block until the request finishes. Returns `True` on completion, `False` on timeout.
- **`close()`** — abort the request and release resources. Safe to call any time, from any thread, any number of times.
- **`done`** *(property)* — `True` once the request has finished (success, failure, or close).
- **`response`** — a `Response` object on success, otherwise `None`.
- **`error`** — the exception raised during the request, or `None`.

### `ResponseTooLargeError`

Raised when the response body exceeds `max_response_size`. Available as `cancellable_http_client.ResponseTooLargeError`.

### `Response`

A read-only, socket-free container exposing the same attributes as `http.client.HTTPResponse`:

- `status`, `reason`, `version`
- `headers` (an `http.client.HTTPMessage`)
- `body` (`bytearray`, eagerly read)
- `getheader(name, default=None)`, `getheaders()`

## Defaults

| Parameter | Scope | Default |
|---|---|---|
| `socket_timeout` | Per socket operation (connect, send, recv) | 30 s |
| `timeout` | Wall-clock limit on the entire request | None (no limit) |
| `max_response_size` | Maximum response body size | 5 MB |

## Robust timeout

Most Python HTTP clients set a *per-socket-operation* timeout (`socket.settimeout`). This leaves several gaps:

- **Slow drip** — a server that sends one byte every 29 seconds never triggers a 30-second socket timeout, yet the total transfer can take arbitrarily long.
- **DNS resolution** — `socket.getaddrinfo()` is a blocking C library call with no timeout parameter. Python cannot interrupt it.
- **Total elapsed time** — there is no built-in way to cap the wall-clock time of an entire request across connection, TLS handshake, sending, and receiving.

The `timeout` parameter addresses this. When it fires it calls `close()`, which immediately unblocks any pending socket operation by closing the underlying connection. This gives you a hard upper bound on how long `wait()` will block — something that `socket_timeout` alone cannot guarantee.

**Note:** `timeout` is disabled by default (`None`). Set it explicitly when you need a wall-clock guarantee.

## Comparison with existing libraries

| | cancellable_http_client | httpx | requests |
|---|---|---|---|
| Cancel an in-flight request from another thread | ✅ | ⚠️ async, [unreliable](https://github.com/encode/httpx/issues/1461) | ⚠️ hacky |
| Hard wall-clock timeout on entire request | ✅ | ⚠️ per-operation | ⚠️ per-operation |
| Synchronous API | ✅ | ✅ (also async) | ✅ |
| No third-party dependencies | ✅ | ❌ | ❌ |
| Line count | ~350 (~180 code) | thousands | thousands |
| Fits a threading-based worker | ✅ | ❌ | ⚠️ |
| Redirects, cookies, User-Agent | ⚠️ manual | ✅ | ✅ |

`httpx` is a good choice if you are already in an `asyncio` world, though `task.cancel()` on in-flight requests [can leave the connection pool in a broken state](https://github.com/encode/httpx/issues/1461). `requests` does not offer a reliable way to interrupt an in-flight call; `Session.close()` does not forcibly close active sockets ([psf/requests#5633](https://github.com/psf/requests/issues/5633)).

## Limitations

This library is a thin wrapper around `http.client` and does not provide the high-level conveniences found in `requests` or `httpx`:

- No automatic redirect following
- No cookie management
- No default User-Agent header
- No HTTP/2 support

These are all `http.client` limitations, not restrictions added by this library. You can still handle them manually via the `headers` parameter.

## Tests

```
python -m unittest discover -s tests -v
```

No third-party test dependencies. Tests use local throwaway servers (normal, slow, blackhole, mid-body disconnect) to exercise cancellation, timeout, and error paths without touching the network.

## License

Copyright 2026 Sakilabo Corporation Ltd.
Licensed under the Universal Permissive License v 1.0
([UPL-1.0](https://oss.oracle.com/licenses/upl/)).

---

## References (for the eventual public release)

Background reading and related work collected during the design of this module. Useful as citations in the README or a launch blog post.

### Prior art / closest existing work

- **["TIL: Stopping Requests Mid Flight" — haykot.dev](https://haykot.dev/blog/til-stopping-requests-mid-flight/)**
  A blog post describing the "close the socket from another thread" trick as a personal discovery. The same underlying idea as this module, but kept as a snippet rather than packaged as a library.
  
- **[httpcore on PyPI](https://pypi.org/project/httpcore/)**
  the low-level HTTP engine behind `httpx`. Supports cancellation via async task cancellation; relies on `asyncio` or `trio`.
  
- **[HTTPX](https://www.python-httpx.org/)**
  modern high-level HTTP client; cancellation is done via `task.cancel()` in an async context.
  
- **[asyncio-cancel-token](https://asyncio-cancel-token.readthedocs.io/en/latest/cancel_token.html)**
  a cancellation-token utility for `asyncio`-based code.

### The underlying Python pain points

- **[Graceful exit from ThreadPoolExecutor when blocked on IO — discuss.python.org](https://discuss.python.org/t/graceful-exit-from-threadpoolexecutor-when-blocked-on-io-problem-and-possible-enhancement/80380)**
  Ongoing discussion acknowledging that Python has no clean way to cancel a worker that is blocked on I/O. This module is effectively a targeted workaround for the HTTP-specific case.
  
- **[`threading` — Python docs](https://docs.python.org/3/library/threading.html)**
  `Thread` has no `cancel()` / `interrupt()`; cooperation via `Event` is the only sanctioned approach.
  
- **[`Session.close()` does not close underlying sockets — psf/requests#5633](https://github.com/psf/requests/issues/5633)**
  Illustrates why "just use `requests` and close the session" is not a reliable answer.
  
- **[Unclosed socket in urllib when ftp request times out after connect — cpython#140691](https://github.com/python/cpython/issues/140691)**
  A related stdlib lifecycle bug — background for why we deliberately take full control of the connection object.

### Related stdlib primitives this module builds on

- **[`http.client` — Python docs](https://docs.python.org/3/library/http.client.html)**
  the low-level HTTP protocol implementation we wrap.
- **[`threading.Event` — Python docs](https://docs.python.org/3/library/threading.html#event-objects)**
  used internally to signal completion.
