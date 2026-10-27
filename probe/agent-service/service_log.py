"""The service-log vocabulary and its classification: which records the agent service's log
holds, as the containment probe's B5 cell counts them."""

from __future__ import annotations

import re
from typing import NamedTuple

REFUSED_ANNOUNCEMENT = "A CLI run's start announcement was refused ("
LOGIN_REFUSED = "CliLoginRefusedError"
RELAY_NOT_CONNECTED = f"{REFUSED_ANNOUNCEMENT}RELAY_NOT_CONNECTED)"
RELAY_OWNER_FAILED = re.compile(
    r"The owner of the relay of run \S+ failed: CapabilityReflected at "
)
RUNNER_OF_RUN = r"The runner of run (\S+) failed: "
RUNNER_FAILED = re.compile(RUNNER_OF_RUN + "CapabilityReflected at ")
RUNNER_LOGIN_REFUSED = re.compile(RUNNER_OF_RUN + f"{LOGIN_REFUSED} at ")
LOG_FORMAT = "%(levelname)s %(name)s %(message)s"
ROOT_RECORD = re.compile(r"(WARNING|ERROR|CRITICAL) (\S+) (.*)")
UVICORN_RECORD = re.compile(r"(INFO|WARNING|ERROR|CRITICAL):\s")

# The service logs through `logging.lastResort`, which prints the bare message; the same
# WARNING-and-above records, with their level and logger, are what B5 classifies.
BOOTSTRAP = f"""\
import logging, sys
from agent_runtime_service import cli
handler = logging.StreamHandler()
handler.setLevel(logging.WARNING)
handler.setFormatter(logging.Formatter({LOG_FORMAT!r}))
logging.getLogger().addHandler(handler)
sys.exit(cli.main(sys.argv[1:]))
"""


class LogClasses(NamedTuple):
    """How many records of each class the service log holds, and the runs whose runner failed on
    the refused login. `escaped` counts a bare line that continues no record."""

    login_refused: int
    other_errors: int
    refused: int
    agent_warnings: int
    uvicorn_warnings: int
    other_warnings: int
    escaped: int
    run_ids: frozenset[str]

    def since(self, earlier: LogClasses) -> LogClasses:
        counts = (now - before for now, before in zip(self[:-1], earlier[:-1], strict=True))
        return LogClasses(*counts, self.run_ids - earlier.run_ids)


def classify_log(text: str) -> LogClasses:
    """Records are a prefixed line and the lines that continue it; INFO lines are ignored."""
    errors = refused = login = agent = uvicorn = other = escaped = 0
    run_ids: set[str] = set()
    in_record = False
    for line in text.splitlines():
        root, uvicorn_line = ROOT_RECORD.match(line), UVICORN_RECORD.match(line)
        if root:
            level, logger, message = root.groups()
            if level == "WARNING" and message.startswith(REFUSED_ANNOUNCEMENT):
                refused += 1
            elif level == "WARNING":
                agent += logger.startswith("agent_runtime")
                uvicorn += logger.startswith("uvicorn")
                other += not logger.startswith(("agent_runtime", "uvicorn"))
            elif found := RUNNER_LOGIN_REFUSED.match(message):
                login += 1
                run_ids.add(found[1])
            else:
                errors += 1
        elif uvicorn_line:
            level = uvicorn_line[1]
            errors += level in ("ERROR", "CRITICAL")
            uvicorn += level == "WARNING"
        elif not in_record:
            escaped += 1
        in_record = in_record or bool(root or uvicorn_line)
    return LogClasses(login, errors, refused, agent, uvicorn, other, escaped, frozenset(run_ids))
