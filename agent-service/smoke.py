#!/usr/bin/env python3
"""The operator's smoke of the agent service: one run, its event types and its text on stdout.

    docker compose run --rm -T agent-smoke [--cancel]

Stdin holds exactly two lines, the person's token and then the workload's, and nothing else
supplies one: there is no option, no environment variable and no file for a token. Both tokens
are removed from everything the smoke prints, errors included. Without options it starts one
run and prints each event's type and the text of the answer; exit 0 when the run succeeded.
`--cancel` starts a long answer, holds the stream open a few seconds after `run_started` so
the CLI run behind it can be seen, then closes it before any answer, to see that the service
ends that run; it fails when the run succeeded (README "The agent service", "The smoke").

Only the standard library of the service image is used.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import IO, NoReturn

PROTOCOL_VERSION = 1
PURPOSE = "chat"
WORKLOAD_TOKEN_HEADER = "Agent-Workload-Token"
SYSTEM_PROMPT = "You answer a smoke test of the agent service. Follow the instruction exactly."
ANSWER_PROMPT = "Reply with the single word: ok"
LONG_ANSWER_PROMPT = "Count from 1 to 2000, one number per line, and write nothing else."
CANCEL_HOLD_SECONDS = 3.0
CANCEL_PROBE_SECONDS = 0.5
RUN_END_EVENTS = {"succeeded", "failed", "cancelled"}
SERVICE_HOST = "agent-service"
SERVICE_PORT = 8080
DEFAULT_TIMEOUT_SECONDS = 300.0
TEXT_EVENTS = {"assistant_text", "succeeded"}
MAX_REFUSAL_BYTES = 65536
REDACTED = "[token]"


class SmokeFailed(Exception):
    """The smoke cannot go on; the message is the one line to print, free of any token."""


class QuietParser(argparse.ArgumentParser):
    """A rejected argument is never printed: a token typed as an option must not reach a log."""

    def error(self, message: str) -> NoReturn:
        self.exit(2, "smoke: unusable arguments; the options are --timeout and --cancel\n")


@dataclass(frozen=True)
class Tokens:
    person: str
    workload: str


@dataclass(frozen=True)
class Frame:
    event: str
    data: dict


Connect = Callable[[str, int, float], http.client.HTTPConnection]


def read_tokens(stream: IO[str]) -> Tokens:
    lines = [line.strip() for line in stream.read().splitlines()]
    if len(lines) != 2 or not all(lines) or any(" " in line for line in lines):
        raise SmokeFailed("stdin must hold two lines: the person's token, then the workload's")
    return Tokens(person=lines[0], workload=lines[1])


def request_body(prompt: str) -> bytes:
    document = {
        "v": PROTOCOL_VERSION,
        "purpose": PURPOSE,
        "turn": {"system": SYSTEM_PROMPT, "messages": [{"role": "user", "text": prompt}]},
    }
    return json.dumps(document).encode("utf-8")


def frames(response: http.client.HTTPResponse) -> Iterator[Frame]:
    """The server-sent events of a run; a frame ends at its blank line."""
    fields: dict[str, str] = {}
    for raw in iter(response.readline, b""):
        line = raw.decode("utf-8").rstrip("\r\n")
        if line:
            name, _, value = line.partition(":")
            fields[name] = value.removeprefix(" ")
        elif "data" in fields:
            data = json.loads(fields["data"])
            if not isinstance(data, dict):
                raise ValueError("a frame whose data is no object")
            yield Frame(fields.get("event", ""), data)
            fields = {}


def text_of(frame: Frame, key: str) -> str:
    value = frame.data.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"a {frame.event} frame whose {key} is no text")
    return value


def refusal_line(response: http.client.HTTPResponse) -> str:
    """The service's typed refusal, or the bare status when the body is not one."""
    body = response.read(MAX_REFUSAL_BYTES)
    try:
        document = json.loads(body)
        return f"refused with {response.status} {document['code']}: {document['message']}"
    except (ValueError, KeyError, TypeError):
        return f"refused with {response.status}"


class Smoke:
    def __init__(
        self, tokens: Tokens, out: IO[str], connect: Connect, pause: Callable[[float], None]
    ) -> None:
        self.tokens = tokens
        self.out = out
        self.connect = connect
        self.pause = pause

    def say(self, line: str) -> None:
        for token in (self.tokens.person, self.tokens.workload):
            line = line.replace(token, REDACTED)
        print(line, file=self.out, flush=True)

    def run(self, timeout_seconds: float, prompt: str, cancel: bool) -> int:
        connection = self.connect(SERVICE_HOST, SERVICE_PORT, timeout_seconds)
        try:
            connection.request("POST", "/runs", request_body(prompt), self.headers())
            response = connection.getresponse()
            if response.status != http.HTTPStatus.OK:
                self.say(refusal_line(response))
                return 1
            return self.read_run(response, connection, cancel)
        except (OSError, http.client.HTTPException, ValueError) as error:
            self.say(f"the run broke off: {type(error).__name__}")
            return 1
        finally:
            connection.close()

    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.tokens.person}",
            WORKLOAD_TOKEN_HEADER: self.tokens.workload,
        }

    def read_run(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        cancel: bool,
    ) -> int:
        last = ""
        for frame in frames(response):
            last = frame.event
            self.say(f"event: {frame.event}")
            self.say_text(frame)
            if cancel and frame.event == "run_started":
                self.say(f"holding the stream open {CANCEL_HOLD_SECONDS:g} s")
                self.pause(CANCEL_HOLD_SECONDS)
                return self.close_while_running(response, connection)
        if cancel:
            self.say("the run ended before the stream could be closed")
            return 1
        return 0 if last == "succeeded" else 1

    def close_while_running(
        self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection
    ) -> int:
        """A frame already waiting after the hold that ends the run, or a stream the service
        closed, means there was nothing left to cancel."""
        connection.sock.settimeout(CANCEL_PROBE_SECONDS)
        ended = ""
        try:
            for waiting in frames(response):
                if waiting.event in RUN_END_EVENTS:
                    ended = f"sent {waiting.event}"
                    break
            else:
                ended = "closed its stream"
        except TimeoutError:
            pass
        if ended:
            self.say(f"the run ended before the close ({ended}); L4 is not proven")
            return 1
        self.say("closing the stream before the answer")
        return 0

    def say_text(self, frame: Frame) -> None:
        if frame.event in TEXT_EVENTS:
            self.say(text_of(frame, "text"))
        elif frame.event == "failed":
            self.say(f"{text_of(frame, 'code')}: {text_of(frame, 'message')}")


def main(
    argv: list[str],
    stdin: IO[str] = sys.stdin,
    out: IO[str] = sys.stdout,
    connect: Connect = http.client.HTTPConnection,
    pause: Callable[[float], None] = time.sleep,
) -> int:
    parser = QuietParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--cancel", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        tokens = read_tokens(stdin)
    except SmokeFailed as failure:
        print(f"smoke: {failure}", file=sys.stderr)
        return 1
    prompt = LONG_ANSWER_PROMPT if arguments.cancel else ANSWER_PROMPT
    return Smoke(tokens, out, connect, pause).run(arguments.timeout, prompt, arguments.cancel)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
