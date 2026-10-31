"""A listener run by an interpreter is named by its script or module, and the allow-list agrees."""

import json
import os
import subprocess
import sys

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
WSDD = ("python3", "/usr/bin/wsdd", "--no-host", "--discovery")
PID = 4242
USER_CGROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/app.scope"


def one_socket(process: str, protocol: str = "udp", port: int = 41000) -> str:
    """One socket of a process in the user's own cgroup, as ss shows it to that user."""
    state = "LISTEN" if protocol == "tcp" else "UNCONN"
    owner = f'users:(("{process}",pid={PID},fd=3)) cgroup:{USER_CGROUP}'
    return f"{protocol} {state} 0 0 0.0.0.0:{port} 0.0.0.0:* {owner} <->\n"


def allowing(owner: str) -> str:
    return f'[[allow]]\nprotocol = "udp"\nowner = "{owner}"\nreason = "a known listener"\n'


def publishing(port: int) -> str:
    """`docker inspect` of one running container that publishes a TCP port on every address."""
    return json.dumps(
        [
            {
                "Name": "/web",
                "State": {"Running": True},
                "Config": {"ExposedPorts": {}, "Labels": None},
                "HostConfig": {
                    "PublishAllPorts": False,
                    "PortBindings": {f"{port}/tcp": [{"HostIp": "", "HostPort": str(port)}]},
                },
            }
        ]
    )


@pytest.fixture
def subjects_on(monkeypatch, tmp_path):
    """The subject of each finding on a host with these sockets, command lines and allow-list."""

    def inspect(sockets, read_command_line, allow_list="", docker="[]") -> list[str]:
        monkeypatch.setattr(host, "docker_inspect_json", lambda: docker)
        monkeypatch.setattr(host, "socket_listeners", lambda: sockets)
        monkeypatch.setattr(host, "command_line", read_command_line)
        allow_list_file = tmp_path / "allow-list.toml"
        allow_list_file.write_text(allow_list, encoding="utf-8")
        return [finding.subject for finding in inspection.inspect(allow_list_file)]

    return inspect


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
    **{
        f"{' '.join(command_line[:2])} is inline code, so the interpreter is named": (
            one_socket(command_line[0]),
            {PID: command_line},
            "",
            [command_line[0]],
        )
        for command_line in (
            ("perl", "-E", "say 1"),
            ("node", "-p", "1+1"),
            ("node", "--eval", "require('dgram')"),
            ("node", "--print", "1+1"),
        )
    },
    "an option's value before the script is not the script": (
        one_socket("node"),
        {PID: ("node", "--require", "./hook.js", "server.js")},
        "",
        ["server.js"],
    ),
    "a warning filter before the script is not the script": (
        one_socket("python3"),
        {PID: ("python3", "-W", "ignore", "app.py")},
        "",
        ["app.py"],
    ),
    "a process that is no interpreter is named by its process": (
        one_socket("sshd", "tcp", 22),
        {PID: ("sshd", "/usr/bin/other")},
        "",
        ["sshd"],
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
    "an entry naming the script does not permit it, and the finding names the script": (
        WSDD_SOCKETS,
        {93017: WSDD},
        allowing("wsdd"),
        ["wsdd"] * 3,
    ),
    "a script named like a system unit is not permitted by that unit's entry": (
        one_socket("python3"),
        {PID: ("python3", "/home/u/avahi-daemon.service")},
        allowing("avahi-daemon.service"),
        ["avahi-daemon.service"],
    ),
    "a process named like a system unit is not permitted by that unit's entry": (
        one_socket("docker.service"),
        {},
        allowing("docker.service"),
        ["docker.service"],
    ),
}


@pytest.mark.parametrize(
    ("sockets", "command_lines", "allow_list", "subjects"), CASES.values(), ids=CASES.keys()
)
def test_the_finding_names_the_program_the_listener_runs(
    sockets, command_lines, allow_list, subjects, subjects_on
):
    assert subjects_on(sockets, command_lines.get, allow_list) == subjects


@pytest.mark.parametrize(
    ("process", "command_lines"),
    [("python3", {PID: ("python3", "/home/u/docker.service")}), ("docker.service", {})],
    ids=["a script", "a process"],
)
def test_a_listener_named_like_dockers_unit_on_a_published_port_is_reported(
    process, command_lines, subjects_on
):
    sockets = one_socket(process, "tcp", 8080)

    assert "docker.service" in subjects_on(sockets, command_lines.get, docker=publishing(8080))


def test_a_script_named_like_a_shipped_process_entry_is_reported_under_its_name(subjects_on):
    sockets = one_socket("python3", "udp", 5353)
    script = {PID: ("python3", "/home/u/chrome")}
    shipped = inspection.DEFAULT_ALLOW_LIST.read_text(encoding="utf-8")

    assert subjects_on(sockets, script.get, shipped) == ["chrome"]


def test_a_command_line_is_read_once_per_process(subjects_on):
    reads = []

    def read(pid: int) -> tuple[str, ...]:
        reads.append(pid)
        return WSDD

    subjects_on(WSDD_SOCKETS, read)

    assert reads == [93017]


def test_the_host_reads_the_arguments_a_running_process_was_started_with():
    assert host.command_line(os.getpid()) == tuple(sys.orig_argv)


def test_the_host_reads_no_arguments_of_a_process_that_has_ended():
    with subprocess.Popen(["true"]) as process:  # noqa: S607 - a program that ends at once
        pass

    assert host.command_line(process.pid) is None
