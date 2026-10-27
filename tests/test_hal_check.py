"""`hal check` driven through `hal.cli.main`, with a fake `docker` and `ss` first on PATH."""

import errno
import io
import json
import os
import shlex
import signal
import sys
from pathlib import Path

import pytest
from git_support import hal_program, run_hal

from edge.external import cloudflare_read, host_probe, tofu
from edge_check import inspection
from edge_check.exposure import Finding, Problem
from hal import cli

CLEAN_LINE = "exposure: nothing reaches beyond loopback\n"
SSHD_LISTENS_ON_ALL = 'tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=900,fd=3)) <->\n'
FORBIDDEN_STDIN = """
class Forbidden:
    def __getattr__(self, name):
        raise AssertionError("stdin was touched")
sys.stdin = Forbidden()
"""


@pytest.fixture
def fake_host(monkeypatch, tmp_path):
    """A `docker` without containers and an `ss` printing the listeners, first on PATH; `docker`
    can run another script."""
    directory = tmp_path / "bin"
    directory.mkdir()

    def install(listeners: str = "", docker_script: str = "exit 0") -> None:
        scripts = {"docker": docker_script, "ss": f"printf %s {shlex.quote(listeners)}"}
        for name, script in scripts.items():
            executable = directory / name
            executable.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
            executable.chmod(0o755)
        monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")

    return install


@pytest.fixture
def run_hal_check(fake_host, tmp_path, capsys):
    def run(*arguments: str, ss: str = "") -> tuple[int, str, str]:
        fake_host(ss)
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
@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_a_bare_check_and_the_exposure_area_print_the_same(run_hal_check, ss, flags):
    """E1, cells 2 and 6."""
    bare = run_hal_check(*flags, ss=ss)

    assert bare == run_hal_check("exposure", *flags, ss=ss)
    assert bare[0] == (1 if ss else 0)


def test_a_clean_host_says_so_and_exits_0(run_hal_check):
    """E2, cell 1."""
    assert run_hal_check("exposure", "--allow-list", "ALLOW_LIST") == (0, CLEAN_LINE, "")


def test_a_finding_prints_the_table_on_stdout_and_exits_1(run_hal_check):
    """E2, cell 3."""
    exit_code, out, err = run_hal_check(
        "exposure", "--allow-list", "ALLOW_LIST", ss=SSHD_LISTENS_ON_ALL
    )

    assert (exit_code, err) == (1, "")
    assert out.splitlines()[1].split() == [
        "listener-beyond-loopback", "sshd", "tcp", "0.0.0.0", "22",
    ]  # fmt: skip


def test_json_is_one_object_keyed_by_the_area(run_hal_check):
    """E3, cell 6."""
    exit_code, out, err = run_hal_check(
        "exposure", "--allow-list", "ALLOW_LIST", "--json", ss=SSHD_LISTENS_ON_ALL
    )

    assert (exit_code, err) == (1, "")
    assert list(json.loads(out)) == ["exposure"]
    assert [finding["subject"] for finding in json.loads(out)["exposure"]] == ["sshd"]


def test_json_of_a_clean_host_is_an_empty_list(run_hal_check):
    """E3, cell 6."""
    exit_code, out, _ = run_hal_check("exposure", "--allow-list", "ALLOW_LIST", "--json")

    assert (exit_code, json.loads(out)) == (0, {"exposure": []})


def test_a_check_that_cannot_run_exits_3_with_the_cause_and_empty_stdout(run_hal_check, tmp_path):
    """E2 and E3, cells 4 and 6."""
    for arguments in (("exposure", "--json"), ("exposure",)):
        exit_code, out, err = run_hal_check(
            *arguments, "--allow-list", str(tmp_path / "absent.toml")
        )

        assert (exit_code, out) == (3, "")
        assert err.splitlines()[0].startswith(
            "error: hal check: exposure: cannot check, cannot read"
        )


def test_a_bare_check_with_an_allow_list_is_a_usage_error(run_hal_check):
    """E4, cell 5."""
    exit_code, out, err = run_hal_check("--allow-list", "ALLOW_LIST")

    assert (exit_code, out) == (2, "")
    assert err.splitlines()[-1] == "next: hal check exposure --allow-list FILE"


def test_an_area_that_does_not_exist_is_a_usage_error(run_hal_check):
    """E1, cell 5."""
    exit_code, out, _ = run_hal_check("services")

    assert (exit_code, out) == (2, "")


