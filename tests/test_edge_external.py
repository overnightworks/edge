"""`edge external`: tokens only in the child, an allowlisted environment, the pinned tofu."""

import fcntl
import json
import os
import pty
import select
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from edge import checkout, cli
from edge.external import apply as external_apply
from edge.external import cli as external_cli
from edge.external import cloudflare_read, secrets, tofu

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
WRITE_TOKEN_FILES = {
    ".secrets/cloudflare/fleet-write": "cloudflare-write-token",
    ".secrets/github/fleet-admin": "github-admin-token",
}
PLANS_DIRECTORY = ".secrets/opentofu/plans"
PROMPT = "to apply exactly this plan"

FAKE_TOFU = f"""#!{sys.executable}
import json, os, signal, sys, time
from pathlib import Path

here = Path(__file__).parent
config = json.loads((here / "fake.json").read_text())
command = sys.argv[1]
with (here / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps({{
        "argv": sys.argv[1:],
        "env": dict(os.environ),
        "plan_file_existed": Path(sys.argv[-1]).exists(),
        "tmpdir_mode": os.stat(os.environ["TMPDIR"]).st_mode & 0o777,
    }}) + "\\n")
plan_file = next((a[5:] for a in sys.argv[1:] if a.startswith("-out=")), None)
if plan_file:
    Path(plan_file).write_text("encrypted plan")
if command == config["hang_in"]:
    def stop_gracefully(*_):
        with (here / "sigints").open("a") as received:
            received.write("x")
        while config["drain_until_released"] and not (here / "released").exists():
            time.sleep(0.05)
        (here / "stopped gracefully").write_text("stopped")
        print("tofu stopped gracefully", flush=True)
        sys.exit(130)
    signal.signal(signal.SIGINT, stop_gracefully)
    with open(here / "applying", "w") as started:
        started.write("applying")
    time.sleep(60)
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
        hang_in=None,
        drain_until_released=False,
        **exit_codes,
    ) -> None:
        config = {
            "version": version or tofu.pinned_version(),
            "echo_environment": echo_environment,
            "leave_undeletable": leave_undeletable,
            "locked_name": locked_name,
            "hang_in": hang_in,
            "drain_until_released": drain_until_released,
            "exit_codes": exit_codes,
        }
        (self.directory / "fake.json").write_text(json.dumps(config))

    def calls(self) -> list[dict]:
        log = self.directory / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    @contextmanager
    def watch_hang(self) -> Iterator[Callable[[], None]]:
        """Yield a wait that returns once the hanging tofu run has installed its SIGINT handler."""
        descriptor = os.open(self.directory / "applying", os.O_RDONLY | os.O_NONBLOCK)

        def wait_until_hanging() -> None:
            ready, _, _ = select.select([descriptor], [], [], 20)
            assert ready, "hanging tofu command never started"

        try:
            yield wait_until_hanging
        finally:
            os.close(descriptor)

    @property
    def sigints_received(self) -> int:
        received = self.directory / "sigints"
        return len(received.read_text()) if received.exists() else 0

    def release_draining_apply(self) -> None:
        (self.directory / "released").write_text("go")

    @property
    def stopped_gracefully(self) -> bool:
        return (self.directory / "stopped gracefully").exists()

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
    for relative, value in WRITE_TOKEN_FILES.items():
        (home / relative).write_text(value + "\n")
        (home / relative).chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def fake_tofu(tmp_path, monkeypatch) -> FakeTofu:
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / "tofu"
    executable.write_text(FAKE_TOFU)
    executable.chmod(0o755)
    os.mkfifo(directory / "applying")
    monkeypatch.setenv("PATH", f"{directory}:/usr/bin:/bin")
    fake = FakeTofu(directory)
    fake.configure()
    return fake


def plain_git(tree: Path, *arguments: str) -> None:
    identity = ["-c", "user.name=edge test", "-c", "user.email=test@edge.invalid"]
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    subprocess.run(
        ["git", "-C", str(tree), *identity, "-c", "core.hooksPath=/dev/null", *arguments],
        check=True, capture_output=True, stdin=subprocess.DEVNULL, env=environment,
    )  # fmt: skip


@pytest.fixture
def live_checkout(home) -> Path:
    """`~/git/edge` as the operator has it: the main worktree, a clean `main` with one commit."""
    tree = home / "git" / "edge"
    (tree / "external").mkdir(parents=True)
    (tree / "external" / "main.tf").write_text("# catalog\n")
    (tree / ".gitignore").write_text((PROJECT_ROOT / ".gitignore").read_text())
    (tree / "fleet.yaml").write_text((PROJECT_ROOT / "fleet.yaml").read_text())
    plain_git(tree, "init", "--quiet", "--initial-branch", "main")
    plain_git(tree, "add", "--all")
    plain_git(tree, "commit", "--quiet", "--message", "init")
    return tree


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
    assert "check" in capsys.readouterr().out
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


@dataclass
class Terminal:
    """`edge external apply` as a child on a pty, the way the operator runs it."""

    process: subprocess.Popen
    master: int
    output: str = ""

    def read_until(self, text: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while text not in self.output:
            assert time.monotonic() < deadline, f"never saw {text!r} in {self.output!r}"
            if select.select([self.master], [], [], 0.2)[0]:
                self.output += self._read()

    def _read(self) -> str:
        try:
            return os.read(self.master, 65536).decode(errors="replace")
        except OSError:
            return ""

    def type(self, text: str) -> None:
        os.write(self.master, text.encode())

    def signal(self, number: int) -> None:
        self.process.send_signal(number)

    def finish(self) -> int:
        code = self.process.wait(timeout=20)
        while chunk := self._read():
            self.output += chunk
        return code


@pytest.fixture
def terminal(home, fake_tofu, live_checkout):
    """Start `command` on a pty with a minimal environment and no session bus."""
    started = []

    def start(
        *, command="apply", stdin_is_terminal=True, stdout_is_terminal=True, tree=None
    ) -> Terminal:
        """`tree` is the checkout whose code runs; the live checkout unless a test says another."""
        master, slave = pty.openpty()
        program = (
            "import sys; from pathlib import Path; from edge import cli; "
            "from edge.external import tofu; "
            f"tofu.PROJECT_ROOT = Path({str(tree or live_checkout)!r}); "
            "sys.exit(cli.main(sys.argv[1:]))"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", program, "external", command],
            stdin=slave if stdin_is_terminal else subprocess.DEVNULL,
            stdout=slave if stdout_is_terminal else subprocess.DEVNULL,
            stderr=slave,
            env={
                "HOME": str(home),
                "PATH": f"{fake_tofu.directory}:/usr/bin:/bin",
                "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
            },
            start_new_session=True,
        )
        os.close(slave)
        started.append(process)
        return Terminal(process, master)

    yield start
    for process in started:
        process.kill()
        process.wait()


def plan_files(home) -> list[Path]:
    return list((home / PLANS_DIRECTORY).glob("*"))


def test_apply_without_a_terminal_exits_1_before_any_tofu_call(home, fake_tofu, capsys):
    """#45 rule 2: stdin or stdout that is no terminal refuses before tofu."""
    assert cli.main(["external", "apply"]) == 1
    assert "terminal" in capsys.readouterr().err
    assert fake_tofu.calls() == []


