"""edge-check driven through its CLI, with fixture command outputs in place of the host."""

import json
import os
from dataclasses import dataclass, field

import pytest

from edge_check import cli, host

ALLOW_AVAHI = """
[[allow]]
protocol = "udp"
port = 5353
reason = "mDNS: avahi answers service discovery on the local network"
"""

ALLOW_CLOUDFLARED = """
[[allow]]
protocol = "udp"
owner = "cloudflared.service"
reason = "the tunnel's QUIC client sockets use random ports"
"""

ALLOW_AVAHI_UNIT = """
[[allow]]
protocol = "udp"
owner = "avahi-daemon.service"
reason = "mDNS: avahi answers local-network service discovery"
"""

SS_LOOPBACK_ONLY = """\
udp UNCONN 0 0 127.0.0.53%lo:53    0.0.0.0:* cgroup:/system.slice/systemd-resolved.service <->
tcp LISTEN 0 4096 127.0.0.1:8081  0.0.0.0:* cgroup:/system.slice/docker.service <->
tcp LISTEN 0 511 [::1]:6379     [::]:* users:(("redis-server",pid=4242,fd=7)) <->
"""
SS_AVAHI = """\
udp UNCONN 0 0 0.0.0.0:5353  0.0.0.0:* uid:108 cgroup:/system.slice/avahi-daemon.service <->
"""
SS_AVAHI_SEEN_BY_ROOT = """\
udp UNCONN 0 0 0.0.0.0:5353  0.0.0.0:* users:(("avahi-daemon",pid=812,fd=12)) uid:108 \
cgroup:/system.slice/avahi-daemon.service <->
"""
SS_USER_SCOPE_NAMED_AVAHI = """\
udp UNCONN 0 0 0.0.0.0:5353  0.0.0.0:* uid:1001 \
cgroup:/user.slice/user-1001.slice/user@1001.service/app.slice/avahi-daemon.service <->
"""
SS_CLOUDFLARED = """\
udp UNCONN 0 0 *:33697       *:* uid:62260 cgroup:/system.slice/cloudflared.service v6only:0 <->
"""
SS_SSHD = """\
tcp LISTEN 0 128 0.0.0.0:22    0.0.0.0:* users:(("sshd",pid=900,fd=3)) <->
"""
SS_DHCP6_LINK_LOCAL = """\
udp UNCONN 0 0 [fe80::d8e8:44d:1d8e:31f0]%wlp7s0:546      [::]:* ino:18742 sk:f <->
"""
SS_DOCKER_PROXY_3000 = """\
tcp LISTEN 0 4096 0.0.0.0:3000  0.0.0.0:* uid:0 cgroup:/system.slice/docker.service <->
"""
SS_DOCKER_PROXY_8080_BOTH_FAMILIES = """\
tcp LISTEN 0 4096 0.0.0.0:8080  0.0.0.0:* uid:0 cgroup:/system.slice/docker.service <->
tcp LISTEN 0 4096    [::]:8080     [::]:* uid:0 cgroup:/system.slice/docker.service <->
"""
SS_CHROME_MDNS_TWICE = """\
udp UNCONN 0 0 224.0.0.251:5353  0.0.0.0:* users:(("chrome",pid=16393,fd=49)) <->
udp UNCONN 0 0 224.0.0.251:5353  0.0.0.0:* users:(("chrome",pid=16393,fd=48)) <->
"""


def container(
    name: str,
    port_bindings: dict[str, list[tuple[str, str]]] | None = None,
    *,
    exposed: tuple[str, ...] = (),
    publish_all: bool = False,
    labels: dict[str, str] | None = None,
    running: bool = True,
) -> dict:
    """One `docker inspect` entry; port_bindings maps "80/tcp" to [(host_ip, host_port)]."""
    return {
        "Name": f"/{name}",
        "State": {"Running": running},
        "Config": {"ExposedPorts": {port: {} for port in exposed}, "Labels": labels},
        "HostConfig": {
            "PublishAllPorts": publish_all,
            "PortBindings": {
                container_port: [{"HostIp": ip, "HostPort": port} for ip, port in bindings]
                for container_port, bindings in (port_bindings or {}).items()
            },
        },
    }


def inspect_json(*containers: dict) -> str:
    return json.dumps(list(containers))


def finding(
    problem: str,
    subject: str,
    protocol: str | None = None,
    address: str = "",
    port: int | str | None = None,
    *,
    edge_name: str | None = None,
) -> dict:
    return {
        "problem": problem,
        "subject": subject,
        "protocol": protocol,
        "address": address,
        "port": port,
        "edge_name": edge_name,
    }


