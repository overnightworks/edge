"""The fake app's hold (#469, agent-runtime#440 B9 (d)): a `step` call gets no answer until its
connection closes, and nothing else waits for it. The app runs here as a real threading server on
the loopback, its log lines read from a queue."""

from __future__ import annotations

import http.client
import json
import queue
import socket
import struct
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from support.probe_support import probe_imports

LINE_SECONDS = 5


class LogQueue:
    """Stands in for stdout: each completed line is one item."""

    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()

    def write(self, text: str) -> int:
        for line in text.splitlines():
            self.lines.put(line)
        return len(text)

    def flush(self) -> None:
        """Nothing is buffered."""

    def next_line(self) -> str:
        return self.lines.get(timeout=LINE_SECONDS)


@pytest.fixture(scope="module")
def tools() -> Iterator[ModuleType]:
    with probe_imports():
        import app_tools

        yield app_tools


@pytest.fixture
def served(tools, tmp_path) -> Iterator[tuple[int, LogQueue, Path]]:
    mode_file = tmp_path / "mode"
    mode_file.write_text("hold", encoding="utf-8")
    log = LogQueue()
    server = tools.ThreadingServer(("127.0.0.1", 0), mode_file, log)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], log, mode_file
    server.shutdown()
    server.server_close()
    thread.join()


def post(port: int, document: dict) -> socket.socket:
    """Sends one JSON-RPC request on a fresh connection and leaves the answer unread."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, **document}).encode()
    peer = socket.create_connection(("127.0.0.1", port), timeout=LINE_SECONDS)
    peer.sendall(
        b"POST /mcp HTTP/1.0\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    return peer


def answered(peer: socket.socket) -> bytes:
    with peer.makefile("rb") as reader:
        return reader.read()


def call(name: str) -> dict:
    return {"method": "tools/call", "params": {"name": name, "arguments": {"n": 1}}}


def reset(peer: socket.socket) -> None:
    """A close that sends a reset: the held read raises instead of reading nothing."""
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    peer.close()


@pytest.mark.parametrize("how", ["orderly close", "reset"])
def test_a_step_call_is_held_until_the_peer_closes_and_then_logs_its_release(served, how):
    """B9 (d): the app logs `held`, answers nothing, and logs `released` on the close."""
    port, log, _ = served
    peer = post(port, call("step"))
    assert log.next_line() == "method=tools/call tool=step held"
    assert log.lines.empty()
    peer.settimeout(0.2)
    with pytest.raises(TimeoutError):
        peer.recv(1)
    if how == "reset":
        reset(peer)
    else:
        peer.close()
    assert log.next_line().startswith("method=tools/call tool=step released at=")


def test_another_request_is_answered_while_a_call_is_held(served):
    """B9 (d): a held call blocks no other request: a listing during the hold is answered."""
    port, log, _ = served
    held = post(port, call("step"))
    assert log.next_line() == "method=tools/call tool=step held"
    other = post(port, {"method": "tools/list"})
    assert b"HTTP/1.0 200" in answered(other)
    assert log.next_line() == "method=tools/list offered=step,bare,extra canary=0 bearer=0"
    held.close()
    assert log.next_line().startswith("method=tools/call tool=step released at=")


def test_a_released_time_has_three_decimals(served):
    """B9 (d): the released time has the three decimals APP_LINE expects."""
    port, log, _ = served
    post(port, call("step")).close()
    log.next_line()
    released = log.next_line()
    assert released.rsplit("at=", 1)[1].count(".") == 1
    assert len(released.rsplit(".", 1)[1]) == 3


@pytest.mark.parametrize(
    "params", [{"name": "bare"}, {"name": "extra"}, {}, "text", None], ids=lambda p: str(p)
)
def test_a_call_of_another_tool_is_answered_not_found_and_logged_as_other(served, params):
    """Matrix row 11: in hold mode, a tools/call that is not `step` is answered 'Method not
    found'."""
    port, log, _ = served
    answer = answered(post(port, {"method": "tools/call", "params": params}))
    assert b"Method not found" in answer
    assert log.next_line() == "method=tools/call tool=other"


@pytest.mark.parametrize("mode", ["relay", "canary"])
def test_a_call_outside_hold_mode_is_logged_as_any_other_method(served, mode):
    """Matrix row 11: only hold mode knows `tools/call`; elsewhere it is `method=other`,
    answered as before."""
    port, log, mode_file = served
    mode_file.write_text(mode, encoding="utf-8")
    assert b"Method not found" in answered(post(port, call("step")))
    assert log.next_line() == "method=other offered=- canary=0 bearer=0"


def test_the_step_schema_takes_an_integer_n(served):
    """B9 driven: R2's prompt sets `n` to 1, so `step` takes an integer n."""
    port, _, _ = served
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=LINE_SECONDS)
    connection.request("POST", "/mcp", json.dumps({"id": 1, "method": "tools/list"}))
    listed = json.loads(connection.getresponse().read())["result"]["tools"]
    step = next(tool for tool in listed if tool["name"] == "step")
    assert step["inputSchema"]["properties"] == {"n": {"type": "integer"}}


def test_a_nonnumeric_request_id_is_answered_without_echo_or_traceback(served, capfd):
    """defect:#469: a non-numeric request id is answered with a null id, never echoed."""
    port, _, _ = served
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=LINE_SECONDS)
    connection.request("POST", "/mcp", json.dumps({"id": "request-canary", "method": "tools/list"}))
    answer = connection.getresponse().read()
    assert json.loads(answer)["id"] is None
    assert "request-canary" not in answer.decode() + capfd.readouterr().err


def test_every_new_line_belongs_to_the_closed_vocabulary(tools):
    """Matrix row 11: every line the hold adds belongs to the closed vocabulary."""
    with probe_imports():
        import fake_app

        lines = [
            "method=tools/call tool=step held",
            "method=tools/call tool=step released at=1761566400.123",
            "method=tools/call tool=other",
        ]
        assert all(fake_app.APP_LINE.fullmatch(line) for line in lines)
        assert not fake_app.APP_LINE.fullmatch("method=tools/call tool=step released at=1.2")
        assert not fake_app.APP_LINE.fullmatch("method=tools/call tool=secret")