def test_an_interrupt_exits_130_with_empty_stdout(fake_host, tmp_path, capsys):
    """E2, cell 7: the fake `docker` sends SIGINT to the process that runs it."""
    fake_host(docker_script="kill -INT $PPID\nexec sleep 30")
    allow_list = tmp_path / "allow-list.toml"
    allow_list.write_text("", encoding="utf-8")

    exit_code = cli.main(["check", "exposure", "--allow-list", str(allow_list)])

    assert (exit_code, capsys.readouterr().out) == (130, "")


def test_the_check_never_reads_stdin(fake_host, monkeypatch, tmp_path):
    """Cell 8: a real `hal` process whose stdin raises on any touch, as G-11 runs the commands."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    fake_host()

    result = run_hal(tmp_path, hal_program(before=FORBIDDEN_STDIN), "check")

    assert (result.returncode, result.stdout) == (0, CLEAN_LINE)


@pytest.mark.parametrize("problem", list(Problem))
def test_every_kind_of_finding_shows_in_the_table_and_in_json(problem):
    """E3, done when: every finding kind shows in text and JSON."""
    finding = Finding(problem, "subject")

    assert problem.value in inspection.render_table([finding])
    assert inspection.as_json_objects([finding])[0]["problem"] == problem.value


SENTINEL = "sentinel-4b1d-never-print-me"
ZONE_TARGET = "t-1.cfargotunnel.com"
DECLARED_HOSTS = """\
cloudflare: {zone: example.test}
services:
  site:
    hosts:
      - {name: "@", exposure: public, reason: the site}
