"""The API route's canary: one run of a person on the API route, and only verdict lines on stdout.

    python -B /opt/canary/api_canary.py --marker TEXT [--cancel]

Stdin holds the person's token and then the workload's, as smoke.py reads them. It prints
`answer <status>`, one `event <name>` per frame, `route <word>` for the route `run_started` names,
`code <code>` for a failed run and `marker yes|no`: whether the text of the answer holds TEXT. It
never prints a message, a token or a run id. Exit 0 when the run succeeded and the answer held the
marker.

With `--cancel` it holds the stream open for smoke.py's hold, then closes it, reads the run's record
(`GET /runs/{id}`) until it has ended and prints `status <state>`: a client that closes its stream
sees no terminal frame. Exit 0 when that state is `cancelled`.
"""

import argparse
import http.client
import json
import re
import sys
import time

from smoke import (
    ANSWER_PROMPT,
    CANCEL_HOLD_SECONDS,
    SERVICE_HOST,
    SERVICE_PORT,
    WORKLOAD_TOKEN_HEADER,
    frames,
    read_tokens,
    request_body,
    text_of,
)

TIMEOUT_SECONDS = 120
STATUS_POLLS = 60
STATUS_POLL_SECONDS = 0.5
CANCELLED = "cancelled"
RUN_ID = re.compile(r"[0-9a-f-]{36}")
CLOSED_WORD = re.compile(r"[a-z_]{1,40}")


def closed(word: object) -> str:
    return word if isinstance(word, str) and CLOSED_WORD.fullmatch(word) else "other"


def run_status(headers: dict[str, str], run_id: str) -> str:
    """The state of the run's record once it has ended, `running` if it has not within the polls."""
    state = "running"
    for _ in range(STATUS_POLLS):
        connection = http.client.HTTPConnection(SERVICE_HOST, SERVICE_PORT, timeout=TIMEOUT_SECONDS)
        connection.request("GET", f"/runs/{run_id}", None, headers)
        response = connection.getresponse()
        if response.status != http.HTTPStatus.OK:
            return "other"
        record = json.loads(response.read())
        state = closed(record.get("state"))
        if record.get("ended_at") is not None:
            break
        time.sleep(STATUS_POLL_SECONDS)
    return state


def cancel_after_start(
    connection: http.client.HTTPConnection, headers: dict[str, str], run_id: object, out
) -> int:
    time.sleep(CANCEL_HOLD_SECONDS)
    connection.close()
    state = (
        run_status(headers, run_id)
        if isinstance(run_id, str) and RUN_ID.fullmatch(run_id)
        else "other"
    )
    print(f"status {state}", file=out, flush=True)
    return 0 if state == CANCELLED else 1


def main(argv: list[str], stdin=sys.stdin, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker", required=True)
    parser.add_argument("--cancel", action="store_true")
    arguments = parser.parse_args(argv)
    tokens = read_tokens(stdin)
    connection = http.client.HTTPConnection(SERVICE_HOST, SERVICE_PORT, timeout=TIMEOUT_SECONDS)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {tokens.person}",
        WORKLOAD_TOKEN_HEADER: tokens.workload,
    }
    connection.request("POST", "/runs", request_body(ANSWER_PROMPT), headers)
    response = connection.getresponse()
    print(f"answer {response.status}", file=out, flush=True)
    seen, ended = False, ""
    if response.status == http.HTTPStatus.OK:
        for frame in frames(response):
            ended = frame.event
            print(f"event {closed(frame.event)}", file=out, flush=True)
            if frame.event == "run_started":
                print(f"route {closed(frame.data.get('route'))}", file=out, flush=True)
                if arguments.cancel:
                    return cancel_after_start(connection, headers, frame.data.get("run_id"), out)
            if frame.event == "failed":
                print(f"code {closed(frame.data.get('code'))}", file=out, flush=True)
            if frame.event in {"assistant_text", "succeeded"}:
                seen = seen or arguments.marker in text_of(frame, "text")
    print(f"marker {'yes' if seen else 'no'}", file=out, flush=True)
    return 0 if ended == "succeeded" and seen else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