def a_linked_worktree_as_the_live_checkout(live_checkout, tmp_path) -> Path:
    main_worktree = tmp_path / "main-worktree"
    live_checkout.rename(main_worktree)
    plain_git(main_worktree, "worktree", "add", "--quiet", "--detach", str(live_checkout))
    return live_checkout


def another_copy(live_checkout, tmp_path) -> Path:
    copy = tmp_path / "copy"
    plain_git(tmp_path, "clone", "--quiet", str(live_checkout), str(copy))
    return copy


def on_a_branch(live_checkout, _tmp_path) -> Path:
    plain_git(live_checkout, "switch", "--quiet", "--create", "feature/x")
    return live_checkout


def detached(live_checkout, _tmp_path) -> Path:
    plain_git(live_checkout, "switch", "--quiet", "--detach")
    return live_checkout


def modified(live_checkout, _tmp_path) -> Path:
    (live_checkout / "external" / "main.tf").write_text("# edited\n")
    return live_checkout


def with_a_stray_tfvars(live_checkout, _tmp_path) -> Path:
    (live_checkout / "external" / "x.auto.tfvars").write_text("zone = 1\n")
    return live_checkout


REFUSAL = "edge external apply: "
LIVE_ONLY = f"{REFUSAL}runs only from the live checkout ~/git/edge ("
MAIN_ONLY = f"{REFUSAL}runs only on main ("


@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        pytest.param(a_linked_worktree_as_the_live_checkout,
                     f"{LIVE_ONLY}a linked worktree, not the main checkout)", id="E1-linked"),
        pytest.param(another_copy, f"{LIVE_ONLY}this is ", id="E2-another-copy"),
        pytest.param(on_a_branch, f"{MAIN_ONLY}this is branch feature/x)", id="E3-branch"),
        pytest.param(detached, f"{MAIN_ONLY}HEAD is detached at ", id="E3-detached"),
        pytest.param(modified, f"{REFUSAL}the live checkout has local changes",
                     id="E4-modified-file"),
        pytest.param(modified, f"{REFUSAL}external/main.tf", id="E4-the-path-on-its-own-line"),
        pytest.param(with_a_stray_tfvars, f"{REFUSAL}external/x.auto.tfvars",
                     id="E5-untracked-tfvars"),
    ],
)  # fmt: skip
def test_apply_outside_a_clean_main_of_the_live_checkout_exits_2_before_any_token_or_tofu(
    home, fake_tofu, terminal, live_checkout, tmp_path, arrange, reason
):
    """#124 rule 2: the reason is named, no token file is read (a missing one is not named)."""
    tree = arrange(live_checkout, tmp_path)
    (home / next(iter(WRITE_TOKEN_FILES))).unlink()
    apply = terminal(tree=tree)
    assert apply.finish() == 2
    assert reason in " ".join(apply.output.split())
    assert str(home / ".secrets") not in apply.output
    assert fake_tofu.calls() == []


