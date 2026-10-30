"""`hal alarm`: the five-minute run over the services and the backup.

It runs the same two checks as the daily `hal check`, counts how many runs in a row each finding
has appeared in, and counts a finding only from the configured number of runs on. What it counts
joins the daily check's remembered set, so the mail, its words and the channels are the daily
check's, and neither run mails the other's news again. A finding that is gone leaves the set in the
run that no longer sees it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hal.output import write_line
from hal.output.command import Verb, command
from hal.workstation import check
from hal.workstation.jobs import DEFAULT_ALARM_INTERVAL
from hal.workstation.layout import Layout
from hal.workstation.report import read_state_file, write_state_file

ALARM_AREAS = frozenset({"platform", "backup"})
STREAKS_FILE_NAME = "alarm-streaks.json"


@dataclass(frozen=True)
class AlarmSettings:
    interval: int = DEFAULT_ALARM_INTERVAL
    """Minutes between two runs."""
    runs: int = 2
    """Runs in a row that a finding must appear in before it counts."""


def _streaks_file(layout: Layout) -> Path:
    return layout.hal_state_dir / STREAKS_FILE_NAME


def _read_streaks(layout: Layout) -> dict[str, int]:
    text = read_state_file(layout.home, _streaks_file(layout))
    if text is None:
        return {}
    streaks = json.loads(text)
    if not isinstance(streaks, dict) or not all(isinstance(identity, str) and type(count) is int for identity, count in streaks.items()):
        raise ValueError("alarm streaks invalid: expected identities with run counts")
    return streaks


def _counted(
    observed: frozenset[str], streaks: dict[str, int], previous: frozenset[str], runs: int
) -> frozenset[str]:
    """What the run counts: a finding seen in `runs` runs in a row, or one already remembered, which must not wait for its count again."""
    return frozenset(identity for identity in observed if streaks[identity] >= runs or identity in previous)


def alarm(ctx: check.CheckContext, settings: AlarmSettings) -> None:
    layout = ctx.layout
    earlier_streaks = _read_streaks(layout)
    previous = check.previous_identities(layout)
    results = check.run_areas(ctx, ALARM_AREAS)
    observed = check.remembered_identities(results, previous)
    remembered = previous or frozenset()
    streaks = {identity: min(earlier_streaks.get(identity, 0) + 1, settings.runs) for identity in observed}
    write_state_file(layout.home, _streaks_file(layout), json.dumps(streaks, sort_keys=True) + "\n")
    elsewhere = frozenset(identity for identity in remembered if identity.partition("\t")[0] not in ALARM_AREAS)
    current = _counted(observed, streaks, remembered, settings.runs) | elsewhere
    outcome = check.deliver_news(ctx, current, previous)
    # Unsettled news stays unremembered, so the next run delivers it again.
    if outcome.settled:
        check.remember(layout, current)
    write_line(f"{command(Verb.ALARM)}: {outcome.delivery}")
