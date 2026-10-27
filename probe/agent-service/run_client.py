"""The run client of the login command's R2 (#469, agent-runtime#440 B9): it runs in `agent-smoke`
(`compose run ... --entrypoint python`), posts one run that grants the tool `step`, reads its
event stream and closes the connection when the host writes `close` on its stdin. The host's
verdicts read what it prints, and nothing else of it.

Stdin: the person's token, the workload's token, and later the host's one `close` line.
Stdout: only the lines of `CLIENT_LINE`, built from status codes, event names, enum values and
one wall time, never a message, text, tool result, prompt or run id. A value outside the closed
sets of the wire package prints as `other`.

Only the standard library at the top: `main` imports the runtime packages, which only the
service image has, so a host test imports this module and hands the pure parts its own sets.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterable
from typing import IO, NamedTuple

R2_PROMPT = "Call the tool step exactly once, with n set to 1. Call no other tool."
R2_TOOLS = {"step": "read_only"}
CLOSE_COMMAND = "close"
STATUS_SECONDS = 30  # twice the close's own bounds, 14.6 s after the loss
STATUS_POLL_SECONDS = 0.5
SMOKE_DIRECTORY = "/opt/smoke"
RUN_ID = re.compile(r"[0-9a-f-]{36}")
MAXIMUM_BODY_BYTES = 65536
CLIENT_LINE = re.compile(
    r"answer (?P<answer>\d{3})(?: (?P<answer_code>[a-z_]+))?"
    r"|event (?P<event>[a-z_]+)"
    r"|closed at=(?P<closed_at>\d+\.\d{3})"
    r"|status (?P<state>[a-z_]+) (?P<loss>[a-z_]+)"
    r"|broke off (?P<broke_off>\w+)"
)
NO_LOSS = "none"
REFUSED = "refused"
OTHER = "other"


class Vocabulary(NamedTuple):
    """The closed sets a printed value must belong to, from the wire package."""

    events: frozenset[str]
    states: frozenset[str]
    losses: frozenset[str]
    codes: frozenset[str]

    def shown(self, value: object, allowed: frozenset[str]) -> str:
        return value if isinstance(value, str) and value in allowed else OTHER


def vocabulary_from(wire) -> Vocabulary:
    """The sets of `agent_runtime_client.wire`: the members of three enums, and the `type` name
    of each event class of the `RunEvent` union."""
    classes = wire.RunEvent.__args__[0].__args__
    names = (cls.model_fields["type"].annotation.__args__[0] for cls in classes)
    return Vocabulary(
        events=frozenset(names),
        states=frozenset(member.value for member in wire.RunState),
        losses=frozenset(member.value for member in wire.ClientLoss),
        codes=frozenset(member.value for member in wire.RejectionCode),
    )


def answer_line(status: int, code: object, vocabulary: Vocabulary) -> str:
    if code is None:
        return f"answer {status}"
    return f"answer {status} {vocabulary.shown(code, vocabulary.codes)}"


def event_line(name: object, vocabulary: Vocabulary) -> str:
    return f"event {vocabulary.shown(name, vocabulary.events)}"


def closed_line(moment: float) -> str:
    return f"closed at={moment:.3f}"


def status_line(state: object, loss: object, vocabulary: Vocabulary) -> str:
    shown_loss = NO_LOSS if loss is None else vocabulary.shown(loss, vocabulary.losses)
    return f"status {vocabulary.shown(state, vocabulary.states)} {shown_loss}"


def refused_line(code: object, vocabulary: Vocabulary) -> str:
    return f"status {REFUSED} {vocabulary.shown(code, vocabulary.codes)}"


def broke_off_line(problem: BaseException) -> str:
    return f"broke off {type(problem).__name__}"


class RunClient:
    def __init__(
        self,
        *,
        connect: Callable[[], http.client.HTTPConnection],
        frames: Callable[[http.client.HTTPResponse], Iterable],
        vocabulary: Vocabulary,
        body: bytes,
        headers: dict[str, str],
        stdin: IO[str],
        out: IO[str],
        clock: Callable[[], float] = time.monotonic,
        pause: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.connect, self.frames, self.vocabulary = connect, frames, vocabulary
        self.body, self.headers, self.stdin, self.out = body, headers, stdin, out
        self.clock, self.pause, self.wall = clock, pause, wall
        self.lock = threading.Lock()
        self.closing = False
        self.finished = False
        self.run_id: str | None = None

    def say(self, line: str) -> None:
        self.out.write(line + "\n")
        self.out.flush()

    def run(self) -> int:
        connection = self.connect()
        closer: threading.Thread | None = None
        try:
            connection.request("POST", "/runs", self.body, self.headers)
            response = connection.getresponse()
            if response.status != http.HTTPStatus.OK:
                self.say(answer_line(response.status, refusal_code(response), self.vocabulary))
                return 1
            self.say(answer_line(response.status, None, self.vocabulary))
            closer = threading.Thread(target=self.close_on_command, args=(connection,), daemon=True)
            closer.start()
            self.read_events(response)
        except (OSError, http.client.HTTPException, ValueError) as problem:
            if not self.closing:
                self.say(broke_off_line(problem))
        finally:
            with self.lock:
                self.finished = True
            if closer is not None and self.closing:
                closer.join()
            connection.close()
        return self.read_status()

    def read_events(self, response: http.client.HTTPResponse) -> None:
        for frame in self.frames(response):
            self.say(event_line(frame.event, self.vocabulary))
            run_id = frame.data.get("run_id")
            if self.run_id is None and isinstance(run_id, str) and RUN_ID.fullmatch(run_id):
                self.run_id = run_id

    def close_on_command(self, connection: http.client.HTTPConnection) -> None:
        """On the host's `close`: shut the socket down, stamp t0, close it, say so."""
        if self.stdin.readline().strip() != CLOSE_COMMAND:
            return
        with self.lock:
            if self.finished:
                return
            self.closing = True
            opened = connection.sock
            opened.shutdown(socket.SHUT_RDWR)
            moment = self.wall()
            opened.close()
            self.say(closed_line(moment))

    def read_status(self) -> int:
        """`GET /runs/{id}` every `STATUS_POLL_SECONDS` until the run has ended or
        `STATUS_SECONDS` have passed; one line says the last read."""
        if self.run_id is None:
            return 1
        end = self.clock() + STATUS_SECONDS
        while True:
            try:
                ended, line = self.status_once()
            except (OSError, http.client.HTTPException, ValueError, KeyError) as problem:
                self.say(broke_off_line(problem))
                return 1
            if ended or self.clock() >= end:
                self.say(line)
                return 0
            self.pause(STATUS_POLL_SECONDS)

    def status_once(self) -> tuple[bool, str]:
        connection = self.connect()
        try:
            connection.request("GET", f"/runs/{self.run_id}", None, self.headers)
            response = connection.getresponse()
            if response.status != http.HTTPStatus.OK:
                return True, refused_line(refusal_code(response), self.vocabulary)
            document = json.loads(response.read(MAXIMUM_BODY_BYTES))
            line = status_line(document["state"], document["client_lost"], self.vocabulary)
            return document["ended_at"] is not None, line
        finally:
            connection.close()


def refusal_code(response: http.client.HTTPResponse) -> object:
    """The `code` of the service's typed refusal, or None when the body is not one."""
    try:
        document = json.loads(response.read(MAXIMUM_BODY_BYTES))
        return document["code"] if isinstance(document, dict) else None
    except ValueError:
        return None


def main() -> int:
    sys.path.insert(0, SMOKE_DIRECTORY)  # `-I` implies safe-path, which leaves it out
    import smoke
    from agent_runtime_client import wire

    person, workload = (sys.stdin.readline().strip() for _ in range(2))
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {person}",
        smoke.WORKLOAD_TOKEN_HEADER: workload,
    }
    return RunClient(
        connect=lambda: http.client.HTTPConnection(smoke.SERVICE_HOST, smoke.SERVICE_PORT),
        frames=smoke.frames,
        vocabulary=vocabulary_from(wire),
        body=smoke.request_body(R2_PROMPT, R2_TOOLS),
        headers=headers,
        stdin=sys.stdin,
        out=sys.stdout,
    ).run()


if __name__ == "__main__":
    sys.exit(main())