def test_apply_from_a_clean_main_ignores_ignored_files(home, fake_tofu, terminal, live_checkout):
    """#124 rule 3, E6: `.terraform/` and a `*.tfplan` are no change."""
    (live_checkout / "external" / ".terraform").mkdir()
    (live_checkout / "external" / ".terraform" / "x").touch()
    (live_checkout / "external" / "x.tfplan").touch()
    fake_tofu.configure(plan=0)
    assert terminal().finish() == 0


def test_apply_with_a_failing_git_exits_2_saying_it_cannot_read_the_checkout(
    home, fake_tofu, terminal
):
    """#124 rule 2, E11."""
    broken_git = fake_tofu.directory / "git"
    broken_git.write_text("#!/bin/sh\nexit 128\n")
    broken_git.chmod(0o755)
    apply = terminal()
    assert apply.finish() == 2
    assert "edge external apply: cannot read the live checkout, " in apply.output
    assert fake_tofu.calls() == []


def git_exiting_128(tree, arguments, profile):
    return subprocess.CompletedProcess(arguments, 128, b"", b"fatal: broken\n")


@pytest.mark.parametrize(
    ("arrange", "git", "exit_code", "stderr_contains"),
    [
        pytest.param(a_linked_worktree_as_the_live_checkout, checkout.run_git, 2,
                     f"{LIVE_ONLY}a linked worktree, not the main checkout)", id="E1-linked"),
        pytest.param(on_a_branch, checkout.run_git, 2,
                     f"{MAIN_ONLY}this is branch feature/x)", id="E3-branch"),
        pytest.param(lambda live, _tmp: live, git_exiting_128, 2,
                     f"{REFUSAL}cannot read the live checkout, ", id="E11-failing-git"),
        pytest.param(lambda live, _tmp: live, checkout.run_git, 42, "", id="E7-clean-main"),
    ],
)  # fmt: skip
def test_apply_command_refuses_off_a_clean_live_main_in_process(
    home,
    fake_tofu,
    capsys,
    monkeypatch,
    live_checkout,
    tmp_path,
    arrange,
    git,
    exit_code,
    stderr_contains,
):
    """#124 rule 2 in process: the terminal refusal is lifted, the tree decides, `git=` is used."""
    monkeypatch.setattr(external_apply, "refusal_without_terminal", lambda: None)
    monkeypatch.setattr(external_apply, "apply_plan", lambda _home, _tokens: 42)
    tree = arrange(live_checkout, tmp_path)
    assert external_cli.apply_command(home, tree, git) == exit_code
    error = " ".join(capsys.readouterr().err.split())
    assert stderr_contains in error
    assert (error == "") == (exit_code == 42)


def test_apply_without_a_terminal_from_a_wrong_tree_exits_1_first(
    home, fake_tofu, capsys, live_checkout, tmp_path
):
    """#124 E9: the terminal refusal comes before the tree is asked."""
    assert external_cli.apply_command(home, another_copy(live_checkout, tmp_path)) == 1
    assert "terminal" in capsys.readouterr().err


def test_plan_runs_from_any_checkout(home, fake_tofu, live_checkout, tmp_path, monkeypatch):
    """#124 rule 4, E8: plan asks nothing of the tree it runs from."""
    monkeypatch.setattr(tofu, "PROJECT_ROOT", another_copy(live_checkout, tmp_path))
    assert edge_external_plan() == 0


INVALID_CATALOG = """\
cloudflare: {zone: edge-fixture.invalid}
services:
  songmaker:
    hosts:
      - {name: www, exposure: private}
"""
CATALOG_LINE = "services.songmaker.hosts[0].exposure: must be one of public, bypass, protected"


def commit_catalog(tree: Path, text: str) -> None:
    (tree / "fleet.yaml").write_text(text)
    plain_git(tree, "commit", "--quiet", "--all", "--message", "catalog")


def without_tokens(home) -> None:
    for token_file in TOKEN_FILES | WRITE_TOKEN_FILES:
        (home / token_file).unlink()