"""


def snapshot_with(*records: cloudflare_read.Record) -> cloudflare_read.Snapshot:
    return cloudflare_read.Snapshot("t-1", records, (), ())


CLEAN_SNAPSHOT = snapshot_with(cloudflare_read.Record("example.test", "CNAME", ZONE_TARGET, True))
FINDING_SNAPSHOT = snapshot_with(
    cloudflare_read.Record("example.test", "A", SENTINEL, True),
    cloudflare_read.Record("stray.example.test", "CNAME", ZONE_TARGET, True),
)


class FakeCloudflare:
    """Stands in for `CloudflareClient`: answers a snapshot, raises, or runs a callable first."""

    def __init__(self, outcome) -> None:
        self.outcome = outcome

    def __call__(self, _token: str) -> "FakeCloudflare":
        return self

    def read(self, _zone: str):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            self.outcome()
            return CLEAN_SNAPSHOT
        return self.outcome


class AnsweringProber:
    def __init__(self, answer=None) -> None:
        self.answer = answer or host_probe.Answer(200, None, True, False)

    def __call__(self) -> "AnsweringProber":
        return self

    def ask(self, _name: str, _path: str = "/"):
        return self.answer


@pytest.fixture
def hosts_home(tmp_path, monkeypatch) -> Path:
    """A temporary HOME whose read-token file holds the sentinel, and a one-host catalog."""
    home = tmp_path / "home"
    token_file = home / ".secrets/cloudflare/fleet-read"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(SENTINEL + "\n")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "fleet.yaml").write_text(DECLARED_HOSTS)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(tofu, "PROJECT_ROOT", tree)
    return home


@pytest.fixture
def run_hosts(hosts_home, monkeypatch, capsys):
    def run(*arguments: str, reads=CLEAN_SNAPSHOT, prober=None) -> tuple[int, str, str]:
        monkeypatch.setattr(cloudflare_read, "CloudflareClient", FakeCloudflare(reads))
        monkeypatch.setattr(host_probe, "HttpProber", prober or AnsweringProber())
        try:
            exit_code = cli.main(["check", "hosts", *arguments])
        except SystemExit as usage_error:
            exit_code = usage_error.code
        captured = capsys.readouterr()
        return exit_code, captured.out, captured.err

    return run


def test_clean_hosts_say_how_many_and_exit_0(run_hosts):
    assert run_hosts() == (0, "no findings (1 hosts)\n", "")


def test_clean_hosts_in_json_are_an_empty_list_keyed_by_the_area(run_hosts):
    exit_code, out, err = run_hosts("--json")

    assert (exit_code, json.loads(out), err) == (0, {"hosts": []}, "")


def test_findings_exit_1_and_never_print_the_token_in_text(run_hosts):
    exit_code, out, err = run_hosts(reads=FINDING_SNAPSHOT)

    assert (exit_code, err) == (1, "")
    assert out.splitlines()[0].startswith("record-wrong example.test: ")
    assert SENTINEL not in out


def test_findings_in_json_are_one_object_keyed_by_the_area_without_the_token(run_hosts):
    exit_code, out, err = run_hosts("--json", reads=FINDING_SNAPSHOT)

    assert (exit_code, err) == (1, "")
    assert [item["problem"] for item in json.loads(out)["hosts"]] == [
        "record-wrong",
        "undeclared-record",
    ]
    assert SENTINEL not in out


@pytest.mark.parametrize(
    "outcome, problem",
    [
        (host_probe.Unreachable(host_probe.FailureKind.TIMEOUT), "unreachable"),
        (host_probe.Answer(200, None, False, False), "answer-wrong"),
    ],
)
def test_a_host_that_answers_wrong_or_not_at_all_is_a_finding_in_text_and_json(
    run_hosts, outcome, problem
):
    text = run_hosts(prober=AnsweringProber(outcome))
    as_json = run_hosts("--json", prober=AnsweringProber(outcome))

    assert text[0] == as_json[0] == 1
    assert text[1].startswith(f"{problem} example.test: ")
    assert json.loads(as_json[1])["hosts"][0]["problem"] == problem


@pytest.mark.parametrize(
    "failure, cause",
    [
        (cloudflare_read.CloudflareReadError(f"/zones: {SENTINEL}"), "/zones: ***"),
        (OSError(errno.EIO, SENTINEL), "OSError: "),
        (ValueError(SENTINEL), "ValueError"),
    ],
    ids=["read error", "os error", "value error"],
)
@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_a_reader_failure_exits_3_names_the_cause_without_the_token_and_prints_no_stdout(
    run_hosts, failure, cause, flags
):
    exit_code, out, err = run_hosts(*flags, reads=failure)

    assert (exit_code, out) == (3, "")
    assert err.splitlines()[0].startswith(f"error: hal check: hosts: cannot check, {cause}")
    assert len(err.splitlines()) == 1
    assert SENTINEL not in err


def test_a_missing_token_file_exits_3_naming_it(run_hosts, hosts_home):
    token_file = hosts_home / ".secrets/cloudflare/fleet-read"
    token_file.unlink()

    exit_code, out, err = run_hosts("--json")

    assert (exit_code, out) == (3, "")
    assert err.splitlines()[0] == (
        f"error: hal check: hosts: cannot check, cannot read {token_file}: "
        "No such file or directory"
    )


def test_an_invalid_catalog_exits_3_before_any_token_is_read(run_hosts, hosts_home):
    (tofu.PROJECT_ROOT / "fleet.yaml").write_text("services: 3\n")
    (hosts_home / ".secrets/cloudflare/fleet-read").unlink()

    exit_code, out, err = run_hosts()

    assert (exit_code, out) == (3, "")
    assert err.startswith("error: hal check: hosts: cannot check, ")
    assert "fleet-read" not in err


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_an_interrupt_before_the_output_exits_130_with_stdout_empty(run_hosts, interrupt, flags):
    reads = lambda: os.kill(os.getpid(), interrupt)  # noqa: E731

    exit_code, out, err = run_hosts(*flags, reads=reads)

    assert (exit_code, out) == (130, "")
    assert SENTINEL not in err


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_an_interrupt_while_the_output_is_written_leaves_the_whole_document_and_the_verdict(
    hosts_home, monkeypatch, interrupt
):
    class InterruptedHalfway(io.StringIO):
        delivered = False

        def write(self, text: str) -> int:
            if self.delivered or len(text) < 2:
                return super().write(text)
            self.delivered = True
            middle = len(text) // 2
            super().write(text[:middle])
            os.kill(os.getpid(), interrupt)
            return middle + super().write(text[middle:])

    stdout = InterruptedHalfway()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(cloudflare_read, "CloudflareClient", FakeCloudflare(FINDING_SNAPSHOT))
    monkeypatch.setattr(host_probe, "HttpProber", AnsweringProber())

    exit_code = cli.main(["check", "hosts", "--json"])

    assert exit_code == 1
    assert len(json.loads(stdout.getvalue())["hosts"]) == 2


def test_the_allow_list_belongs_to_the_exposure_area(run_hosts):
    exit_code, out, err = run_hosts("--allow-list", "ALLOW_LIST")

    assert (exit_code, out) == (2, "")
    assert err.splitlines()[-1] == "next: hal check exposure --allow-list FILE"


def test_the_exposure_area_reads_no_cloudflare(fake_host, hosts_home, monkeypatch, capsys):
    allow_list = hosts_home / "allow-list.toml"
    allow_list.write_text("", encoding="utf-8")

    def forbidden(*_arguments, **_keywords):
        raise AssertionError("Cloudflare was read")

    monkeypatch.setattr(cloudflare_read, "CloudflareClient", forbidden)
    fake_host()

    assert cli.main(["check", "exposure", "--allow-list", str(allow_list)]) == 0
    assert capsys.readouterr().err == ""
