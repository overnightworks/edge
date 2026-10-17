"""The files of `tunnel/` (#76): the cloudflared unit, its drop-ins and the stop-only polkit rule
that home-tools#9 installs. edge only ships them; the pins below keep them equal to the facts
`edge.tunnel` and `edge.switches` own. polkit's real verdict and `systemd-analyze verify` are
home-tools#9 B1's, where cloudflared and systemd as PID 1 exist."""

import re
from pathlib import Path

import pytest

from edge import cli, switches, tunnel

TUNNEL_FILES = Path(__file__).resolve().parents[1] / "tunnel"
BASE_UNIT = TUNNEL_FILES / tunnel.CLOUDFLARED_UNIT
DROP_INS = TUNNEL_FILES / f"{tunnel.CLOUDFLARED_UNIT}.d"
NONROOT_DROP_IN = DROP_INS / "20-nonroot.conf"
BOOT_DROP_IN = DROP_INS / tunnel.BOOT_DROP_IN
POLKIT_RULE = TUNNEL_FILES / "polkit" / "10-edge-cloudflared-stop.rules"
ALL_FILES = (BASE_UNIT, NONROOT_DROP_IN, BOOT_DROP_IN, POLKIT_RULE)

PLACEHOLDER = re.compile(r"@[A-Z_]+@")
CLEAN_EXEC_START = (
    "ExecStart=/usr/bin/cloudflared --no-autoupdate --config /etc/cloudflared/config.yml tunnel run"
)
# `systemctl cat cloudflared.service` on the PC, 17.10.2026 (read-only, the tunnel id replaced by
# its placeholder), without its `ExecStart` lines: the stale one of the base unit and the
# `10-root-binary.conf` override that the clean base unit replaces.
EFFECTIVE_UNIT_WITHOUT_EXEC_START = frozenset(
    {
        "Description=cloudflared",
        "After=network-online.target",
        "Wants=network-online.target",
        "TimeoutStartSec=15",
        "Type=notify",
        "Restart=on-failure",
        "RestartSec=5s",
        "WantedBy=multi-user.target",
        "DynamicUser=yes",
        "LoadCredential=tunnel.json:/etc/cloudflared/@TUNNEL_ID@.json",
        "Environment=TUNNEL_CRED_FILE=%d/tunnel.json",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
    }
)
EDGE_SUBCOMMANDS = "{apply,stop,start,external}"


def render(text: str, values: dict[str, str]) -> str:
    """What the installer does: every `@NAME@` replaced by its value."""
    for name, value in values.items():
        text = text.replace(f"@{name}@", value)
    return text


def key_value_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "=" in line and not line.startswith("#")]


def test_the_boot_drop_in_carries_the_name_edge_checks_for():
    """#76 rule 3, cell 1."""
    assert sorted(path.name for path in DROP_INS.iterdir()) == [
        NONROOT_DROP_IN.name,
        tunnel.BOOT_DROP_IN,
    ]


def test_the_boot_drop_in_skips_the_tunnel_while_the_global_stop_exists(tmp_path, monkeypatch):
    """#76 rule 3, cell 2: rendered with a temporary HOME, the condition names switches' marker."""
    monkeypatch.setenv("HOME", str(tmp_path))
    marker = switches.default_state_directory() / switches.GLOBAL_STOP_FILE
    rendered = render(BOOT_DROP_IN.read_text(), {"HOME": str(tmp_path)})
    assert key_value_lines(rendered) == [
        f"ConditionPathExists=!{marker}",
        f"RequiresMountsFor={marker.parent}",
    ]
    assert "[Unit]" in rendered.splitlines()


def test_the_polkit_rule_grants_only_the_operators_stop_of_the_tunnel():
    """#76 rule 1, rule 3, cell 3."""
    rule = POLKIT_RULE.read_text()
    for condition in (
        f'action.id == "{tunnel.MANAGE_UNITS_ACTION}"',
        f'action.lookup("unit") == "{tunnel.CLOUDFLARED_UNIT}"',
        'action.lookup("verb") == "stop"',
        'subject.user == "@OPERATOR_USER@"',
    ):
        assert condition in rule
    assert re.findall(r"polkit\.Result\.\w+", rule) == ["polkit.Result.YES"]
    assert rule.count("return") == 1
    assert "subject.local" not in rule and "subject.active" not in rule


def test_the_polkit_rule_is_es5():
    """#76 rule 1, cell 4: polkit 124 runs its rules on Duktape."""
    code = "\n".join(
        line for line in POLKIT_RULE.read_text().splitlines() if not line.lstrip().startswith("//")
    )
    assert "=>" not in code
    assert not re.search(r"\b(let|const)\b", code)


def test_the_files_carry_exactly_the_three_placeholders_the_installer_fills():
    """#76 rule 2, cell 5."""
    texts = [path.read_text() for path in ALL_FILES]
    assert {name for text in texts for name in PLACEHOLDER.findall(text)} == {
        "@HOME@",
        "@OPERATOR_USER@",
        "@TUNNEL_ID@",
    }
    values = {"HOME": "/home/someone", "OPERATOR_USER": "someone", "TUNNEL_ID": "0000"}
    assert not [text for text in texts if PLACEHOLDER.search(render(text, values))]


def test_the_base_unit_and_20_are_todays_effective_unit_with_the_clean_exec_start():
    """#76 rule 1, cell 6: only the stale binary path of the base unit goes."""
    lines = key_value_lines(BASE_UNIT.read_text()) + key_value_lines(NONROOT_DROP_IN.read_text())
    exec_starts = [line for line in lines if line.startswith("ExecStart")]
    assert exec_starts == [CLEAN_EXEC_START]
    assert sorted(line for line in lines if line not in exec_starts) == sorted(
        EFFECTIVE_UNIT_WITHOUT_EXEC_START
    )


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda path: path.name)
def test_no_tunnel_file_asks_for_sudo(path):
    """#76 rule 4, cell 9: the files are content; installing them is home-tools#9's one sudo."""
    assert "sudo" not in path.read_text()


def test_edge_gains_no_command_to_render_or_install_the_tunnel_files(capsys):
    """#76 rule 4, cell 9: no `edge system render` or any other new command."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert EDGE_SUBCOMMANDS in capsys.readouterr().out