def test_plan_with_an_invalid_catalog_exits_2_before_any_token_or_tofu(
    home, fake_tofu, live_checkout, monkeypatch, capsys
):
    """#131 P1: the pre-flight names the key; no token file is read and tofu never starts."""
    commit_catalog(live_checkout, INVALID_CATALOG)
    monkeypatch.setattr(tofu, "PROJECT_ROOT", live_checkout)
    without_tokens(home)
    assert edge_external_plan() == 2
    assert capsys.readouterr().err == f"edge external plan: {CATALOG_LINE}\n"
    assert fake_tofu.calls() == []


def test_apply_with_an_invalid_catalog_exits_2_before_any_token_or_tofu(
    home, fake_tofu, live_checkout, terminal
):
    """#131 P1: after the terminal and live-main refusals, before the tokens."""
    commit_catalog(live_checkout, INVALID_CATALOG)
    without_tokens(home)
    apply = terminal()
    assert apply.finish() == 2
    assert f"edge external apply: {CATALOG_LINE}" in apply.output
    assert fake_tofu.calls() == []


def test_apply_without_a_terminal_refuses_before_the_catalog_is_read(
    home, fake_tofu, live_checkout, monkeypatch, capsys
):
    """#131 P2: the terminal refusal still comes first."""
    commit_catalog(live_checkout, INVALID_CATALOG)
    monkeypatch.setattr(tofu, "PROJECT_ROOT", live_checkout)
    assert cli.main(["external", "apply"]) == 1
    assert "terminal" in capsys.readouterr().err


def test_apply_without_a_terminal_exits_1_even_when_the_write_tokens_are_missing(
    home, fake_tofu, capsys
):
    """#45 rule 2: the refusal comes before any token file is read."""
    for write_file in WRITE_TOKEN_FILES:
        (home / write_file).unlink()
    assert cli.main(["external", "apply"]) == 1
    assert "terminal" in capsys.readouterr().err
    assert fake_tofu.calls() == []


@pytest.mark.parametrize(
    "terminals",
    [
        pytest.param({"stdout_is_terminal": False}, id="stdin-terminal-stdout-redirected"),
        pytest.param({"stdin_is_terminal": False}, id="stdin-redirected-stdout-terminal"),
    ],
)
def test_apply_with_only_one_of_stdin_and_stdout_a_terminal_exits_1_before_tofu(
    home, fake_tofu, terminal, terminals
):
    """#45 rule 2: stdin and stdout must both be terminals."""
    apply = terminal(**terminals)
    assert apply.finish() == 1
    assert "must be a terminal" in apply.output
    assert fake_tofu.calls() == []


def test_apply_reads_the_write_tokens_through_the_allowlist(home, fake_tofu, terminal):
    """#45 rule 1: apply uses fleet-write and fleet-admin, same environment as plan."""
    fake_tofu.configure(plan=2, echo_environment=True)
    apply = terminal()
    apply.read_until(PROMPT)
    apply.type("yes\n")
    assert apply.finish() == 0
    environment = fake_tofu.call("apply")["env"]
    assert set(environment) == ALLOWED_NAMES
    assert environment["CLOUDFLARE_API_TOKEN"] == "cloudflare-write-token"
    assert environment["GITHUB_TOKEN"] == "github-admin-token"
    assert "write-token" not in apply.output
    assert "admin-token" not in apply.output


@pytest.mark.parametrize("write_file", WRITE_TOKEN_FILES)
def test_a_missing_write_token_exits_2_naming_it_without_running_tofu(
    home, fake_tofu, terminal, write_file
):
    """#45 rule 1: a missing write file stops apply before tofu; the read file is no stand-in."""
    (home / write_file).unlink()
    apply = terminal()
    assert apply.finish() == 2
    assert str(home / write_file) in apply.output
    assert fake_tofu.calls() == []


@pytest.mark.parametrize(
    "typed,expected_exit,applied",
    [
        pytest.param("yes\n", 0, True, id="yes"),
        pytest.param("y\n", 1, False, id="y"),
        pytest.param("Yes\n", 1, False, id="Yes"),
        pytest.param("\n", 1, False, id="empty"),
        pytest.param(" yes\n", 1, False, id="padded"),
        pytest.param("\x04", 1, False, id="end-of-input"),
    ],
)
def test_only_the_exact_yes_applies(home, fake_tofu, terminal, typed, expected_exit, applied):
    """#45 rule 2: only the exact input `yes` proceeds; anything else aborts with exit 1."""
    fake_tofu.configure(plan=2)
    apply = terminal()
    apply.read_until(PROMPT)
    apply.type(typed)
    assert apply.finish() == expected_exit
    assert ("apply" in [call["argv"][0] for call in fake_tofu.calls()]) is applied


def test_a_plan_without_changes_exits_0_without_asking(home, fake_tofu, terminal):
    """#45 rule 2: nothing to apply is exit 0 and no prompt."""
    apply = terminal()
    assert apply.finish() == 0
    assert PROMPT not in apply.output
    assert "apply" not in [call["argv"][0] for call in fake_tofu.calls()]


