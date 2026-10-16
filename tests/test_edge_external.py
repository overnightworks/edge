"""`edge external plan`: tokens only in the child, an allowlisted environment, the pinned tofu."""

import json
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from edge import cli
from edge.external import tofu

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
    calls.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
if command == "version":
    print("OpenTofu v" + config["version"])
if config["echo_environment"]:
    print(json.dumps(dict(os.environ)))
sys.exit(config["exit_codes"].get(command, 0))
"""


@dataclass
class FakeTofu:
    directory: Path

    def configure(self, *, version=None, echo_environment=False, **exit_codes) -> None:
        config = {
            "version": version or tofu.pinned_version(),
            "echo_environment": echo_environment,
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
    assert edge_external_plan() == 0


def test_changes_exit_1_with_one_line_naming_the_cause(home, fake_tofu, capsys):
    fake_tofu.configure(plan=2)
    assert edge_external_plan() == 1
    assert "changes pending" in capsys.readouterr().out


def test_a_failed_plan_exits_2(home, fake_tofu):
    fake_tofu.configure(plan=1)
    assert edge_external_plan() == 2


@pytest.mark.parametrize("token_file", TOKEN_FILES)
def test_a_missing_token_file_exits_2_naming_it_without_running_tofu(
    home, fake_tofu, capsys, token_file
):
    (home / token_file).unlink()
    assert edge_external_plan() == 2
    assert str(home / token_file) in capsys.readouterr().err
    assert fake_tofu.calls() == []


def test_the_tokens_never_reach_the_wrapper_output(home, fake_tofu, capsys):
    (home / ".secrets/github/fleet-read").write_text(SENTINEL)
    fake_tofu.configure(echo_environment=True)
    edge_external_plan()
    printed = capsys.readouterr()
    assert SENTINEL not in printed.out + printed.err
    assert SENTINEL in json.dumps(fake_tofu.call("plan")["env"])


@pytest.mark.parametrize(
    "inherited",
    [
        {"TF_LOG": "TRACE", "TF_LOG_PATH": "{tmp}/trace", "TF_LOG_PROVIDER": "TRACE"},
        {"TF_CLI_ARGS": "-lock=false", "TF_CLI_ARGS_plan": "-refresh=false"},
        {"TF_ENCRYPTION": "unenforced-config"},
        {"TF_VAR_state_passphrase": "wrong", "TF_VAR_other": "x"},
        {"TF_DATA_DIR": "{tmp}/d", "TF_PLUGIN_CACHE_DIR": "{tmp}/c", "TF_WORKSPACE": "w"},
        {"GITHUB_TOKEN": "wrong", "GITHUB_OWNER": "x", "GH_TOKEN": "x", "GH_HOST": "x"},
        {"CLOUDFLARE_API_TOKEN": "wrong", "CLOUDFLARE_EMAIL": "x"},
        {"HTTPS_PROXY": "http://p", "http_proxy": "http://p", "ALL_PROXY": "http://p"},
    ],
    ids=[
        "TF_LOG",
        "TF_CLI_ARGS",
        "TF_ENCRYPTION",
        "TF_VAR",
        "TF_DIRS",
        "GITHUB",
        "CLOUDFLARE",
        "PROXY",
    ],
)
def test_tofu_gets_exactly_the_allowlisted_variables(
    home, fake_tofu, monkeypatch, tmp_path, inherited
):
    for name, value in inherited.items():
        monkeypatch.setenv(name, value.format(tmp=tmp_path))
    assert edge_external_plan() == 0
    environment = fake_tofu.call("plan")["env"]
    assert set(environment) == ALLOWED_NAMES
    assert environment["TF_VAR_state_passphrase"] == "file-passphrase"
    assert environment["GITHUB_TOKEN"] == "github-token"
    assert not (tmp_path / "trace").exists()


def test_the_child_environment_values(home, fake_tofu):
    edge_external_plan()
    environment = fake_tofu.call("plan")["env"]
    assert environment["PATH"] == f"{fake_tofu.directory.resolve()}:/usr/bin:/bin"
    assert environment["HOME"] == str(home)
    assert environment["LANG"] == "C.UTF-8"
    assert environment["TF_INPUT"] == "0"
    assert environment["TF_CLI_CONFIG_FILE"] == str(PROJECT_ROOT / "external/tofurc")
    assert Path(environment["TMPDIR"]).parent == home / ".secrets/opentofu/tmp"


def test_the_temporary_directory_is_private_and_removed_afterwards(home, fake_tofu):
    fake_tofu.configure(echo_environment=False)
    edge_external_plan()
    parent = home / ".secrets/opentofu/tmp"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert list(parent.iterdir()) == []


def test_another_tofu_version_exits_2_naming_both_versions(home, fake_tofu, capsys):
    fake_tofu.configure(version="1.12.0")
    assert edge_external_plan() == 2
    error = capsys.readouterr().err
    assert "1.12.0" in error
    assert tofu.pinned_version() in error
    assert [call["argv"][0] for call in fake_tofu.calls()] == ["version"]


def test_init_is_locked_non_interactive_and_uses_the_state_in_secrets(home, fake_tofu):
    edge_external_plan()
    assert fake_tofu.call("init")["argv"] == [
        "init",
        "-input=false",
        "-lockfile=readonly",
        f"-backend-config=path={home}/.secrets/opentofu/fleet.tfstate",
    ]
    assert fake_tofu.call("plan")["argv"] == ["plan", "-refresh=true", "-detailed-exitcode"]


def test_a_failed_init_exits_2_without_planning(home, fake_tofu):
    fake_tofu.configure(init=1)
    assert edge_external_plan() == 2
    assert [call["argv"][0] for call in fake_tofu.calls()] == ["version", "init"]


def test_edge_external_help_exits_0_and_edge_help_lists_it(capsys):
    with pytest.raises(SystemExit) as external_help:
        cli.main(["external", "--help"])
    assert external_help.value.code == 0
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "external" in capsys.readouterr().out


def test_the_only_new_script_is_edge():
    scripts = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["edge"] == "edge.cli:main"
    assert "edge-external" not in scripts
