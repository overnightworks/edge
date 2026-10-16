"""`edge external plan`: tokens only in the child, an allowlisted environment, the pinned tofu."""

import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from edge import cli
from edge.external import secrets, tofu

SENTINEL = "sentinel-4b1d-never-print-me"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_NAMES = {
    "PATH",
    "HOME",
    "LANG",
    "TMPDIR",
    "TF_INPUT",
    "TF_CLI_CONFIG_FILE",
    "CLOUDFLARE_API_TOKEN",
    "GITHUB_TOKEN",
    "TF_VAR_state_passphrase",
}
TOKEN_FILES = {
    ".secrets/cloudflare/fleet-read": "cloudflare-token",
    ".secrets/github/fleet-read": "github-token",
    ".secrets/opentofu/state-passphrase": "file-passphrase",
}

FAKE_TOFU = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path

here = Path(__file__).parent
config = json.loads((here / "fake.json").read_text())
command = sys.argv[1]
with (here / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps({{
        "argv": sys.argv[1:],
        "env": dict(os.environ),
        "tmpdir_mode": os.stat(os.environ["TMPDIR"]).st_mode & 0o777,
    }}) + "\\n")
locked = Path(os.environ["TMPDIR"]) / config["locked_name"]
if config["leave_undeletable"] and not locked.exists():
    locked.mkdir()
    (locked / config["locked_name"]).write_text("x")
    locked.chmod(0o500)
if command == "version":
    print("OpenTofu v" + config["version"])
if config["echo_environment"]:
    print(json.dumps(dict(os.environ)))
sys.exit(config["exit_codes"].get(command, 0))
"""


@dataclass
class FakeTofu:
    directory: Path

    def configure(
        self,
        *,
        version=None,
        echo_environment=False,
        leave_undeletable=False,
        locked_name="locked",
        **exit_codes,
    ) -> None:
        config = {
            "version": version or tofu.pinned_version(),
            "echo_environment": echo_environment,
            "leave_undeletable": leave_undeletable,
            "locked_name": locked_name,
            "exit_codes": exit_codes,
        }
        (self.directory / "fake.json").write_text(json.dumps(config))

    def calls(self) -> list[dict]:
        log = self.directory / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def call(self, command: str) -> dict:
        return next(call for call in self.calls() if call["argv"][0] == command)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    for relative, value in TOKEN_FILES.items():
        token_file = home / relative
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(value + "\n")
        token_file.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def fake_tofu(tmp_path, monkeypatch) -> FakeTofu:
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / "tofu"
    executable.write_text(FAKE_TOFU)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}:/usr/bin:/bin")
    fake = FakeTofu(directory)
    fake.configure()
    return fake


def edge_external_plan() -> int:
    return cli.main(["external", "plan"])


def test_a_clean_plan_exits_0(home, fake_tofu):
    """#40 rule 4: no changes exits 0."""
    assert edge_external_plan() == 0


def test_changes_exit_1_with_one_line_naming_the_cause(home, fake_tofu, capsys):
    """#40 rule 4: pending changes exit 1 with one line naming the cause."""
    fake_tofu.configure(plan=2)
    assert edge_external_plan() == 1
    assert "changes pending" in capsys.readouterr().out


def test_a_failed_plan_exits_2(home, fake_tofu, capsys):
    """#40 rule 4: a failed plan exits 2 and names the cause on stderr."""
    fake_tofu.configure(plan=1)
    assert edge_external_plan() == 2
    error = capsys.readouterr().err
    assert "tofu plan failed (exit 1)" in error
    assert "edge external plan:" in error


@pytest.mark.parametrize("token_file", TOKEN_FILES)
def test_a_missing_token_file_exits_2_naming_it_without_running_tofu(
    home, fake_tofu, capsys, token_file
):
    """#40 rule 1: a missing token file stops the run before tofu."""
    (home / token_file).unlink()
    assert edge_external_plan() == 2
    assert str(home / token_file) in capsys.readouterr().err
    assert fake_tofu.calls() == []


def test_the_tokens_never_reach_the_wrapper_output(home, fake_tofu, capsys):
    """#40 rule 1: tokens reach only the tofu child, never the wrapper output."""
    (home / ".secrets/github/fleet-read").write_text(SENTINEL)
    fake_tofu.configure(echo_environment=True)
    edge_external_plan()
    printed = capsys.readouterr()
    assert SENTINEL not in printed.out + printed.err
    assert SENTINEL in json.dumps(fake_tofu.call("plan")["env"])


