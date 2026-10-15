from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FakeProbe

from home_tools import cli
from home_tools.tidy_home import Layout


def home_check(home: Path, runner, probe: FakeProbe | None = None) -> int:
    return cli.main(["check"], home=home, run=runner, probe=probe or FakeProbe())


def test_a_first_run_with_findings_writes_one_report_and_sends_one_notification(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home)

    code = home_check(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    assert code == 1
    assert report.startswith("# home check ")
    assert "| pasted-images |" in report
    assert len(runner.notifications) == 1
    assert runner.notifications[0] == report.splitlines()[0].removeprefix("# ")
    assert report.splitlines()[0].endswith("| 2 Funde")  # the image is also unknown to ~/README.md
    assert runner.notification_bodies[0] == f"2 neu, 0 weg. Bericht: {Layout(clean_home).check_report_file}"


def test_unchanged_findings_never_notify_again_even_as_they_age(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home, age_days=30)
    home_check(clean_home, runner)
    leave_pasted_image(clean_home, age_days=40)

    code = home_check(clean_home, runner)

    assert code == 1
    assert len(runner.notifications) == 1


def test_an_unchanged_finding_about_a_secret_name_that_is_not_utf8_notifies_once(clean_home, runner) -> None:
    secrets_dir = os.fsencode(Layout(clean_home).secrets_dir)
    os.close(os.open(secrets_dir + b"/bad\xffname", os.O_CREAT | os.O_WRONLY, 0o600))
    assert home_check(clean_home, runner) == 1

    code = home_check(clean_home, runner)

    assert code == 1
    assert len(runner.notifications) == 1


def test_a_changed_set_of_findings_notifies_again(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home)
    home_check(clean_home, runner)
    leave_pasted_image(clean_home / "git")

    home_check(clean_home, runner)

    assert len(runner.notifications) == 2
    assert runner.notification_bodies[1].startswith("2 neu, 0 weg.")  # the image, and git/ litter


def test_a_finding_that_goes_while_another_stays_notifies_it_as_gone(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home)
    second = leave_pasted_image(clean_home / "git")
    home_check(clean_home, runner)
    second.unlink()

    home_check(clean_home, runner)

    assert runner.notification_bodies[1].startswith("0 neu, 2 weg.")


def test_a_clean_home_exits_0_without_notifying(clean_home, runner) -> None:
    code = home_check(clean_home, runner)

    assert code == 0
    assert runner.notifications == []
    assert "Nichts zu tun." in Layout(clean_home).check_report_file.read_text()


def test_findings_that_return_after_a_clean_run_notify_again(clean_home, leave_pasted_image, runner) -> None:
    image = leave_pasted_image(clean_home)
    home_check(clean_home, runner)
    image.unlink()
    home_check(clean_home, runner)
    leave_pasted_image(clean_home)

    home_check(clean_home, runner)

    assert len(runner.notifications) == 2


def test_switched_off_notifications_stay_silent(clean_home, leave_pasted_image, runner) -> None:
    config_file = Layout(clean_home).config_file
    config_file.parent.mkdir(parents=True)
    config_file.write_text("[notify]\nenabled = false\n")
    leave_pasted_image(clean_home)

    code = home_check(clean_home, runner)

    assert code == 1
    assert runner.notifications == []


def test_a_failed_notification_is_reported_and_sent_again_on_the_next_run(clean_home, leave_pasted_image, runner, capsys) -> None:
    leave_pasted_image(clean_home)
    runner.notify_delivers = False

    failed_code = home_check(clean_home, runner)
    runner.notify_delivers = True
    home_check(clean_home, runner)

    assert failed_code == 1
    assert "notify-send" in capsys.readouterr().err
    assert len(runner.notifications) == 2


def test_secrets_findings_join_the_one_report_and_the_one_notification(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home / "git")
    (clean_home / ".secrets" / "gitlab").unlink()

    code = home_check(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    assert code == 1
    assert report.splitlines()[0].endswith("| 3 Funde")
    assert "# home tidy " in report
    assert "# home secrets " in report
    assert "| missing | gitlab |" in report
    assert runner.notification_bodies == [f"3 neu, 0 weg. Bericht: {Layout(clean_home).check_report_file}"]


def break_tidy(home: Path) -> Path:
    (home / "git").mkdir(exist_ok=True)
    (home / "git" / "stray.txt").write_text("litter")
    (home / ".secrets" / "gitlab").unlink()  # a secrets finding that must stay visible
    return home / "git"


def break_secrets(home: Path) -> Path:
    shutil.copy(home / "README.md", home / "Eingefügtes Bild.png")
    os.utime(home / "Eingefügtes Bild.png", (0, 0))  # a tidy finding that must stay visible
    return home / ".secrets" / "cloudflared"


def home_check_with_an_unreadable_secret(home: Path, runner) -> None:
    locked = home / ".secrets" / "cloudflared"
    locked.chmod(0)
    try:
        home_check(home, runner)
    finally:
        locked.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
@pytest.mark.parametrize(
    ("break_one", "broken", "still_visible"),
    [(break_tidy, "tidy", "| missing | gitlab |"), (break_secrets, "secrets", "| pasted-images |")],
    ids=["tidy breaks", "secrets breaks"],
)
def test_one_failing_checker_leaves_the_others_findings_in_the_report_and_exits_3(
    clean_home, runner, capsys, break_one: Callable[[Path], Path], broken: str, still_visible: str
) -> None:
    locked = break_one(clean_home)
    locked.chmod(0)
    try:
        code = home_check(clean_home, runner)
    finally:
        locked.chmod(0o700)

    report = Layout(clean_home).check_report_file.read_text()
    error = capsys.readouterr().err
    assert code == 3
    assert f"| fehlgeschlagen: {broken}" in report.splitlines()[0]
    assert f"Prüfung {broken} fehlgeschlagen (PermissionError)" in report
    assert still_visible in report
    assert error.startswith("home: unerwarteter Fehler (CheckFailed): ")
    assert error.count("\n") == 1
    assert len(runner.notifications) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_broken_checker_counts_its_unobserved_findings_as_neither_new_nor_gone(clean_home, runner) -> None:
    locked = break_tidy(clean_home)
    home_check(clean_home, runner)
    locked.chmod(0)
    try:
        broken_codes = [home_check(clean_home, runner) for _ in range(2)]
    finally:
        locked.chmod(0o700)
    home_check(clean_home, runner)

    unchanged = f"0 neu, 0 weg. Bericht: {Layout(clean_home).check_report_file}"
    assert broken_codes == [3, 3]
    assert runner.notifications[1].endswith("| 1 Fund | fehlgeschlagen: tidy")
    assert runner.notification_bodies[0] != unchanged
    assert runner.notification_bodies[1:] == [unchanged, unchanged]  # one for the break, one for the recovery


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_broken_checker_that_recovers_into_a_clean_home_notifies_the_recovery(clean_home, runner) -> None:
    home_check_with_an_unreadable_secret(clean_home, runner)

    code = home_check(clean_home, runner)

    assert code == 0
    assert len(runner.notifications) == 2
    assert runner.notifications[1].endswith("| 0 Funde")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_recovery_into_a_clean_home_whose_notification_fails_is_sent_again(clean_home, runner) -> None:
    home_check_with_an_unreadable_secret(clean_home, runner)
    runner.notify_delivers = False
    home_check(clean_home, runner)
    runner.notify_delivers = True

    home_check(clean_home, runner)

    assert len(runner.notifications) == 3
    assert runner.notifications[2].endswith("| 0 Funde")


def test_an_invalid_manifest_leaves_the_tidy_findings_in_the_report_and_exits_2(
    clean_home, leave_pasted_image, runner, use_manifest, capsys
) -> None:
    leave_pasted_image(clean_home)
    manifest = use_manifest('[[secret]]\nname = "e"\n')

    code = home_check(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    error = capsys.readouterr().err
    assert code == 2
    assert "| fehlgeschlagen: secrets" in report.splitlines()[0]
    assert "Prüfung secrets fehlgeschlagen (ManifestError)" in report
    assert "| pasted-images |" in report
    assert error.startswith(f"home: {manifest}: ")
    assert error.count("\n") == 1


def memory_text(text: str) -> Callable[[Path], None]:
    return lambda path: path.write_text(text)


@pytest.mark.parametrize(
    ("plant_memory", "refusal"),
    [
        (memory_text('["tidy\\t'), "JSONDecodeError"),
        (memory_text('["tidy\\tleaked-secret"]'), "ValueError"),
        (memory_text('{"tidy": 1}'), "ValueError"),
        (os.mkfifo, "ValueError"),
    ],
    ids=["cut off", "not a finding identity", "not a list", "a fifo that never gets a writer"],
)
def test_a_corrupt_findings_memory_still_leaves_the_report_written(
    clean_home, runner, capsys, plant_memory: Callable[[Path], None], refusal: str
) -> None:
    layout = Layout(clean_home)
    layout.state_dir.mkdir(parents=True)
    plant_memory(layout.check_findings_file)

    code = home_check(clean_home, runner)

    assert code == 3
    assert capsys.readouterr().err.startswith(f"home: unerwarteter Fehler ({refusal})")
    assert layout.check_report_file.read_text().startswith("# home check ")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_symlinked_findings_memory_is_refused_and_never_carried_into_the_state(clean_home, runner, tmp_path) -> None:
    layout = Layout(clean_home)
    layout.state_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text('["tidy\\tpasted-images\\tleaked-secret"]')
    layout.check_findings_file.symlink_to(outside)
    locked = break_tidy(clean_home)
    locked.chmod(0)
    try:
        code = home_check(clean_home, runner)
    finally:
        locked.chmod(0o700)

    state_files = [path for path in layout.state_dir.iterdir() if path.is_file() and not path.is_symlink()]
    assert code == 3
    assert state_files
    assert not any("leaked-secret" in path.read_text() for path in state_files)
