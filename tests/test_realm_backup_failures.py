"""What the realm step says when `hal backup` fails or answers something it cannot read."""

import subprocess

import pytest

from edge.realm import reconcile


def _answering(stdout: str, stderr: str, code: int):
    def run(command: list[str], _timeout: int, _stdin: str | None = None):
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    return run


@pytest.mark.parametrize(
    ("stderr", "cause"),
    [
        (
            "error: hal backup: fehlgeschlagen: kein Passwort\nnext: hal backup --help\n",
            "hal backup: fehlgeschlagen: kein Passwort",
        ),
        (
            "error: --pre-apply needs the name of an app\nnext: hal backup --help\n",
            "--pre-apply needs the name of an app",
        ),
        ("Traceback ... boom\n", "Traceback ... boom"),
    ],
    ids=["a runtime failure", "a usage failure", "a failure that is not hal's own"],
)
def test_a_failing_backup_names_the_cause_never_the_next_line(stderr: str, cause: str) -> None:
    backup = reconcile.Backup(("hal",), _answering("", stderr, 3))

    with pytest.raises(reconcile.RealmError) as raised:
        backup.pre_apply()

    assert str(raised.value) == f"the Keycloak backup failed ({cause})"


@pytest.mark.parametrize("stdout", ["{}\n", "[]\n", '{"snapshot": null}', "not json"])
def test_an_answer_without_a_snapshot_id_is_a_failure(stdout: str) -> None:
    backup = reconcile.Backup(("hal",), _answering(stdout, "", 0))

    with pytest.raises(reconcile.RealmError, match="named no pre-apply snapshot"):
        backup.pre_apply()