DROPPED_VARIABLE_FAMILIES = [
    pytest.param(
        {"TF_LOG": "TRACE", "TF_LOG_PATH": "{tmp}/trace", "TF_LOG_PROVIDER": "TRACE"}, id="TF_LOG"
    ),
    pytest.param(
        {"TF_CLI_ARGS": "-lock=false", "TF_CLI_ARGS_plan": "-refresh=false"}, id="TF_CLI_ARGS"
    ),
    pytest.param({"TF_ENCRYPTION": "unenforced-config"}, id="TF_ENCRYPTION"),
    pytest.param({"TF_VAR_state_passphrase": "wrong", "TF_VAR_other": "x"}, id="TF_VAR"),
    pytest.param(
        {"TF_DATA_DIR": "{tmp}/d", "TF_PLUGIN_CACHE_DIR": "{tmp}/c", "TF_WORKSPACE": "w"},
        id="TF_DIRS",
    ),
    pytest.param(
        {"GITHUB_TOKEN": "wrong", "GITHUB_OWNER": "x", "GH_TOKEN": "x", "GH_HOST": "x"},
        id="GITHUB",
    ),
    pytest.param({"CLOUDFLARE_API_TOKEN": "wrong", "CLOUDFLARE_EMAIL": "x"}, id="CLOUDFLARE"),
    pytest.param(
        {"HTTPS_PROXY": "http://p", "http_proxy": "http://p", "ALL_PROXY": "http://p"}, id="PROXY"
    ),
]
TOFU_VERSIONS = [
    pytest.param(None, 0, id="pinned"),
    pytest.param("1.12.0", 2, id="other"),
]


def inherit(monkeypatch, tmp_path, variables):
    for name, value in variables.items():
        monkeypatch.setenv(name, value.format(tmp=tmp_path))


@pytest.mark.parametrize("version,expected_exit", TOFU_VERSIONS)
@pytest.mark.parametrize("inherited", DROPPED_VARIABLE_FAMILIES)
def test_tofu_gets_exactly_the_allowlisted_variables(
    home, fake_tofu, monkeypatch, tmp_path, inherited, version, expected_exit
):
    """#40 rule 2: tofu gets exactly the allowlisted variables, tokens from the files."""
    fake_tofu.configure(version=version)
    inherit(monkeypatch, tmp_path, inherited)
    assert edge_external_plan() == expected_exit
    environment = fake_tofu.call("version")["env"]
    assert set(environment) == ALLOWED_NAMES
    assert environment["TF_VAR_state_passphrase"] == "file-passphrase"
    assert environment["GITHUB_TOKEN"] == "github-token"
    assert environment["CLOUDFLARE_API_TOKEN"] == "cloudflare-token"
    assert not (tmp_path / "trace").exists()


def test_the_child_environment_values(home, fake_tofu):
    """#40 rule 2: the allowlisted variables carry the wrapper's own values."""
    edge_external_plan()
    environment = fake_tofu.call("plan")["env"]
    assert environment["PATH"] == f"{fake_tofu.directory.resolve()}:/usr/bin:/bin"
    assert environment["HOME"] == str(home)
    assert environment["LANG"] == "C.UTF-8"
    assert environment["TF_INPUT"] == "0"
    assert environment["TF_CLI_CONFIG_FILE"] == str(PROJECT_ROOT / "external/tofurc")
    assert Path(environment["TMPDIR"]).parent == home / ".secrets/opentofu/tmp"


def test_the_temporary_directory_is_private_and_removed_afterwards(home, fake_tofu):
    """#40 rule 2: TMPDIR is a 0700 directory removed afterwards."""
    edge_external_plan()
    assert fake_tofu.call("plan")["tmpdir_mode"] == 0o700
    assert list((home / ".secrets/opentofu/tmp").iterdir()) == []


def test_another_tofu_version_exits_2_naming_both_versions(home, fake_tofu, capsys):
    """#40 rule 3: another tofu version exits 2 naming both versions."""
    fake_tofu.configure(version="1.12.0")
    assert edge_external_plan() == 2
    error = capsys.readouterr().err
    assert "1.12.0" in error
    assert tofu.pinned_version() in error
    assert [call["argv"][0] for call in fake_tofu.calls()] == ["version"]


def test_init_is_locked_non_interactive_and_uses_the_state_in_secrets(home, fake_tofu):
    """#40 rule 3: init is locked, non-interactive and uses the state in secrets."""
    edge_external_plan()
    assert fake_tofu.call("init")["argv"] == [
        "init",
        "-input=false",
        "-lockfile=readonly",
        f"-backend-config=path={home}/.secrets/opentofu/fleet.tfstate",
    ]
    assert fake_tofu.call("plan")["argv"] == ["plan", "-refresh=true", "-detailed-exitcode"]


def test_a_failed_init_exits_2_without_planning(home, fake_tofu):
    """#40 rule 4: a failed init exits 2 without planning."""
    fake_tofu.configure(init=1)
    assert edge_external_plan() == 2
    assert [call["argv"][0] for call in fake_tofu.calls()] == ["version", "init"]


def test_edge_external_help_exits_0_and_edge_help_lists_it(capsys):
    """#40 rule 5: `edge external --help` works and `edge --help` lists it."""
    with pytest.raises(SystemExit) as external_help:
        cli.main(["external", "--help"])
    assert external_help.value.code == 0
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "external" in capsys.readouterr().out