def test_apply_applies_exactly_the_encrypted_plan_file_in_the_private_directory(
    home, fake_tofu, terminal
):
    """#45 rules 2 and 3: the shown plan file is in the 0700 plans directory and is what applies."""
    fake_tofu.configure(plan=2)
    apply = terminal()
    apply.read_until(PROMPT)
    assert (home / PLANS_DIRECTORY).stat().st_mode & 0o777 == 0o700
    apply.type("yes\n")
    assert apply.finish() == 0
    out_argument = next(a for a in fake_tofu.call("plan")["argv"] if a.startswith("-out="))
    plan_file = Path(out_argument.removeprefix("-out="))
    assert plan_file.parent == home / PLANS_DIRECTORY
    assert fake_tofu.call("apply")["argv"] == ["apply", str(plan_file)]
    assert fake_tofu.call("apply")["plan_file_existed"]


CLEANUP_PATHS = [
    pytest.param({"plan": 2}, "yes\n", 0, id="success"),
    pytest.param({"plan": 2}, "no\n", 1, id="abort"),
    pytest.param({"plan": 1}, None, 2, id="plan-failure"),
    pytest.param({"plan": 2, "apply": 1}, "yes\n", 1, id="apply-failure"),
    pytest.param({"plan": 2}, signal.SIGINT, 1, id="sigint-at-prompt"),
    pytest.param({"plan": 2}, signal.SIGTERM, 1, id="sigterm-at-prompt"),
    pytest.param({"plan": 2, "hang_in": "apply"}, signal.SIGINT, 1, id="sigint-in-apply"),
    pytest.param({"plan": 2, "hang_in": "apply"}, signal.SIGTERM, 1, id="sigterm-in-apply"),
]


@pytest.mark.parametrize("configuration,reaction,expected_exit", CLEANUP_PATHS)
def test_the_plan_file_is_deleted_on_every_path(
    home, fake_tofu, terminal, configuration, reaction, expected_exit
):
    """#45 rule 4: success, abort, failures and signals leave the plans directory empty."""
    fake_tofu.configure(**configuration)
    apply = terminal()
    with fake_tofu.watch_hang() as wait_until_hanging:
        if reaction is not None:
            apply.read_until(PROMPT)
            if configuration.get("hang_in") == "apply":
                apply.type("yes\n")
                wait_until_hanging()
            if isinstance(reaction, signal.Signals):
                apply.signal(reaction)
            else:
                apply.type(reaction)
        assert apply.finish() == expected_exit
    assert plan_files(home) == []
    assert list((home / ".secrets/opentofu/tmp").iterdir()) == []


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_an_interrupt_during_plan_exits_2_names_the_step_and_shows_tofus_output(
    home, fake_tofu, terminal, interrupt
):
    """#40 rule 4 / #45: an interrupted plan is `cannot plan` (2), never `changes pending` (1)."""
    fake_tofu.configure(hang_in="plan")
    plan = terminal(command="plan")
    with fake_tofu.watch_hang() as wait_until_hanging:
        wait_until_hanging()
        plan.signal(interrupt)
        assert plan.finish() == 2
    assert fake_tofu.stopped_gracefully
    assert fake_tofu.sigints_received == 1
    assert "tofu stopped gracefully" in plan.output
    assert "edge external plan: interrupted during the plan" in plan.output
    assert "Traceback" not in plan.output


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_an_interrupt_during_apply_lets_tofu_stop_gracefully_and_names_the_step(
    home, fake_tofu, terminal, interrupt
):
    """#45 rule 4/5: SIGINT or SIGTERM reaches tofu as one SIGINT and is awaited, not killed."""
    fake_tofu.configure(plan=2, hang_in="apply")
    apply = terminal()
    with fake_tofu.watch_hang() as wait_until_hanging:
        apply.read_until(PROMPT)
        apply.type("yes\n")
        wait_until_hanging()
        apply.signal(interrupt)
        assert apply.finish() == 1
    assert fake_tofu.stopped_gracefully
    assert "tofu stopped gracefully" in apply.output
    assert "interrupted during tofu apply, live services may be partly changed" in apply.output


def test_further_interrupts_while_tofu_drains_are_not_forwarded_to_it(home, fake_tofu, terminal):
    """#45 rule 4/5: later interrupts while tofu drains are not forwarded as further SIGINTs."""
    fake_tofu.configure(plan=2, hang_in="apply", drain_until_released=True)
    apply = terminal()
    with fake_tofu.watch_hang() as wait_until_hanging:
        apply.read_until(PROMPT)
        apply.type("yes\n")
        wait_until_hanging()
        apply.signal(signal.SIGINT)
        deadline = time.monotonic() + 20
        while fake_tofu.sigints_received < 1:
            assert time.monotonic() < deadline, "tofu never received the forwarded SIGINT"
        apply.signal(signal.SIGINT)
        apply.signal(signal.SIGTERM)
        assert apply.process.poll() is None
        fake_tofu.release_draining_apply()
        assert apply.finish() == 1
    assert fake_tofu.sigints_received == 1
    assert fake_tofu.stopped_gracefully