ROUTED = {"traefik.enable": "true"}


@dataclass(frozen=True)
class Host:
    docker: str = "[]"
    ss: str = ""
    allow_list: str = ""
    findings: list[dict] = field(default_factory=list)


@pytest.fixture
def run_edge_check(monkeypatch, tmp_path, capsys):
    def run(state: Host, *arguments: str) -> tuple[int, str, str]:
        monkeypatch.setattr(host, "docker_inspect_json", lambda: state.docker)
        monkeypatch.setattr(host, "socket_listeners", lambda: state.ss)
        allow_list = tmp_path / "edge-check.toml"
        allow_list.write_text(state.allow_list, encoding="utf-8")
        exit_code = cli.main(["--allow-list", str(allow_list), *arguments])
        captured = capsys.readouterr()
        return exit_code, captured.out, captured.err

    return run


HOSTS = {
    "nothing runs": Host(),
    "publications on 127.0.0.1 and ::1, listeners on loopback": Host(
        docker=inspect_json(
            container("traefik", {"8081/tcp": [("127.0.0.1", "8081")]}),
            container("dns", {"53/udp": [("::1", "5353")]}),
            container("worker", {}, exposed=("9000/tcp",)),
        ),
        ss=SS_LOOPBACK_ONLY,
    ),
    "allow-listed mDNS port": Host(ss=SS_AVAHI, allow_list=ALLOW_AVAHI),
    "the project's allow-list permits the tunnel's QUIC and Chrome's mDNS sockets": Host(
        ss=SS_CLOUDFLARED + SS_CHROME_MDNS_TWICE, allow_list=cli.DEFAULT_ALLOW_LIST.read_text()
    ),
    "allow-listed owner on a random port": Host(ss=SS_CLOUDFLARED, allow_list=ALLOW_CLOUDFLARED),
    "publication on 0.0.0.0": Host(
        docker=inspect_json(container("grafana", {"3000/tcp": [("0.0.0.0", "3000")]})),
        findings=[finding("publication-beyond-loopback", "grafana", "tcp", "0.0.0.0", 3000)],
    ),
    "publication on ::": Host(
        docker=inspect_json(container("dns", {"53/udp": [("::", "53")]})),
        findings=[finding("publication-beyond-loopback", "dns", "udp", "::", 53)],
    ),
    "publication on a loopback port range": Host(
        docker=inspect_json(container("ranged", {"80/tcp": [("127.0.0.1", "8000-8010")]})),
    ),
    "publication on a 0.0.0.0 port range names the range": Host(
        docker=inspect_json(container("ranged", {"80/tcp": [("0.0.0.0", "8000-8010")]})),
        findings=[finding("publication-beyond-loopback", "ranged", "tcp", "0.0.0.0", "8000-8010")],
    ),
    "publish-all publishes every exposed port not bound explicitly": Host(
        docker=inspect_json(
            container(
                "lab",
                {"9000/tcp": [("127.0.0.1", "9000")]},
                exposed=("9000/tcp", "53/udp"),
                publish_all=True,
            )
        ),
        findings=[finding("publication-without-host-ip", "lab", "udp", "", None)],
    ),
    "publication without host IP or host port": Host(
        docker=inspect_json(container("hello", {"80/tcp": [("", "")]})),
        findings=[finding("publication-without-host-ip", "hello", "tcp", "", None)],
    ),
    "allow-listed system unit, run as root so ss shows the process": Host(
        ss=SS_AVAHI_SEEN_BY_ROOT, allow_list=ALLOW_AVAHI_UNIT
    ),
    "allow-listed process name, run as root": Host(
        ss=SS_AVAHI_SEEN_BY_ROOT, allow_list=ALLOW_AVAHI_UNIT.replace(".service", "")
    ),
    "a user unit named like an allow-listed system unit is not permitted": Host(
        ss=SS_USER_SCOPE_NAMED_AVAHI,
        allow_list=ALLOW_AVAHI_UNIT,
        findings=[
            finding("listener-beyond-loopback", "avahi-daemon.service", "udp", "0.0.0.0", 5353)
        ],
    ),
    "tcp listener on 0.0.0.0 names its process": Host(
        ss=SS_LOOPBACK_ONLY + SS_SSHD,
        findings=[finding("listener-beyond-loopback", "sshd", "tcp", "0.0.0.0", 22)],
    ),
    "udp listener on * names its systemd unit": Host(
        ss=SS_CLOUDFLARED,
        findings=[finding("listener-beyond-loopback", "cloudflared.service", "udp", "*", 33697)],
    ),
    "link-local listener with unknown owner": Host(
        ss=SS_DHCP6_LINK_LOCAL,
        findings=[
            finding(
                "listener-beyond-loopback",
                "unknown process",
                "udp",
                "fe80::d8e8:44d:1d8e:31f0",
                546,
            )
        ],
    ),
    "allow-list entry for another protocol does not permit": Host(
        ss=SS_AVAHI,
        allow_list=ALLOW_AVAHI.replace('"udp"', '"tcp"'),
        findings=[
            finding("listener-beyond-loopback", "avahi-daemon.service", "udp", "0.0.0.0", 5353)
        ],
    ),
    "allow-list entry for another owner does not permit": Host(
        ss=SS_SSHD,
        allow_list=ALLOW_CLOUDFLARED.replace('"udp"', '"tcp"'),
        findings=[finding("listener-beyond-loopback", "sshd", "tcp", "0.0.0.0", 22)],
    ),
    "a 0.0.0.0 publication and its docker-proxy listener are one finding": Host(
        docker=inspect_json(container("grafana", {"3000/tcp": [("0.0.0.0", "3000")]})),
        ss=SS_DOCKER_PROXY_3000,
        findings=[finding("publication-beyond-loopback", "grafana", "tcp", "0.0.0.0", 3000)],
    ),
    "a publication without host IP and docker-proxy on both families are one finding": Host(
        docker=inspect_json(container("hello", {"80/tcp": [("", "8080")]})),
        ss=SS_DOCKER_PROXY_8080_BOTH_FAMILIES,
        findings=[finding("publication-without-host-ip", "hello", "tcp", "", 8080)],
    ),
    "another process on a published port is its own finding": Host(
        docker=inspect_json(container("grafana", {"3000/tcp": [("0.0.0.0", "3000")]})),
        ss=SS_SSHD.replace(":22 ", ":3000 "),
        findings=[
            finding("listener-beyond-loopback", "sshd", "tcp", "0.0.0.0", 3000),
            finding("publication-beyond-loopback", "grafana", "tcp", "0.0.0.0", 3000),
        ],
    ),
    "a stopped container's publication does not hide a listener on its port": Host(
        docker=inspect_json(
            container("grafana", {"3000/tcp": [("0.0.0.0", "3000")]}, running=False)
        ),
        ss=SS_DOCKER_PROXY_3000,
        findings=[
            finding("listener-beyond-loopback", "docker.service", "tcp", "0.0.0.0", 3000),
            finding("publication-beyond-loopback", "grafana", "tcp", "0.0.0.0", 3000),
        ],
    ),
    "routed container with a valid, unique edge.name": Host(
        docker=inspect_json(container("blog", labels=ROUTED | {"edge.name": "blog-2"})),
    ),
    "routed container without edge.name": Host(
        docker=inspect_json(container("blog", labels=ROUTED)),
        findings=[finding("routed-without-edge-name", "blog")],
    ),
    "edge.name outside [a-z0-9-]{1,63}": Host(
        docker=inspect_json(container("blog", labels=ROUTED | {"edge.name": "Blog.App"})),
        findings=[finding("invalid-edge-name", "blog", edge_name="Blog.App")],
    ),
    "edge.name longer than 63 characters": Host(
        docker=inspect_json(container("blog", labels={"edge.name": "b" * 64})),
        findings=[finding("invalid-edge-name", "blog", edge_name="b" * 64)],
    ),
    "two running containers with the same edge.name": Host(
        docker=inspect_json(
            container("blog-old", labels=ROUTED | {"edge.name": "blog"}),
            container("blog-new", labels=ROUTED | {"edge.name": "blog"}),
        ),
        findings=[finding("duplicate-edge-name", "blog-new, blog-old", edge_name="blog")],
    ),
    "a stopped container sharing an edge.name is no duplicate": Host(
        docker=inspect_json(
            container("blog-old", labels=ROUTED | {"edge.name": "blog"}, running=False),
            container("blog-new", labels=ROUTED | {"edge.name": "blog"}),
        ),
    ),
    "repeated sockets and mixed findings come once, in a stable order": Host(
        docker=inspect_json(
            container("web", {"8080/tcp": [("0.0.0.0", "8080"), ("::", "8080")]}),
            container("api", {"9000/tcp": [("", "9000")]}),
        ),
        ss=SS_CHROME_MDNS_TWICE + SS_SSHD,
        findings=[
            finding("listener-beyond-loopback", "chrome", "udp", "224.0.0.251", 5353),
            finding("listener-beyond-loopback", "sshd", "tcp", "0.0.0.0", 22),
            finding("publication-beyond-loopback", "web", "tcp", "0.0.0.0", 8080),
            finding("publication-beyond-loopback", "web", "tcp", "::", 8080),
            finding("publication-without-host-ip", "api", "tcp", "", 9000),
        ],
    ),
}


