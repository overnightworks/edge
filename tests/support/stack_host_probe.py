"""What the `test_stack_host_probe` tests share with other test modules."""

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Behaviour:
    """What the loopback server answers; `release` ends a body that never finishes."""

    def __init__(self, status=200, headers=(), stall_seconds=0.0, announce_body=False):
        self.status = status
        self.headers = list(headers)
        self.stall_seconds = stall_seconds
        self.announce_body = announce_body
        self.requests: list[dict] = []
        self.release = threading.Event()


@contextmanager
def serving(behaviour: Behaviour):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            behaviour.requests.append(
                {
                    "line": self.requestline,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
            )
            if behaviour.stall_seconds:
                behaviour.release.wait(behaviour.stall_seconds)
            self.send_response(behaviour.status)
            for name, value in behaviour.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", "100" if behaviour.announce_body else "0")
            self.end_headers()
            if behaviour.announce_body:
                behaviour.release.wait(5)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        behaviour.release.set()
        server.shutdown()
        server.server_close()


RAY = ("CF-RAY", "8a1b2c3d-FRA")
