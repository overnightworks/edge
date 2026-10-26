"""Tests of the Erledigt lines of the `home check` mail (#415 Z4): what the mail claims as done."""

from __future__ import annotations

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

    assert "Erledigt" not in body


@pytest.mark.parametrize(
    ("previous", "current", "erledigt"),
    [
        (
            {"platform\tcontainer-restarting\tpresentator-live", "platform\tcontainer-restarting\tsongmaker-live"},
            {"platform\tcontainer-restarting\tsongmaker-live"},
            "- presentator-live startet nicht mehr ständig neu.",
        ),
        (
            {"backup\tbackup-stale\tsongmaker", "backup\tbackup-stale\tpresentator"},
            {"backup\tbackup-stale\tpresentator"},
            "- songmaker wurde wieder gesichert.",
        ),
        (
            {"hook\thook-file\t/x/pre-commit", "hook\tquality\t/x/quality"},
            set(),
            "Erledigt\n\n- Der Schutz, der jeden Commit auf Geheimnisse prüft, greift wieder.\n\n",
        ),
        (
            {"secrets\tloose-mode\tGMAIL", "secrets\tloose-mode\tGITHUB"},
            {"secrets\tloose-mode\tGITHUB"},
            "- Ein Geheimnis ist nicht mehr für andere lesbar.",
        ),
        (
            {"secrets\tverification-failed\tGMAIL", "secrets\tverification-failed\tGITHUB"},
            {"secrets\tverification-failed\tGITHUB"},
            "- Die Prüfung einer Anmeldung meldet keinen Fehler mehr.",
        ),
        (
            {"secrets\tmissing-password\tPASSWORD"},
            {"secrets\tpresence-unknown\tPASSWORD"},
            "- Das Passwort der Sicherung wird nicht mehr als fehlend gemeldet.",
        ),
    ],
    ids=["one of two containers recovers", "one of two backups recovers", "two hook rules give one line", "one of two lenient secrets recovers", "one of two failed verifications recovers", "missing password turns unobservable"],
)
def test_the_recovery_of_a_subject_is_mailed_while_another_subject_stays_broken(previous, current, erledigt) -> None:
    """#415 Z4: a mail that comes because an action went says what went, once, and claims no presence it did not see."""
    body = check.mail_body(frozenset(current), frozenset(previous), REPORT_DISPLAY, datetime(2026, 10, 26, 14, 54), check.MailFacts())

    assert erledigt in body
    assert body.count("greift wieder") <= 1
    assert "ist wieder da" not in body


def test_an_action_that_turns_into_a_note_is_mailed_as_done_in_words_the_note_does_not_deny() -> None:
    """#415 Z4: a mail that comes because an action went says what went."""
    body = check.mail_body(
        frozenset({"secrets\tverification-unreachable\tGMAIL"}), frozenset({"secrets\tverification-failed\tGMAIL"}), REPORT_DISPLAY, datetime(2026, 10, 26, 14, 54), check.MailFacts()
    )

    assert "Erledigt\n\n- Die Prüfung einer Anmeldung meldet keinen Fehler mehr." in body