@pytest.mark.parametrize("state", HOSTS.values(), ids=HOSTS.keys())
def test_exits_1_exactly_when_something_reaches_beyond_loopback(run_edge_check, state):
    exit_code, out, err = run_edge_check(state, "--json")

    assert (exit_code, json.loads(out), err) == (1 if state.findings else 0, state.findings, "")


def test_human_table_names_subject_address_and_port(run_edge_check):
    exit_code, out, _ = run_edge_check(HOSTS["publication without host IP or host port"])

    assert exit_code == 1
    assert out.splitlines()[1].split() == [
        "publication-without-host-ip",
        "hello",
        "tcp",
        "(none)",
        "(random)",
    ]


def test_human_table_names_the_edge_name_a_finding_is_about(run_edge_check):
    exit_code, out, _ = run_edge_check(HOSTS["edge.name outside [a-z0-9-]{1,63}"])

    assert exit_code == 1
    assert out.splitlines()[1].split() == ["invalid-edge-name", "blog", "-", "-", "-", "Blog.App"]


def test_human_output_says_so_when_nothing_is_exposed(run_edge_check):
    exit_code, out, _ = run_edge_check(HOSTS["allow-listed mDNS port"])

    assert (exit_code, out) == (0, "edge-check: nothing reaches beyond loopback\n")


