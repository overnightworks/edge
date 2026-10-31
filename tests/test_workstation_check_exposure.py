"""Tests of the exposure area of the daily check: what the PC lets in from beyond loopback, as rows, report, mail and remembered identities."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from pathlib import Path

import pytest
from support.workstation_support import (
    RunnerWithCleanLogs,
    Sandbox,
    home_check,
    message_of,
    write_hal_config,
)

from hal.workstation.layout import Layout

pytestmark = pytest.mark.usefixtures("quiet_pc")

REACHABLE = "sshd can be reached from beyond this PC."
SSHD_ON_PORT = 'tcp LISTEN 0 128 0.0.0.0:{port} 0.0.0.0:* users:(("sshd",pid=900,fd=3)) <->\n'
SSHD_ROW = "  ! exposure        1 action       sshd can be reached from beyond this PC"
BROKEN_ROW = "  ✗ exposure        not checked    the check did not run · details in the report"
ENV_CANARY = "CANARY-ENV-VALUE-0xDEADBEEF"
PUBLISHED_ADDRESS = "0.0.0.0"  # noqa: S104 - the address of a publication open to every IPv4 interface


@pytest.fixture
def clean_home(sandbox: Sandbox, furnish_clean_home: Callable[[Path], Path]) -> Path:
    return furnish_clean_home(sandbox.home)


@pytest.fixture
def runner() -> RunnerWithCleanLogs:
    return RunnerWithCleanLogs()


def docker_with_containers(directory: Path, *containers: dict) -> str:
    """A `docker` that lists one container id per container and inspects them all."""
    listing = directory / "inspect.json"
    listing.write_text(json.dumps(list(containers)))
    ids = " ".join(f"id{number}" for number, _ in enumerate(containers))
    return f'case "$1" in ps) echo {ids};; inspect) cat {shlex.quote(str(listing))};; esac'


def published_container(
    name: str, host_ip: str, host_port: str, labels: dict[str, str] | None = None
) -> dict:
    return {
        "Name": f"/{name}",
        "State": {"Running": True},
        "Config": {"Labels": labels, "Env": [f"SECRET={ENV_CANARY}"]},
        "HostConfig": {"PortBindings": {"80/tcp": [{"HostIp": host_ip, "HostPort": host_port}]}},
    }


def test_a_listener_beyond_loopback_is_an_action_in_the_sixth_area(
    clean_home, runner, mail_stub, capsys
) -> None:
    """One row, the action sentence in the mail and popup, and the identity of rule and subject."""
    code = home_check(clean_home, runner, listeners=SSHD_ON_PORT.format(port=22))

    output = capsys.readouterr().out.splitlines()
    body = message_of(mail_stub.sent[0]).get_content()
    assert code == 1
    assert output[5] == SSHD_ROW
    assert (
        f'1. {REACHABLE} (new)\n   Next step: Tell an agent: "sshd can be reached from beyond this PC, please look".'
        in body
    )
    assert "\nExposure: Action needed\n" in body
    assert runner.notifications == ["PC check: action needed – Exposure"]
    assert "exposure\treachable-beyond-loopback\tsshd" in json.loads(
        Layout(clean_home).check_findings_file.read_text()
    )


def test_the_same_exposure_on_another_port_is_not_news(clean_home, runner, mail_stub) -> None:
    """The identity is rule plus subject, so a moved port sends nothing."""
    home_check(clean_home, runner, listeners=SSHD_ON_PORT.format(port=22))

    home_check(clean_home, runner, listeners=SSHD_ON_PORT.format(port=2222))

    assert len(mail_stub.sent) == 1
    assert "| reachable-beyond-loopback | sshd | 1 TCP port beyond loopback |" in (
        Layout(clean_home).check_report_file.read_text()
    )


def test_an_exposure_that_is_gone_is_reported_done(clean_home, runner, mail_stub) -> None:
    home_check(clean_home, runner, listeners=SSHD_ON_PORT.format(port=22))

    code = home_check(clean_home, runner)

    body = message_of(mail_stub.sent[1]).get_content()
    assert code == 0
    assert "Done\n\n- sshd can no longer be reached from beyond this PC.\n" in body
    assert "Exposure: OK" in body


def test_a_clean_pc_says_all_six_areas_are_well(clean_home, runner, capsys) -> None:
    code = home_check(clean_home, runner)

    output = capsys.readouterr().out.splitlines()
    assert (code, output[5]) == (0, "  ✓ exposure        ok")
    assert output[6].startswith("✓ all well · 6 areas checked")


def test_a_scan_that_cannot_run_is_a_failure_line_and_the_mail_says_so(
    clean_home, runner, mail_stub, capsys
) -> None:
    """Docker down is exit 3, one broken row, the failure in the report and a mail line."""
    code = home_check(
        clean_home, runner, docker_script="echo 'Cannot connect to the Docker daemon' >&2; exit 1"
    )

    captured = capsys.readouterr()
    assert code == 3
    assert captured.out.splitlines()[5] == BROKEN_ROW
    assert captured.err.splitlines()[0] == "error: 1 of 6 areas could not check · mail sent"
    assert (
        "check exposure failed (ExposureCheckError):"
        in Layout(clean_home).check_report_file.read_text()
    )
    assert "The Exposure check did not run." in message_of(mail_stub.sent[0]).get_content()


def test_a_recovered_scan_is_reported_in_the_mail_as_running_again(
    clean_home, runner, mail_stub
) -> None:
    home_check(clean_home, runner, docker_script="exit 1")

    code = home_check(clean_home, runner)

    assert code == 0
    assert "The Exposure check runs again." in message_of(mail_stub.sent[1]).get_content()


def test_ctrl_c_during_the_scan_leaves_report_and_state_unchanged(
    clean_home, runner, mail_stub
) -> None:
    layout = Layout(clean_home)

    code = home_check(clean_home, runner, docker_script="kill -INT $PPID; exec tail -f /dev/null")

    assert code == 130
    assert not layout.check_report_file.exists()
    assert not layout.check_findings_file.exists()
    assert mail_stub.sent == []


def test_neither_the_address_the_port_nor_an_env_value_reach_mail_or_popup(
    clean_home, runner, mail_stub, tmp_path
) -> None:
    """The mail names the container and nothing more; no env value is read into any output."""
    docker = docker_with_containers(
        tmp_path, published_container("edge-keycloak-1", PUBLISHED_ADDRESS, "8080")
    )

    home_check(clean_home, runner, docker_script=docker)

    layout = Layout(clean_home)
    body = message_of(mail_stub.sent[0]).get_content()
    assert "edge-keycloak-1 can be reached from beyond this PC." in body
    carried = [body, *runner.notifications, *runner.notification_bodies]
    assert not any(token in text for text in carried for token in (PUBLISHED_ADDRESS, "8080", "tcp"))
    everywhere = [
        *carried,
        layout.check_report_file.read_text(),
        layout.check_findings_file.read_text(),
    ]
    assert not any(ENV_CANARY in text for text in everywhere)
    report = layout.check_report_file.read_text()
    assert PUBLISHED_ADDRESS not in report
    assert "8080" not in report


def test_a_duplicate_routing_name_is_an_action(clean_home, runner, mail_stub, tmp_path) -> None:
    routed = {"traefik.enable": "true", "edge.name": "blog"}
    docker = docker_with_containers(
        tmp_path,
        published_container("blog-old", "127.0.0.1", "80", routed),
        published_container("blog-new", "127.0.0.1", "81", routed),
    )

    code = home_check(clean_home, runner, docker_script=docker)

    assert code == 1
    assert (
        "1. The routing name of blog-new, blog-old cannot be used. (new)"
        in message_of(mail_stub.sent[0]).get_content()
    )
    assert "exposure\trouting-name-unusable\tblog-new, blog-old" in json.loads(
        Layout(clean_home).check_findings_file.read_text()
    )


def test_a_container_that_publishes_beyond_loopback_and_without_a_host_ip_stays_two_lines(
    clean_home, runner, mail_stub, tmp_path
) -> None:
    container = published_container("edge-web-1", PUBLISHED_ADDRESS, "8080")
    container["HostConfig"]["PortBindings"]["81/tcp"] = [{"HostIp": "", "HostPort": "8081"}]
    docker = docker_with_containers(tmp_path, container)

    home_check(clean_home, runner, docker_script=docker)

    layout = Layout(clean_home)
    report = layout.check_report_file.read_text()
    assert "| reachable-beyond-loopback | edge-web-1 | 1 TCP port beyond loopback |" in report
    assert "| reachable-beyond-loopback | edge-web-1 | 1 TCP port without a host IP |" in report
    assert "| 1 finding" in report.splitlines()[0]
    assert "| 2 findings" in next(line for line in report.splitlines() if "# exposure" in line)
    identities = json.loads(layout.check_findings_file.read_text())
    assert "exposure\treachable-beyond-loopback\tedge-web-1" in identities
    assert len(identities) == 1


def test_many_ports_of_one_process_are_one_row_one_mail_line_and_one_report_line_with_the_count(
    clean_home, runner, mail_stub, capsys
) -> None:
    listeners = "".join(
        f'udp UNCONN 0 0 0.0.0.0:{port} 0.0.0.0:* users:(("dnsmasq",pid=7,fd=3))\n'
        for port in range(40000, 40272)
    )

    home_check(clean_home, runner, listeners=listeners)

    body = message_of(mail_stub.sent[0]).get_content()
    exposure_rows = [row for row in capsys.readouterr().out.splitlines() if "exposure" in row]
    assert len(exposure_rows) == 1
    assert "dnsmasq can be reached from beyond this PC" in exposure_rows[0]
    assert "dnsmasq can be reached from beyond this PC." in body
    assert body.count("dnsmasq") == 2  # the sentence and its step
    report = Layout(clean_home).check_report_file.read_text()
    assert report.count("| reachable-beyond-loopback | dnsmasq |") == 1
    assert "| reachable-beyond-loopback | dnsmasq | 272 UDP ports beyond loopback |" in report
    assert "| 1 finding" in report.splitlines()[0]
    assert "| 1 finding" in next(line for line in report.splitlines() if "# exposure" in line)


def test_switching_notifications_off_silences_exposure_news(clean_home, runner, mail_stub) -> None:
    write_hal_config(clean_home, "[check]\nnotify = false\n")

    code = home_check(clean_home, runner, listeners=SSHD_ON_PORT.format(port=22))

    assert code == 1
    assert "exposure\treachable-beyond-loopback\tsshd" in json.loads(
        Layout(clean_home).check_findings_file.read_text()
    )
    assert mail_stub.sent == []
    assert runner.notifications == []