def test_a_second_plan_command_keeps_the_plan_file_of_an_apply_awaiting_confirmation(
    home, fake_tofu, terminal
):
    """#45 rule 4: only leftovers of killed runs are deleted, not a live run's plan."""
    fake_tofu.configure(plan=2)
    apply = terminal()
    apply.read_until(PROMPT)
    edge_external_plan()
    apply.type("yes\n")
    assert apply.finish() == 0
    assert fake_tofu.call("apply")["plan_file_existed"]


def test_a_tofu_that_vanishes_after_the_confirmation_fails_the_apply_step_with_exit_1(
    home, fake_tofu, terminal
):
    """#45 rule 5: a failure after a good plan is exit 1 naming the step, not 'cannot plan'."""
    fake_tofu.configure(plan=2)
    apply = terminal()
    apply.read_until(PROMPT)
    (fake_tofu.directory / "tofu").unlink()
    apply.type("yes\n")
    assert apply.finish() == 1
    assert "tofu apply failed" in apply.output
    assert "cannot plan" not in apply.output
    assert plan_files(home) == []


def leave_a_killed_run_behind(home) -> Path:
    leftover = home / PLANS_DIRECTORY / "killed.tfplan"
    leftover.parent.mkdir(mode=0o700)
    leftover.write_text("encrypted plan")
    leftover.with_suffix(".lock").write_text("")
    return leftover


def test_a_leftover_plan_file_is_deleted_and_named_at_the_next_plan(home, fake_tofu, capsys):
    """#45 rule 4: a plan file left by a killed run is removed and named at the next start."""
    leftover = leave_a_killed_run_behind(home)
    cli.main(["external", "plan"])
    assert plan_files(home) == []
    assert f"removed the leftover plan file {leftover}" in capsys.readouterr().err


def test_a_leftover_plan_file_is_deleted_and_named_at_the_next_apply(home, fake_tofu, terminal):
    """#45 rule 4: the same sweep runs at the start of apply, naming only the plan file."""
    leftover = leave_a_killed_run_behind(home)
    apply = terminal()
    assert apply.finish() == 0
    assert plan_files(home) == []
    assert f"removed the leftover plan file {leftover}" in apply.output
    assert ".lock" not in apply.output


def test_a_non_terminal_apply_still_deletes_and_names_a_leftover_before_refusing(
    home, fake_tofu, capsys
):
    """#45 rule 4: the refusal comes after the sweep, so a killed run's plan is not kept."""
    leftover = leave_a_killed_run_behind(home)
    assert cli.main(["external", "apply"]) == 1
    error = capsys.readouterr().err
    assert plan_files(home) == []
    assert f"removed the leftover plan file {leftover}" in error
    assert "terminal" in error
    assert fake_tofu.calls() == []


def test_a_plan_file_whose_lock_is_gone_is_a_leftover(home, capsys):
    """#45 rule 4: a live run holds its lock until the plan is deleted, so a bare plan is dead."""
    leftover = leave_a_killed_run_behind(home)
    leftover.with_suffix(".lock").unlink()
    external_apply.remove_leftover_plans(home)
    assert plan_files(home) == []
    assert f"removed the leftover plan file {leftover}" in capsys.readouterr().err


def test_a_sweep_keeps_the_files_of_a_live_run_that_replaced_the_lock_it_opened(home):
    """#45 rule 4: the sweep deletes only what the lock it holds still names."""
    directory = home / PLANS_DIRECTORY
    directory.mkdir(mode=0o700)
    stale_lock = directory / "run.lock"
    stale_lock.write_text("")
    live_plan = directory / "run.tfplan"

    def replace_the_lock_then_take_it(descriptor):
        stale_lock.unlink()
        stale_lock.write_text("live run's replacement")
        live_plan.write_text("live plan")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    external_apply.remove_leftover_plans(home, take_lock=replace_the_lock_then_take_it)
    assert stale_lock.exists()
    assert live_plan.exists()


def test_a_sweep_keeps_the_plan_of_a_live_run_that_takes_over_a_bare_plans_stem(home):
    """#45 rule 4: the lock the sweep creates for a bare plan does not shield a replaced stem."""
    directory = home / PLANS_DIRECTORY
    directory.mkdir(mode=0o700)
    live_plan = directory / "run.tfplan"
    live_plan.write_text("dead plan")
    lock = directory / "run.lock"

    def let_a_live_run_replace_the_stem_then_take_the_lock(descriptor):
        lock.unlink()
        lock.write_text("live run's lock")
        live_plan.write_text("live plan")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    external_apply.remove_leftover_plans(
        home, take_lock=let_a_live_run_replace_the_stem_then_take_the_lock
    )
    assert live_plan.read_text() == "live plan"
    assert lock.exists()