VALID_ENTRY = 'protocol = "udp"\nport = 5353\nreason = "mDNS"\n'
INVALID_ALLOW_LISTS = {
    "unknown key": (VALID_ENTRY + 'adress = "0.0.0.0"', "allow entry 2 (udp 5353): unknown key"),
    "port 0": ('protocol = "udp"\nport = 0\nreason = "x"', "allow entry 2 (udp 0): bad port 0"),
    "port above 65535": (
        'protocol = "tcp"\nport = 70000\nreason = "x"',
        "allow entry 2 (tcp 70000): bad port 70000",
    ),
    "port as string": (
        'protocol = "tcp"\nport = "22"\nreason = "x"',
        "allow entry 2 (tcp 22): bad port '22'",
    ),
    "missing reason": ('protocol = "udp"\nport = 546', "allow entry 2 (udp 546): missing reason"),
    "blank reason": (
        'protocol = "udp"\nport = 546\nreason = " "',
        "allow entry 2 (udp 546): missing reason",
    ),
    "unknown protocol": (
        'protocol = "icmp"\nport = 1\nreason = "x"',
        "allow entry 2 (icmp 1): protocol must be tcp or udp",
    ),
    "empty owner": (
        'protocol = "tcp"\nowner = ""\nreason = "x"',
        "allow entry 2 (tcp): owner must be a non-empty string",
    ),
    "neither port nor owner": (
        'protocol = "udp"\nreason = "x"',
        "allow entry 2 (udp): needs a port, an owner, or both",
    ),
}


@pytest.mark.parametrize(
    ("second_entry", "message"), INVALID_ALLOW_LISTS.values(), ids=INVALID_ALLOW_LISTS.keys()
)
def test_exits_2_naming_the_invalid_allow_list_entry(run_edge_check, second_entry, message):
    allow_list = f"[[allow]]\n{VALID_ENTRY}\n[[allow]]\n{second_entry}\n"

    exit_code, out, err = run_edge_check(Host(ss=SS_SSHD, allow_list=allow_list), "--json")

    assert (exit_code, out) == (2, "")
    assert message in err


@pytest.mark.parametrize(
    ("allow_list", "message"),
    [
        ("[[allow]\n", "not valid TOML"),
        ("allowed = []\n", "unknown top-level key ['allowed']"),
    ],
    ids=["broken TOML", "unknown top-level key"],
)
def test_exits_2_on_an_unreadable_allow_list(run_edge_check, allow_list, message):
    exit_code, _, err = run_edge_check(Host(allow_list=allow_list))

    assert exit_code == 2
    assert message in err


