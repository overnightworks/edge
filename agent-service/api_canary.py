"""The API route's canary: one run of a person on the API route, and only verdict lines on stdout.

    python -B /opt/canary/api_canary.py --marker TEXT

Stdin holds the person's token and then the workload's, as smoke.py reads them. It prints
`answer <status>`, one `event <name>` per frame, `code <code>` for a failed run and
`marker yes|no`: whether the text of the answer holds TEXT. It never prints a message, a token or
a run id. Exit 0 when the run succeeded and the answer held the marker.
"""

import argparse
import http.client
import re
import sys

from smoke import (
    ANSWER_PROMPT,
    SERVICE_HOST,
    SERVICE_PORT,
    WORKLOAD_TOKEN_HEADER,
    frames,
    read_tokens,
    request_body,
    text_of,
)

TIMEOUT_SECONDS = 120
CLOSED_WORD = re.compile(r"[a-z_]{1,40}")


def closed(word: object) -> str:
    return word if isinstance(word, str) and CLOSED_WORD.fullmatch(word) else "other"


def main(argv: list[str], stdin=sys.stdin, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker", required=True)
    marker = parser.parse_args(argv).marker
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
            if frame.event == "failed":
                print(f"code {closed(frame.data.get('code'))}", file=out, flush=True)
            if frame.event in {"assistant_text", "succeeded"}:
                seen = seen or marker in text_of(frame, "text")
    print(f"marker {'yes' if seen else 'no'}", file=out, flush=True)
    return 0 if ended == "succeeded" and seen else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