@pytest.mark.parametrize(
    "plan_exit,typed",
    [pytest.param(0, None, id="no-changes"), pytest.param(2, "no\n", id="abort")],
)
def test_a_failed_cleanup_after_no_changes_or_an_abort_exits_1_naming_the_cleanup(
    home, fake_tofu, terminal, plan_exit, typed
):
    """#45 rule 5: a cleanup that fails after a good plan is exit 1 naming it, not 'cannot plan'."""
    fake_tofu.configure(plan=plan_exit, leave_undeletable=True)
    apply = terminal()
    try:
        if typed is not None:
            apply.read_until(PROMPT)
            apply.type(typed)
        assert apply.finish() == 1
        assert "the cleanup failed" in apply.output
        assert "cannot plan" not in apply.output
    finally:
        for locked in (home / ".secrets/opentofu/tmp").glob("*/locked"):
            locked.chmod(0o700)


@pytest.mark.parametrize(
    "plan_exit,typed,live_services_may_have_changed",
    [
        pytest.param(0, None, False, id="no-changes"),
        pytest.param(2, "no", False, id="abort"),
        pytest.param(1, None, False, id="failed-plan"),
        pytest.param(2, "yes", True, id="after-apply"),
    ],
)
def test_an_interrupted_cleanup_exits_1_and_warns_of_live_changes_only_after_apply(
    home, fake_tofu, capsys, monkeypatch, plan_exit, typed, live_services_may_have_changed
):
    """#45 rule 5: the interrupt consequence follows whether apply was reached, not the step."""
    fake_tofu.configure(plan=plan_exit)

    @contextmanager
    def interrupted_when_leaving(home_directory):
        with external_apply.private_plan_file(home_directory) as plan_file:
            yield plan_file
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", lambda _prompt: typed)
    assert external_apply.apply_plan(home, {}, plan_file_scope=interrupted_when_leaving) == 1
    error = capsys.readouterr().err
    assert "interrupted during the cleanup" in error
    assert ("live services may be partly changed" in error) is live_services_may_have_changed


def test_a_failed_cleanup_after_a_clean_body_is_raised_even_inside_an_except_block(home):
    """#34 finding 11 (a): the in-flight exception is the body's own, not an outer handled one."""

    def leave_with_a_locked_directory():
        with secrets.private_temporary_directory(home) as directory:
            locked = directory / "locked"
            locked.mkdir()
            (locked / "file").write_text("x")
            locked.chmod(0o500)

    try:
        try:
            raise ValueError("handled elsewhere")
        except ValueError:
            with pytest.raises(PermissionError):
                leave_with_a_locked_directory()
    finally:
        for locked in (home / ".secrets/opentofu/tmp").glob("*/locked"):
            locked.chmod(0o700)


def test_the_changes_pending_line_follows_the_plan_without_a_blank_line(home, fake_tofu, capsys):
    """#34 finding 11 (c): the plan output ends in one newline, then the line."""
    fake_tofu.configure(plan=2, echo_environment=True)
    edge_external_plan()
    assert "\n\n" not in capsys.readouterr().out


HOSTS_CATALOG = """\
cloudflare: {zone: example.test}
services:
  site:
    hosts:
      - {name: "@", exposure: public, reason: the site}
"""
TUNNEL_TARGET = "t-1.cfargotunnel.com"
CLEAN_SNAPSHOT = cloudflare_read.Snapshot(
    "t-1", (cloudflare_read.Record("example.test", "CNAME", TUNNEL_TARGET, True),), (), ()
)
STRAY_SNAPSHOT = cloudflare_read.Snapshot(
    "t-1",
    (
        cloudflare_read.Record("example.test", "A", "192.0.2.1", True),
        cloudflare_read.Record("stray.example.test", "CNAME", TUNNEL_TARGET, True),
    ),
    (),
    (),
)


class FakeReader:
    """The injected port: answers a snapshot, raises an exception, or runs a callable first."""

    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.tokens: list[str] = []
        self.reads = 0

    def __call__(self, token: str):
        self.tokens.append(token)
        return self

    def read(self, _zone: str):
        self.reads += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            self.outcome()
            return CLEAN_SNAPSHOT
        return self.outcome


@pytest.fixture
def hosts_catalog(tmp_path, monkeypatch) -> Path:
    """A checkout that is not the live main, declaring one public apex."""
    tree = tmp_path / "elsewhere"
    tree.mkdir()
    (tree / "fleet.yaml").write_text(HOSTS_CATALOG)
    monkeypatch.setattr(tofu, "PROJECT_ROOT", tree)
    return tree