UNREADABLE_OUTPUTS = {
    "docker output is not JSON": (Host(docker="Error: no such object"), "docker inspect"),
    "docker output is JSON but not a list": (Host(docker="null"), "not a list of containers"),
    "container without HostConfig": (
        Host(docker=json.dumps([{"Name": "/broken"}])),
        "container 'broken'",
    ),
    "host port that is not a number": (
        Host(docker=inspect_json(container("web", {"80/tcp": [("127.0.0.1", "http")]}))),
        "container 'web'",
    ),
    "host IP that is not an address": (
        Host(docker=inspect_json(container("web", {"80/tcp": [("localhost", "80")]}))),
        "container 'web'",
    ),
    "unknown ss socket type": (
        Host(ss="raw UNCONN 0 0 0.0.0.0:255 0.0.0.0:* <->\n"),
        "raw UNCONN 0 0 0.0.0.0:255",
    ),
    "ss address that is not an address": (
        Host(ss="tcp LISTEN 0 128 eth0:22 0.0.0.0:* <->\n"),
        "tcp LISTEN 0 128 eth0:22",
    ),
    "truncated ss line": (Host(ss="tcp LISTEN 0\n"), "tcp LISTEN 0"),
}


@pytest.mark.parametrize(
    ("state", "message"), UNREADABLE_OUTPUTS.values(), ids=UNREADABLE_OUTPUTS.keys()
)
def test_exits_2_naming_the_command_output_it_cannot_read(run_edge_check, state, message):
    exit_code, out, err = run_edge_check(state, "--json")

    assert (exit_code, out) == (2, "")
    assert message in err


def test_exits_2_when_a_host_command_fails(monkeypatch, tmp_path, capsys):
    def docker_is_down() -> str:
        raise host.HostCommandError("docker ps failed with exit 1: daemon not running")

    monkeypatch.setattr(host, "docker_inspect_json", docker_is_down)
    monkeypatch.setattr(host, "socket_listeners", lambda: "")
    allow_list = tmp_path / "edge-check.toml"
    allow_list.write_text("", encoding="utf-8")

    exit_code = cli.main(["--allow-list", str(allow_list)])

    assert exit_code == 2
    assert "docker ps failed" in capsys.readouterr().err


def test_default_allow_list_is_the_projects_whatever_the_cwd(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(host, "docker_inspect_json", lambda: "[]")
    monkeypatch.setattr(host, "socket_listeners", lambda: "")

    exit_code = cli.main(["--json"])

    assert (exit_code, capsys.readouterr().out) == (0, "[]\n")


def test_exits_2_when_the_allow_list_is_missing(tmp_path, capsys):
    exit_code = cli.main(["--allow-list", str(tmp_path / "absent.toml")])

    assert exit_code == 2
    assert "cannot read" in capsys.readouterr().err


@pytest.fixture
def fake_docker(monkeypatch, tmp_path):
    """A `docker` first on PATH that runs the given shell script as its `ps` / `inspect` answers."""

    def install(script: str) -> None:
        executable = tmp_path / "bin" / "docker"
        executable.parent.mkdir()
        executable.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
        executable.chmod(0o755)
        monkeypatch.setenv("PATH", f"{executable.parent}:{os.environ['PATH']}")
        monkeypatch.setattr(host, "socket_listeners", lambda: "")

    return install


def run_against_real_host_commands(tmp_path) -> int:
    allow_list = tmp_path / "edge-check.toml"
    allow_list.write_text("", encoding="utf-8")
    return cli.main(["--allow-list", str(allow_list)])


def test_exits_2_naming_the_command_that_does_not_answer_in_time(
    fake_docker, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(host, "HOST_COMMAND_TIMEOUT_SECONDS", 0.2)
    fake_docker("exec sleep 30")

    assert run_against_real_host_commands(tmp_path) == 2
    assert "docker ps did not answer within 0.2 s" in capsys.readouterr().err


SHORT_INSPECT_ANSWERS = {"empty list": "[]", "one of two": '[{"Name": "/a"}]'}


@pytest.mark.parametrize("answer", SHORT_INSPECT_ANSWERS.values(), ids=SHORT_INSPECT_ANSWERS.keys())
def test_exits_2_when_docker_inspect_answers_fewer_containers_than_docker_ps_listed(
    fake_docker, tmp_path, capsys, answer
):
    fake_docker(f'[ "$1" = ps ] && printf "aaa\\nbbb\\n" || echo \'{answer}\'')

    assert run_against_real_host_commands(tmp_path) == 2
    assert "docker ps listed 2" in capsys.readouterr().err
