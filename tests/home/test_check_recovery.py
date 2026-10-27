"""Tests of the Done lines of the `hal check` mail (#415 Z4): what the mail claims as done."""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from conftest import REPORT_DISPLAY

from home_tools import check


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ({"backup\tbackup-failed\tsongmaker"}, {"backup\tbackup-failed\tpresentator"}),
        ({"hook\thook-file\t/x/pre-commit", "hook\tquality\t/x/quality"}, {"hook\tquality\t/x/quality"}),
        ({"platform\tcontainer-restarting\tpresentator-live"}, {"platform\tdocker-unreadable\tdocker"}),
    ],
    ids=["same rule, other subject", "same sentence, other rule", "state unreadable"],
)
def test_an_action_is_not_mailed_as_done_while_something_current_contradicts_it(previous, current) -> None:
    """#415 Z2, Z4: the mail never claims what a current action or an unreadable state denies."""
    body = check.mail_body(frozenset(current), frozenset(previous), REPORT_DISPLAY, datetime(2026, 10, 26, 14, 54), check.MailFacts())

    assert "Done" not in body


@pytest.mark.parametrize(
    ("previous", "current", "done"),
    [
        (
            {"platform\tcontainer-restarting\tpresentator-live", "platform\tcontainer-restarting\tsongmaker-live"},
            {"platform\tcontainer-restarting\tsongmaker-live"},
            "- presentator-live no longer keeps restarting.",
        ),
        (
            {"backup\tbackup-stale\tsongmaker", "backup\tbackup-stale\tpresentator"},
            {"backup\tbackup-stale\tpresentator"},
            "- songmaker was backed up again.",
        ),
        (
            {"hook\thook-file\t/x/pre-commit", "hook\tquality\t/x/quality"},
            set(),
            "Done\n\n- The protection that checks every commit for secrets is working again.\n\n",
        ),
        (
            {"secrets\tloose-mode\tGMAIL", "secrets\tloose-mode\tGITHUB"},
            {"secrets\tloose-mode\tGITHUB"},
            "- A secret is no longer readable by others.",
        ),
        (
            {"secrets\tverification-failed\tGMAIL", "secrets\tverification-failed\tGITHUB"},
            {"secrets\tverification-failed\tGITHUB"},
            "- The check of a login reports no error any more.",
        ),
        (
            {"secrets\tmissing-password\tPASSWORD"},
            {"secrets\tpresence-unknown\tPASSWORD"},
            "- The backup password is no longer reported missing.",
        ),
    ],
    ids=["one of two containers recovers", "one of two backups recovers", "two hook rules give one line", "one of two lenient secrets recovers", "one of two failed verifications recovers", "missing password turns unobservable"],
)
def test_the_recovery_of_a_subject_is_mailed_while_another_subject_stays_broken(previous, current, done) -> None:
    """#415 Z4: a mail that comes because an action went says what went, once, and claims no presence it did not see."""
    body = check.mail_body(frozenset(current), frozenset(previous), REPORT_DISPLAY, datetime(2026, 10, 26, 14, 54), check.MailFacts())

    assert done in body
    assert body.count(check.HOOK_BROKEN.resolved) <= 1
    assert not re.search(r"\b(is|are) (present|back)\b|present again", body)


def test_an_action_that_turns_into_a_note_is_mailed_as_done_in_words_the_note_does_not_deny() -> None:
    """#415 Z4: a mail that comes because an action went says what went."""
    body = check.mail_body(
        frozenset({"secrets\tverification-unreachable\tGMAIL"}), frozenset({"secrets\tverification-failed\tGMAIL"}), REPORT_DISPLAY, datetime(2026, 10, 26, 14, 54), check.MailFacts()
    )

    assert "Done\n\n- The check of a login reports no error any more." in body