def test_the_only_new_script_is_edge():
    """#40 rule 5: no new script besides `edge`."""
    scripts = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["edge"] == "edge.cli:main"
    assert "edge-external" not in scripts


@pytest.mark.parametrize(
    "version", [version.values[0] for version in TOFU_VERSIONS], ids=["pinned", "other"]
)
@pytest.mark.parametrize("family", DROPPED_VARIABLE_FAMILIES)
@pytest.mark.parametrize("credential_inherited", [True, False], ids=["inherited", "absent"])
@pytest.mark.parametrize(
    "token_file,variable",
    [
        (".secrets/cloudflare/fleet-read", "CLOUDFLARE_API_TOKEN"),
        (".secrets/github/fleet-read", "GITHUB_TOKEN"),
        (".secrets/opentofu/state-passphrase", "TF_VAR_state_passphrase"),
    ],
)
def test_a_missing_token_file_is_not_replaced_by_the_inherited_credential(
    home,
    fake_tofu,
    monkeypatch,
    tmp_path,
    capsys,
    token_file,
    variable,
    family,
    version,
    credential_inherited,
):
    """#40 rule 1: an inherited credential never stands in for an absent token file."""
    fake_tofu.configure(version=version)
    inherit(monkeypatch, tmp_path, family)
    if credential_inherited:
        monkeypatch.setenv(variable, SENTINEL)
    else:
        monkeypatch.delenv(variable, raising=False)
    (home / token_file).unlink()
    assert edge_external_plan() == 2
    printed = capsys.readouterr()
    assert str(home / token_file) in printed.err
    assert SENTINEL not in printed.out + printed.err
    assert fake_tofu.calls() == []


def test_a_token_file_that_is_not_utf8_exits_2_without_quoting_it(home, fake_tofu, capsys):
    """#40 rule 4: an unreadable token file is exit 2, not a traceback with exit 1."""
    (home / ".secrets/github/fleet-read").write_bytes(b"\xff\xfe")
    assert edge_external_plan() == 2
    assert "not valid UTF-8" in capsys.readouterr().err
    assert fake_tofu.calls() == []


def test_a_temporary_parent_that_is_a_file_exits_2_naming_error_and_path(home, fake_tofu, capsys):
    """#40 rule 4: a filesystem failure while preparing the run is exit 2 naming what failed."""
    in_the_way = home / ".secrets/opentofu/tmp"
    in_the_way.write_text("in the way")
    assert edge_external_plan() == 2
    error = capsys.readouterr().err
    assert "FileExistsError" in error
    assert str(in_the_way) in error
    assert fake_tofu.calls() == []


def test_a_version_report_carrying_a_token_never_prints_it(home, fake_tofu, capsys):
    """#40 rule 1: child-derived diagnostics are redacted before they are printed."""
    fake_tofu.configure(version=TOKEN_FILES[".secrets/github/fleet-read"])
    assert edge_external_plan() == 2
    printed = capsys.readouterr()
    assert "github-token" not in printed.out + printed.err


def test_a_temporary_directory_that_cannot_be_removed_exits_2(home, fake_tofu, capsys):
    """#40 rule 4: a cleanup failure is reported, not reported as success."""
    fake_tofu.configure(leave_undeletable=True)
    try:
        assert edge_external_plan() == 2
        assert "PermissionError" in capsys.readouterr().err
    finally:
        for locked in (home / ".secrets/opentofu/tmp").glob("*/locked"):
            locked.chmod(0o700)


def test_a_cleanup_failure_never_prints_a_token_named_by_the_child(home, fake_tofu, capsys):
    """#40 rule 1: a child-controlled name in a cleanup failure is redacted."""
    fake_tofu.configure(leave_undeletable=True, locked_name="github-token")
    try:
        assert edge_external_plan() == 2
        printed = capsys.readouterr()
        assert "PermissionError" in printed.err
        assert "github-token" not in printed.out + printed.err
    finally:
        for locked in (home / ".secrets/opentofu/tmp").glob("*/github-token"):
            locked.chmod(0o700)


def test_the_temporary_directory_is_removed_when_the_run_is_interrupted(home):
    """#40 rule 2: Ctrl-C during tofu still removes TMPDIR."""
    with pytest.raises(KeyboardInterrupt), secrets.private_temporary_directory(home) as directory:
        (directory / "child-artifact").write_text("x")
        raise KeyboardInterrupt
    assert list((home / ".secrets/opentofu/tmp").iterdir()) == []


def test_a_cleanup_failure_after_another_version_still_names_both_versions(home, fake_tofu, capsys):
    """#40 rule 3: a failing cleanup does not hide the version mismatch already in flight."""
    fake_tofu.configure(version="1.12.0", leave_undeletable=True)
    try:
        assert edge_external_plan() == 2
        error = capsys.readouterr().err
        assert "1.12.0" in error
        assert tofu.pinned_version() in error
        assert "cannot remove" in error
    finally:
        for locked in (home / ".secrets/opentofu/tmp").glob("*/locked"):
            locked.chmod(0o700)
