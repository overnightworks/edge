"""`hal check` driven through `hal.cli.main`, with fixture command outputs in place of the host."""

import io
import json
import sys

import pytest

from edge_check import host, inspection
from edge_check.exposure import Finding, Problem
from hal import cli

CLEAN_LINE = "exposure: nothing reaches beyond loopback\n"
SSHD_LISTENS_ON_ALL = 'tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=900,fd=3)) <->\n'


@pytest.fixture
def run_hal_check(monkeypatch, tmp_path, capsys):
    def run(*arguments: str, ss: str = "") -> tuple[int, str, str]:
        monkeypatch.setattr(host, "docker_inspect_json", lambda: "[]")
        monkeypatch.setattr(host, "socket_listeners", lambda: ss)
        allow_list = tmp_path / "allow-list.toml"
        allow_list.write_text("", encoding="utf-8")
        arguments = tuple(str(allow_list) if item == "ALLOW_LIST" else item for item in arguments)
        try:
            exit_code = cli.main(["check", *arguments])
        except SystemExit as usage_error:
            exit_code = usage_error.code
        captured = capsys.readouterr()
        return exit_code, captured.out, captured.err

    return run


@pytest.mark.parametrize("ss", ["", SSHD_LISTENS_ON_ALL], ids=["clean", "finding"])
def test_a_bare_check_and_the_exposure_area_print_the_same(run_hal_check, ss):
    bare = run_hal_check(ss=ss)

    assert bare == run_hal_check("exposure", ss=ss)
    assert bare[0] == (1 if ss else 0)


def test_a_clean_host_says_so_and_exits_0(run_hal_check):
    assert run_hal_check("exposure", "--allow-list", "ALLOW_LIST") == (0, CLEAN_LINE, "")


def test_a_finding_prints_the_table_on_stdout_and_exits_1(run_hal_check):
    exit_code, out, err = run_hal_check(
        "exposure", "--allow-list", "ALLOW_LIST", ss=SSHD_LISTENS_ON_ALL
    )

    assert (exit_code, err) == (1, "")
    assert out.splitlines()[1].split() == [
        "listener-beyond-loopback", "sshd", "tcp", "0.0.0.0", "22",
    ]  # fmt: skip


def test_json_is_one_object_keyed_by_the_area(run_hal_check):
    exit_code, out, err = run_hal_check(
        "exposure", "--allow-list", "ALLOW_LIST", "--json", ss=SSHD_LISTENS_ON_ALL
    )

    assert (exit_code, err) == (1, "")
    assert list(json.loads(out)) == ["exposure"]
    assert [finding["subject"] for finding in json.loads(out)["exposure"]] == ["sshd"]


def test_json_of_a_clean_host_is_an_empty_list(run_hal_check):
    exit_code, out, _ = run_hal_check("exposure", "--allow-list", "ALLOW_LIST", "--json")

    assert (exit_code, json.loads(out)) == (0, {"exposure": []})


def test_a_check_that_cannot_run_exits_3_with_the_cause_and_empty_stdout(run_hal_check, tmp_path):
    for arguments in (("exposure", "--json"), ("exposure",)):
        exit_code, out, err = run_hal_check(
            *arguments, "--allow-list", str(tmp_path / "absent.toml")
        )

        assert (exit_code, out) == (3, "")
        assert err.splitlines()[0].startswith(
            "error: hal check: exposure: cannot check, cannot read"
        )


def test_a_bare_check_with_an_allow_list_is_a_usage_error(run_hal_check):
    exit_code, out, err = run_hal_check("--allow-list", "ALLOW_LIST")

    assert (exit_code, out) == (2, "")
    assert err.splitlines()[-1] == "next: hal check exposure --allow-list FILE"


def test_an_area_that_does_not_exist_is_a_usage_error(run_hal_check):
    exit_code, out, _ = run_hal_check("hosts")

    assert (exit_code, out) == (2, "")


def test_an_interrupt_exits_130_with_empty_stdout(monkeypatch, run_hal_check):
    def interrupted() -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(host, "docker_inspect_json", interrupted)
    exit_code = cli.main(["check", "exposure"])

    assert exit_code == 130


def test_the_check_never_reads_stdin(monkeypatch, run_hal_check):
    class ForbiddenStdin(io.StringIO):
        def read(self, *_):
            raise AssertionError("hal check read stdin")

        readline = read

    monkeypatch.setattr(sys, "stdin", ForbiddenStdin())

    assert run_hal_check("exposure", "--allow-list", "ALLOW_LIST")[0] == 0


@pytest.mark.parametrize("problem", list(Problem))
def test_every_kind_of_finding_shows_in_the_table_and_in_json(problem):
    finding = Finding(problem, "subject")

    assert problem.value in inspection.render_table([finding])
    assert inspection.as_json_objects([finding])[0]["problem"] == problem.value
