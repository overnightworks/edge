"""#415 Z2, Z6: the mail's picture, one line per area, and only the names the rules allow in it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from conftest import REPORT_DISPLAY, TEST_MANIFEST, RunnerWithCleanLogs, Sandbox, home_check, message_of, restarting_containers, write_backup_status

pytestmark = pytest.mark.usefixtures("quiet_pc")


@pytest.fixture
def clean_home(sandbox: Sandbox, furnish_clean_home: Callable[[Path], Path]) -> Path:
    """The conftest's clean home, inside the sandbox's HOME so that `home setup` can install the hook there."""
    return furnish_clean_home(sandbox.home)


@pytest.fixture
def runner() -> RunnerWithCleanLogs:
    return RunnerWithCleanLogs()


def test_the_mail_says_the_action_with_one_step_and_one_line_per_area_and_no_number(clean_home, leave_pasted_image, runner, mail_stub, use_manifest) -> None:
    """#415 Z2, Z6: mail A; the tidy file, the secret and the counts stay in the report."""
    use_manifest(TEST_MANIFEST.replace('read_by = [".bashrc"]', 'read_by = ["hopin"]'))
    restarting_containers(runner, "presentator-live")
    (clean_home / ".secrets" / "gitlab").unlink()
    leave_pasted_image(clean_home)
    write_backup_status(clean_home, last_backup=local_afternoon(days_ago=0))

    home_check(clean_home, runner)

    body = message_of(mail_stub.sent[0]).get_content()
    stamp = body.split("So steht der PC (")[1].split(")")[0]
    assert body == f"""Handlungsbedarf

1. presentator-live startet ständig neu und ist darum nicht erreichbar. (neu)
   Nächster Schritt: Sag einem Agenten „presentator-live startet ständig neu, schau nach“.

So steht der PC ({stamp})

Dienste: Handlungsbedarf
Sicherung: in Ordnung, zuletzt heute 14:15
Commit-Schutz: in Ordnung
Geheimnisse: Hinweis – Zugangsdaten für hopin fehlen noch
Server-Logs: in Ordnung
Aufräumen: Hinweis – es gibt etwas aufzuräumen

Hinweise eilen nicht; home next zeigt, was davon du tun kannst.
Alles im Einzelnen: {REPORT_DISPLAY}
"""


def local_afternoon(days_ago: int) -> datetime:
    return datetime.combine(date.today() - timedelta(days=days_ago), time(14, 15)).astimezone()


def mailed_area_lines(clean_home, runner, mail_stub) -> list[str]:
    restarting_containers(runner, "presentator-live")
    home_check(clean_home, runner)
    body = message_of(mail_stub.sent[0]).get_content()
    return body.split("\n\n")[3].splitlines()


@pytest.mark.parametrize(
    ("read_by", "line"),
    [
        ('["hopin"]', "Geheimnisse: Hinweis – Zugangsdaten für hopin fehlen noch"),
        ("[]", "Geheimnisse: Hinweis – Zugangsdaten fehlen noch"),
        ('[".bashrc", "git/gmail-cleanup", "SONAR_TOKEN in x"]', "Geheimnisse: Hinweis – Zugangsdaten fehlen noch"),
    ],
    ids=["names the app that reads it", "names nobody when the manifest has no reader", "names no file, path or secret"],
)
def test_a_missing_secret_names_the_app_that_reads_it_and_never_the_secret(clean_home, runner, mail_stub, use_manifest, read_by, line) -> None:
    """#415 Z2, Z6: the area line says whose credentials are missing from the manifest's read_by."""
    use_manifest(TEST_MANIFEST.replace('read_by = [".bashrc"]', f"read_by = {read_by}"))
    (clean_home / ".secrets" / "gitlab").unlink()

    assert line in mailed_area_lines(clean_home, runner, mail_stub)
    assert not {".bashrc", "gmail-cleanup", "SONAR_TOKEN", "gitlab"} & set(message_of(mail_stub.sent[0]).get_content().replace("/", " ").split())


def test_the_logs_note_reads_as_in_the_picture(clean_home, runner, mail_stub) -> None:
    """#415 Z2: the area line is the clause of the picture, lower case after the dash and without a final period."""
    runner.container_listing = "/songmaker-api-1\tjson-file\tsongmaker\n"

    assert "Server-Logs: Hinweis – noch nicht überall nach 30 Tagen gelöscht" in mailed_area_lines(clean_home, runner, mail_stub)


@pytest.mark.parametrize(("days_ago", "said"), [(0, "heute"), (1, "gestern")], ids=["today", "yesterday"])
def test_the_backup_line_says_when_the_last_backup_succeeded(clean_home, runner, mail_stub, days_ago, said) -> None:
    """#415 Z2: a healthy backup area still shows the last success, read from the status the backup check reads."""
    write_backup_status(clean_home, last_backup=local_afternoon(days_ago))

    assert f"Sicherung: in Ordnung, zuletzt {said} 14:15" in mailed_area_lines(clean_home, runner, mail_stub)
