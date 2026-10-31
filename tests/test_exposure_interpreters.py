"""A listener run by an interpreter is named by its script or module, and the allow-list agrees."""

import subprocess

import pytest

from hal.exposure import host, inspection

WSDD_SOCKETS = """\
udp UNCONN 0 0 *:50001 *:* users:(("python3",pid=93017,fd=5)) <->
udp UNCONN 0 0 *:50002 *:* users:(("python3",pid=93017,fd=6)) <->
udp UNCONN 0 0 *:50003 *:* users:(("python3",pid=93017,fd=7)) <->
"""
TWO_SCRIPTS = """\
tcp LISTEN 0 128 0.0.0.0:8001 0.0.0.0:* users:(("python3.12",pid=501,fd=3)) <->
tcp LISTEN 0 128 0.0.0.0:8002 0.0.0.0:* users:(("python3.12",pid=502,fd=3)) <->
"""
NOT_AN_INTERPRETER = """\
tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=900,fd=3)) <->
"""
WSDD = ("python3", "/usr/bin/wsdd", "--no-host", "--discovery")


def allowing(owner: str) -> str:
    return f'[[allow]]\nprotocol = "udp"\nowner = "{owner}"\nreason = "a known listener"\n'


# (listener output, command lines by pid, allow-list, the subject of each finding)
CASES = {
    "an interpreter's sockets are named by its script": (
        WSDD_SOCKETS,
        {93017: WSDD},
        "",
        ["wsdd"] * 3,
    ),
    "a module run with -m is named by the module": (
        WSDD_SOCKETS,
        {93017: ("python3", "-u", "-m", "zeroconf.tool")},
        "",
        ["zeroconf.tool"] * 3,
    ),
    "a command line that cannot be read names the interpreter": (
        WSDD_SOCKETS,
        {},
        "",
        ["python3"] * 3,
    ),
    "inline code has no name, so the interpreter is named": (
        WSDD_SOCKETS,
        {93017: ("python3", "-c", "import socket")},
        "",
        ["python3"] * 3,
    ),
    "two scripts under one interpreter are two programs": (
        TWO_SCRIPTS,
        {501: ("python3.12", "/srv/alpha.py"), 502: ("python3.12", "beta.py")},
        "",
        ["alpha.py", "beta.py"],
    ),
    "the interpreter's name in the allow-list still permits its script": (
        WSDD_SOCKETS,
        {93017: WSDD},
        allowing("python3"),
        [],
    ),
    "the script's name in the allow-list permits its sockets": (
        WSDD_SOCKETS,
        {93017: WSDD},
        allowing("wsdd"),
        [],
    ),
}


@pytest.mark.parametrize(
    ("sockets", "command_lines", "allow_list", "subjects"), CASES.values(), ids=CASES.keys()
)
def test_the_finding_names_the_program_the_listener_runs(
    sockets, command_lines, allow_list, subjects, monkeypatch, tmp_path
):
    monkeypatch.setattr(host, "docker_inspect_json", lambda: "[]")
    monkeypatch.setattr(host, "socket_listeners", lambda: sockets)
    monkeypatch.setattr(host, "command_line", command_lines.get)
    allow_list_file = tmp_path / "allow-list.toml"
    allow_list_file.write_text(allow_list, encoding="utf-8")

    findings = inspection.inspect(allow_list_file)

    assert [finding.subject for finding in findings] == subjects


def test_a_command_line_is_read_once_per_process(monkeypatch, tmp_path):
    reads = []

    def read(pid: int) -> tuple[str, ...]:
        reads.append(pid)
        return WSDD

    monkeypatch.setattr(host, "docker_inspect_json", lambda: "[]")
    monkeypatch.setattr(host, "socket_listeners", lambda: WSDD_SOCKETS)
    monkeypatch.setattr(host, "command_line", read)
    allow_list_file = tmp_path / "allow-list.toml"
    allow_list_file.write_text("", encoding="utf-8")

    inspection.inspect(allow_list_file)

    assert reads == [93017]


def test_no_command_line_is_read_for_a_process_that_is_no_interpreter(monkeypatch, tmp_path):
    def read(pid: int) -> tuple[str, ...]:
        raise AssertionError(f"read the command line of {pid}")

    monkeypatch.setattr(host, "docker_inspect_json", lambda: "[]")
    monkeypatch.setattr(host, "socket_listeners", lambda: NOT_AN_INTERPRETER)
    monkeypatch.setattr(host, "command_line", read)
    allow_list_file = tmp_path / "allow-list.toml"
    allow_list_file.write_text("", encoding="utf-8")

    assert [finding.subject for finding in inspection.inspect(allow_list_file)] == ["sshd"]


def test_the_host_reads_the_arguments_a_running_process_was_started_with():
    with subprocess.Popen(["sleep", "30"]) as process:  # noqa: S607 - a harmless program to read the arguments of
        try:
            assert host.command_line(process.pid) == ("sleep", "30")
        finally:
            process.kill()


def test_the_host_reads_no_arguments_of_a_process_that_has_ended():
    with subprocess.Popen(["true"]) as process:  # noqa: S607 - a program that ends at once
        pass

    assert host.command_line(process.pid) is None