def run_check(home, reader: FakeReader, *, as_json: bool = False) -> int:
    return external_cli.check_command(home, as_json=as_json, cloudflare=reader)


def test_a_clean_check_exits_0_and_says_how_many_hosts(home, hosts_catalog, capsys):
    """K1."""
    assert run_check(home, FakeReader(CLEAN_SNAPSHOT)) == 0
    assert capsys.readouterr().out == "no findings (1 hosts)\n"


def test_findings_exit_1_and_json_is_one_document_of_four_strings(home, hosts_catalog, capsys):
    """K1."""
    assert run_check(home, FakeReader(STRAY_SNAPSHOT), as_json=True) == 1
    printed = capsys.readouterr()
    findings = json.loads(printed.out)
    assert [item["problem"] for item in findings] == ["record-wrong", "undeclared-record"]
    assert all(set(item) == {"problem", "subject", "detail", "next"} for item in findings)
    assert all(isinstance(value, str) for item in findings for value in item.values())
    assert printed.err == ""


def test_check_without_hosts_exits_2_naming_the_flag_and_reads_nothing(home, capsys):
    """K2."""
    without_tokens(home)
    with pytest.raises(SystemExit) as refused:
        cli.main(["external", "check"])
    assert refused.value.code == 2
    assert "--hosts" in capsys.readouterr().err


def test_check_help_lists_hosts_and_json(capsys):
    """K2."""
    with pytest.raises(SystemExit) as shown:
        cli.main(["external", "check", "--help"])
    assert shown.value.code == 0
    assert {"--hosts", "--json"} <= set(capsys.readouterr().out.split())


def test_an_invalid_catalog_exits_2_with_the_pre_flight_line_before_any_token(
    home, hosts_catalog, capsys
):
    """K3."""
    (hosts_catalog / "fleet.yaml").write_text(INVALID_CATALOG)
    without_tokens(home)
    reader = FakeReader(CLEAN_SNAPSHOT)
    assert run_check(home, reader) == 2
    assert capsys.readouterr().err == f"edge external check: {CATALOG_LINE}\n"
    assert reader.reads == 0


def test_check_reads_only_the_cloudflare_read_file(home, hosts_catalog):
    """K4."""
    (home / ".secrets/github/fleet-read").unlink()
    (home / ".secrets/opentofu/state-passphrase").unlink()
    reader = FakeReader(CLEAN_SNAPSHOT)
    assert run_check(home, reader) == 0
    assert reader.tokens == ["cloudflare-token"]


def test_a_missing_cloudflare_file_exits_2_naming_it(home, hosts_catalog, capsys):
    """K4."""
    cloudflare_file = home / ".secrets/cloudflare/fleet-read"
    cloudflare_file.unlink()
    reader = FakeReader(CLEAN_SNAPSHOT)
    assert run_check(home, reader) == 2
    assert str(cloudflare_file) in capsys.readouterr().err
    assert reader.reads == 0


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(cloudflare_read.CloudflareReadError(f"/zones: {SENTINEL}"), id="a read error"),
        pytest.param(OSError(5, SENTINEL), id="an OS error"),
    ],
)
def test_a_reader_failure_names_a_cause_without_the_token_and_leaves_stdout_empty(
    home, hosts_catalog, capsys, failure
):
    """K5: the token file holds the sentinel; no output line carries it."""
    (home / ".secrets/cloudflare/fleet-read").write_text(SENTINEL)
    assert run_check(home, FakeReader(failure), as_json=True) == 2
    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err.startswith("edge external check: cannot check, ")
    assert printed.err.count("\n") == 1
    assert SENTINEL not in printed.err


def test_check_starts_no_tofu_and_keeps_a_leftover_plan(home, fake_tofu, hosts_catalog):
    """K6: the sweep belongs to plan and apply."""
    leftover = leave_a_killed_run_behind(home)
    assert run_check(home, FakeReader(CLEAN_SNAPSHOT)) == 0
    assert fake_tofu.calls() == []
    assert leftover.exists()


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_an_interrupt_exits_130_with_stdout_empty(home, hosts_catalog, capsys, interrupt, as_json):
    """K7."""
    reader = FakeReader(lambda: os.kill(os.getpid(), interrupt))
    assert run_check(home, reader, as_json=as_json) == 130
    printed = capsys.readouterr()
    assert printed.out == ""
    assert "interrupted" in printed.err


def test_check_through_main_stops_at_a_missing_token_before_any_network(
    home, hosts_catalog, capsys
):
    """The wiring of `--hosts --json` to the command, ended before a real client is built."""
    (home / ".secrets/cloudflare/fleet-read").unlink()
    assert cli.main(["external", "check", "--hosts", "--json"]) == 2
    assert capsys.readouterr().out == ""
