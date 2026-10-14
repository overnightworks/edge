"""edge-check driven through its CLI, with fixture command outputs in place of the host."""

import json
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

SS_LOOPBACK_ONLY = """\
udp UNCONN 0 0 127.0.0.53%lo:53    0.0.0.0:* cgroup:/system.slice/systemd-resolved.service <->
tcp LISTEN 0 4096 127.0.0.1:8081  0.0.0.0:* cgroup:/system.slice/docker.service <->
tcp LISTEN 0 511 [::1]:6379     [::]:* users:(("redis-server",pid=4242,fd=7)) <->
"""
SS_AVAHI = """\
udp UNCONN 0 0 0.0.0.0:5353  0.0.0.0:* uid:108 cgroup:/system.slice/avahi-daemon.service <->
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
SS_CHROME_MDNS_TWICE = """\
udp UNCONN 0 0 224.0.0.251:5353  0.0.0.0:* users:(("chrome",pid=16393,fd=49)) <->
udp UNCONN 0 0 224.0.0.251:5353  0.0.0.0:* users:(("chrome",pid=16393,fd=48)) <->
"""


def inspect_json(*containers: tuple[str, dict[str, list[tuple[str, str]]]]) -> str:
    """`docker inspect` output for (name, {"80/tcp": [(host_ip, host_port)]}) pairs."""
    return json.dumps(
        [
            {
                "Name": f"/{name}",
                "HostConfig": {
                    "PortBindings": {
                        container_port: [{"HostIp": ip, "HostPort": port} for ip, port in bindings]
                        for container_port, bindings in port_bindings.items()
                    }
                },
            }
            for name, port_bindings in containers
        ]
    )


def finding(
    problem: str, subject: str, protocol: str, address: str, port: int | str | None
) -> dict:
    return {
        "problem": problem,
        "subject": subject,
        "protocol": protocol,
        "address": address,
        "port": port,
    }


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
            ("traefik", {"8081/tcp": [("127.0.0.1", "8081")]}),
            ("dns", {"53/udp": [("::1", "5353")]}),
            ("worker", {}),
        ),
        ss=SS_LOOPBACK_ONLY,
    ),
    "allow-listed mDNS port": Host(ss=SS_AVAHI, allow_list=ALLOW_AVAHI),
    "allow-listed owner on a random port": Host(ss=SS_CLOUDFLARED, allow_list=ALLOW_CLOUDFLARED),
    "publication on 0.0.0.0": Host(
        docker=inspect_json(("grafana", {"3000/tcp": [("0.0.0.0", "3000")]})),
        findings=[finding("publication-beyond-loopback", "grafana", "tcp", "0.0.0.0", 3000)],
    ),
    "publication on ::": Host(
        docker=inspect_json(("dns", {"53/udp": [("::", "53")]})),
        findings=[finding("publication-beyond-loopback", "dns", "udp", "::", 53)],
    ),
    "publication on a loopback port range": Host(
        docker=inspect_json(("ranged", {"80/tcp": [("127.0.0.1", "8000-8010")]})),
    ),
    "publication on a 0.0.0.0 port range names the range": Host(
        docker=inspect_json(("ranged", {"80/tcp": [("0.0.0.0", "8000-8010")]})),
        findings=[finding("publication-beyond-loopback", "ranged", "tcp", "0.0.0.0", "8000-8010")],
    ),
    "publication without host IP or host port": Host(
        docker=inspect_json(("hello", {"80/tcp": [("", "")]})),
        findings=[finding("publication-without-host-ip", "hello", "tcp", "", None)],
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
    "repeated sockets and mixed findings come once, in a stable order": Host(
        docker=inspect_json(
            ("web", {"8080/tcp": [("0.0.0.0", "8080"), ("::", "8080")]}),
            ("api", {"9000/tcp": [("", "9000")]}),
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
    "container without HostConfig": (
        Host(docker=json.dumps([{"Name": "/broken"}])),
        "container 'broken'",
    ),
    "host port that is not a number": (
        Host(docker=inspect_json(("web", {"80/tcp": [("127.0.0.1", "http")]}))),
        "container 'web'",
    ),
    "host IP that is not an address": (
        Host(docker=inspect_json(("web", {"80/tcp": [("localhost", "80")]}))),
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


def test_exits_2_when_the_allow_list_is_missing(tmp_path, capsys):
    exit_code = cli.main(["--allow-list", str(tmp_path / "absent.toml")])

    assert exit_code == 2
    assert "cannot read" in capsys.readouterr().err
