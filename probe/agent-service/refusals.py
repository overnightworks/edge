"""The refusals that come before any run (cell U17 of unit.py `--cell smoke`): a v2 body, a caller
outside the app's group and a caller with no subscription group. Each is asked of the running
service from the `agent-smoke` container with person tokens that carry exactly the groups the case
names; the login is never read."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import NamedTuple

from egress import LABEL, expect
from smoke import PROTOCOL_VERSION, Tokens, request_body

SMOKE_SERVICE = "agent-smoke"
APP_GROUP = "/apps/songmaker"
PLAN_GROUP = "/services/agent-service/subscription"
API_GROUP = "/services/agent-service/api"
MEMBER_GROUPS = (APP_GROUP, PLAN_GROUP)
ASKER_PROGRAM = """\
import http.client, json, sys
asked = json.load(sys.stdin)
for case in asked["cases"]:
    connection = http.client.HTTPConnection("agent-service", 8080, timeout=30)
    connection.request("POST", "/runs", case["body"], {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + case["person"],
        "Agent-Workload-Token": asked["workload"],
    })
    answer = connection.getresponse()
    print(answer.status, json.loads(answer.read()).get("code"))
    connection.close()
"""


class Refusal(NamedTuple):
    name: str
    groups: tuple[str, ...]
    wire_version: int
    answer: str


REFUSALS = (
    Refusal("a v2 body", MEMBER_GROUPS, 2, "400 unsupported_version"),
    Refusal("a caller outside the app", (PLAN_GROUP,), PROTOCOL_VERSION, "403 forbidden"),
    Refusal(
        "a caller of the api group only",
        (APP_GROUP, API_GROUP),
        PROTOCOL_VERSION,
        "403 not_enabled",
    ),
)


def body_of_version(version: int) -> str:
    return json.dumps({**json.loads(request_body("ok")), "v": version})


def check_refusals(
    run: Callable[..., object],
    compose_command: Callable[..., list[str]],
    tokens_for: Callable[[tuple[str, ...]], Tokens],
    held: list[str],
) -> None:
    asked = json.dumps({
        "workload": tokens_for(MEMBER_GROUPS).workload,
        "cases": [
            {
                "person": tokens_for(refusal.groups).person,
                "body": body_of_version(refusal.wire_version),
            }
            for refusal in REFUSALS
        ],
    })  # fmt: skip
    answers = run(
        compose_command(
            "run", "--rm", "-T", "--no-deps", "--label", LABEL, "--entrypoint", "python",
            SMOKE_SERVICE, "-I", "-B", "-c", ASKER_PROGRAM,
        ),
        stdin=asked, check=False, timeout=120,
    )  # fmt: skip
    expected = [refusal.answer for refusal in REFUSALS]
    expect(answers.stdout.splitlines() == expected, f"U17: the answers were {answers.stdout!r}")
    held.append("U17")
    names = [refusal.name for refusal in REFUSALS]
    print(f"U17 held: {names} answered {expected}", flush=True)  # noqa: T201 - the cell verdict line of this probe
