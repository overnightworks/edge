from __future__ import annotations

import email.message
import email.policy
import json
import os
import stat
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from home_tools import backup_check, check, cli, logs_check, platform_check, secrets_view, system_step, tunnel_config
from home_tools.secrets_view import load_manifest
from home_tools.setup import QUALITY_SOURCE
from home_tools.tidy_home import Layout, run_command
from home_tools.tunnel_config import TUNNEL_ID_PLACEHOLDER

DAY_SECONDS = 24 * 60 * 60

# Path-shaped tokens make these entries known, so the state `home` writes never becomes a finding itself.
HOME_README = "`.local/state`, `.config/home`, `.secrets` and `git/` belong here.\n"

# Every planted secret file holds this; no output and no report may ever contain any of it.
FAKE_SECRET_VALUE = "FAKE-SECRET-VALUE ghp_0000notarealtoken glpat-0000notarealtoken"


# The one `docker inspect --format` the platform check may send: named fields, and the compose project label.
# A fake docker answers any other format with a line that carries the canary, so a wider projection shows in the report.
ALLOWED_PLATFORM_INSPECT_FORMAT = (
    '{{.Name}}\t{{.State.Status}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}\t{{.State.ExitCode}}'
    '\t{{index .Config.Labels "com.docker.compose.project"}}'
)
INSPECT_CANARY = "CANARY-ENV-VALUE-s3cr3t"
CANARY_INSPECT_LINE = f"/{INSPECT_CANARY}\trunning\tunhealthy\t0\tedge\n"


@pytest.fixture
def berlin_time(monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def symlinked_folder_fix(place: str) -> str:
    """The command `home secrets` names for a symlink on the way to a secret: the folder it points to, or a new one, takes its place."""
    real_folder = f'if [ -e "$t" ]; then mv -T "$t" {place}; else mkdir {place}; fi'
    return f"t=$(readlink -m {place}) && rm {place} && {real_folder} && chmod 700 {place}"


# A small manifest of three kinds of place: one file, a wildcard plus a file, and a verified tool login.
TEST_TUNNEL_ID = "0000-test-tunnel"
TEST_MANIFEST = """\
[[secret]]
name = "gitlab"
kind = "token"
paths = ["~/.secrets/gitlab"]
read_by = [".bashrc"]
recreate = "https://gitlab.example/-/user_settings/personal_access_tokens"

[[secret]]
name = "cloudflared"
kind = "tool-login"
paths = ["~/.secrets/cloudflared/*.json", "~/.secrets/cloudflared/cert.pem"]
read_by = ["cloudflared.service", ".bashrc"]
recreate = "`cloudflared tunnel login`"

[[secret]]
name = "gh"
kind = "tool-login"
paths = ["~/.config/gh"]
read_by = ["gh"]
recreate = "`gh auth login --web`"
verify = "gh auth status"
"""


@pytest.fixture(autouse=True)
def empty_log_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The logs check looks at /var/log, which this machine fills; the tests look at an empty directory."""
    monkeypatch.setattr(check, "LOG_DIRECTORY", tmp_path / "var-log")


class FakeDisk:
    """The disk the platform check reads; the free share is the scenario, never this machine's."""

    TOTAL = 1000 * platform_check.BYTES_PER_GIGABYTE

    def __init__(self) -> None:
        self.free_share = 0.5
        self.unreadable = False
        self.asked: list[Path] = []

    def __call__(self, path: Path) -> platform_check.DiskSpace:
        self.asked.append(path)
        if self.unreadable:
            raise OSError("no disk")
        return platform_check.DiskSpace(self.TOTAL, round(self.TOTAL * self.free_share))


@pytest.fixture(autouse=True)
def fake_disk(monkeypatch: pytest.MonkeyPatch) -> FakeDisk:
    """A runner's disk may be over 90 % full, and a nearly full disk is an action: every `home check` test reads a roomy fake."""
    disk = FakeDisk()
    monkeypatch.setattr(check, "DISK_SPACE", disk)
    return disk


@pytest.fixture(autouse=True)
def stand_in_tunnel_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The tunnel config at /etc names this machine's tunnel; the tests read a stand-in naming a test tunnel."""
    config = tmp_path_factory.mktemp("etc-cloudflared") / "config.yml"
    config.write_text(f"tunnel: {TEST_TUNNEL_ID}\n")
    monkeypatch.setattr(tunnel_config, "TUNNEL_CONFIG", config)
    return config


@pytest.fixture
def use_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Path]:
    """Points `home secrets` at a manifest written from the given text."""

    def use(text: str) -> Path:
        path = tmp_path / "secrets_manifest.toml"
        path.write_text(text)
        monkeypatch.setattr(secrets_view, "MANIFEST_PATH", path)
        return path

    return use


def plant_secrets(home: Path) -> None:
    """Every place the active manifest names for a live entry gets a fake secret, with the modes a tidy ~/.secrets keeps."""
    for entry in load_manifest(secrets_view.MANIFEST_PATH):
        for pattern in () if entry.retired else entry.paths:
            path = home / str(pattern).replace("*", "fake").replace(TUNNEL_ID_PLACEHOLDER, TEST_TUNNEL_ID)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(FAKE_SECRET_VALUE)
            path.chmod(0o600)
    secrets_dir = Layout(home).secrets_dir
    for directory in (secrets_dir, *(path for path in secrets_dir.rglob("*") if path.is_dir())):
        directory.chmod(0o700)


@pytest.fixture
def furnish_clean_home(use_manifest: Callable[[str], Path]) -> Callable[[Path], Path]:
    """Makes a home directory one with nothing to report: the README knows every entry and every secret of the test manifest is in place."""

    def furnish(home: Path) -> Path:
        use_manifest(TEST_MANIFEST)
        home.mkdir(exist_ok=True)
        (home / "README.md").write_text(HOME_README)
        plant_secrets(home)
        return home

    return furnish


@pytest.fixture
def clean_home(tmp_path: Path, furnish_clean_home: Callable[[Path], Path]) -> Path:
    return furnish_clean_home(tmp_path / "home")


@pytest.fixture
def leave_pasted_image() -> Callable[[Path, int], Path]:
    """A pasted screenshot `age_days` old; past the default week it is a pasted-images finding."""

    def leave(directory: Path, age_days: int = 30) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        image = directory / "Eingefügtes Bild.png"
        image.write_bytes(b"png")
        stamp = time.time() - age_days * DAY_SECONDS
        os.utime(image, (stamp, stamp))
        return image

    return leave


PASSWORD_CANARY = "CANARY-SMTP-PASSWORD-9f3"
SMTP_ENTRIES = """
[[secret]]
name = "smtp/password"
kind = "token"
paths = ["~/.secrets/smtp/password"]
read_by = ["home check"]
recreate = "r"

[[secret]]
name = "smtp/account.toml"
kind = "configuration"
paths = ["~/.secrets/smtp/account.toml"]
read_by = ["home check"]
recreate = "r"
"""
# Stands in for msmtp: records argv and stdin per call, runs --passwordeval through sh like msmtp does, exits as the `exit` file says.
STUB_MSMTP = """\
#!/bin/sh
call=$(mktemp -d "$STUB_MAIL_DIR/call.$(date +%s%N).XXXXXX")
for argument; do printf '%s\\n' "$argument"; done > "$call/argv"
cat > "$call/stdin"
for argument; do
    case "$argument" in --passwordeval=*) sh -c "${argument#--passwordeval=}" > "$call/password" ;; esac
done
exit "$(cat "$STUB_MAIL_DIR/exit" 2>/dev/null || echo 0)"
"""


@dataclass
class SentMail:
    arguments: list[str]
    stdin: str
    password: str


class MailStub:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def exit_with(self, code: int) -> None:
        (self.directory / "exit").write_text(str(code))

    @property
    def sent(self) -> list[SentMail]:
        calls = sorted(self.directory.glob("call.*"))
        return [SentMail((call / "argv").read_text().splitlines(), (call / "stdin").read_text(), (call / "password").read_text()) for call in calls]


def set_up_mail(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int = 587) -> MailStub:
    """An account and password under home, a stub msmtp first on PATH, and a manifest that knows both files."""
    directory = tmp_path / "mail-stub"
    directory.mkdir(exist_ok=True)
    stub = directory / "bin" / "msmtp"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text(STUB_MSMTP)
    stub.chmod(0o755)
    monkeypatch.setenv("STUB_MAIL_DIR", str(directory))
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")
    smtp = Layout(home).secrets_dir / "smtp"
    smtp.mkdir(parents=True, exist_ok=True)
    (smtp / "account.toml").write_text(f'host = "smtp.example.test"\nport = {port}\nuser = "home@example.test"\nto = "me@example.test"\n')
    (smtp / "password").write_text(PASSWORD_CANARY)
    for file in smtp.iterdir():
        file.chmod(0o600)
    smtp.chmod(0o700)
    manifest = secrets_view.MANIFEST_PATH
    assert tmp_path in manifest.parents, "set_up_mail edits the manifest; point it at a temporary copy first (use_manifest)"
    manifest.write_text(manifest.read_text() + SMTP_ENTRIES)
    return MailStub(directory)


class RecordingRunner:
    """Delivers notifications (systemd-run succeeds only if notify-send did), answers the graphical-session probe, fails every other tool as missing, records calls."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.notify_delivers = True
        self.session_active = True

    def __call__(self, argv: Sequence[str]) -> str | None:
        self.calls.append(list(argv))
        delivers = {"systemd-run": self.notify_delivers, "systemctl": self.session_active}
        return "" if delivers.get(argv[0], False) else None

    @property
    def notifications(self) -> list[str]:
        """The summary line of every notification sent."""
        return [call[-3] for call in self.notification_calls]

    @property
    def notification_bodies(self) -> list[str]:
        return [call[-2] for call in self.notification_calls]

    @property
    def notification_calls(self) -> list[list[str]]:
        """The detached waiter commands: [..., summary, body, report path]."""
        return [call for call in self.calls if call[0] == "systemd-run"]


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()


class RunnerWithRealGitAndQuality(RecordingRunner):
    """The hook check reads git and the stub quality, so these two run for real; every other tool stays as in RecordingRunner."""

    def __call__(self, argv: Sequence[str]) -> str | None:
        if Path(argv[0]).name in {"git", "quality"}:
            self.calls.append(list(argv))
            return run_command(argv)
        return super().__call__(argv)


class FakeProbe:
    """Answers every probe with one exit code (None: the tool hung or is missing) and remembers what it ran."""

    def __init__(self) -> None:
        self.code: int | None = 0
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> int | None:
        self.calls.append(tuple(argv))
        return self.code


@pytest.fixture
def probe() -> FakeProbe:
    return FakeProbe()


# What the stub scanner refuses; assembled from parts, as every fake key in these tests is.
FAKE_KEY = "FAKE" + "KEY-" + "0000"

STUB_QUALITY = """\
#!/bin/sh
case "$1" in
scan-staged)
    [ -n "$STUB_SCAN_EXIT" ] && exit "$STUB_SCAN_EXIT"
    if git diff --cached | grep -q "FAKE""KEY-"; then
        echo "quality: secret.txt fake-key sha256=0000 (Wert nicht gezeigt)"
        exit 1
    fi
    ;;
engines)
    case "$2" in
    install)
        [ -n "$STUB_ENGINES_FAIL" ] && exit 1
        mkdir -p "$3" && touch "$3/installed"
        ;;
    verify) [ -e "$3/installed" ] || exit 1 ;;
    esac
    ;;
esac
exit 0
"""

# The `git` value real uv 0.10.9 wrote to uv-receipt.toml after installing QUALITY_SOURCE's pinned SHA; only the revision follows the pin.
RECORDED_RECEIPT_GIT = f"https://github.com/overnightworks/quality?rev={QUALITY_SOURCE.rpartition('@')[2]}"

# Writes what `uv tool install` leaves behind for the requirement it is given: the tool's executable and uv's receipt for it.
# Ansible's executable is a dummy, since the sandbox hands the system step a FakePlaybook instead of running it.
STUB_UV = """\
#!/bin/sh
echo "$@" >> "$STUB_UV_LOG"
[ -n "$STUB_UV_FAIL" ] && exit 1
mkdir -p "$HOME/.local/bin"
case "$4" in
ansible-core==*)
    mkdir -p "$HOME/.local/share/uv/tools/ansible-core"
    printf '#!/bin/sh\\nexit 1\\n' > "$HOME/.local/bin/ansible-playbook" && chmod 755 "$HOME/.local/bin/ansible-playbook"
    printf '[tool]\\nrequirements = [{ name = "ansible-core", specifier = "==%s" }]\\n' "${4#ansible-core==}" \\
        > "$HOME/.local/share/uv/tools/ansible-core/uv-receipt.toml"
    ;;
*)
    mkdir -p "$HOME/.local/share/uv/tools/overnightworks-quality"
    cp "$STUB_QUALITY" "$HOME/.local/bin/quality" && chmod 755 "$HOME/.local/bin/quality"
    printf '[tool]\\nrequirements = [{ name = "overnightworks-quality", git = "%s" }]\\n' "$STUB_RECEIPT_GIT" \\
        > "$HOME/.local/share/uv/tools/overnightworks-quality/uv-receipt.toml"
    ;;
esac
"""


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class FakePlaybook:
    """Ansible as the system step sees it: a converging playbook, whose check finds a change until a run has applied it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.succeeds = True
        self.applied = False

    def __call__(self, argv: Sequence[str]) -> system_step.PlaybookRun:
        self.calls.append(list(argv))
        changed = 0 if self.applied else 1
        if self.succeeds and "--check" not in argv:
            self.applied = True
        return system_step.PlaybookRun(self.succeeds, changed)


@dataclass
class FakeSystem:
    """The system step's boundaries: a terminal that is there or not, the FakePlaybook, and the running user (the operator unless a test says otherwise)."""

    playbook: FakePlaybook
    terminal: bool = True
    user: str = "operator"

    @property
    def boundaries(self) -> system_step.Boundaries:
        return system_step.Boundaries(lambda: self.terminal, self.playbook, lambda: self.user)


@dataclass(frozen=True)
class Sandbox:
    """A throwaway HOME whose global git config, `uv`, scanner and system step are stubs; nothing reaches the operator's own or sudo."""

    home: Path
    global_git_config: Path
    uv_calls: Path
    system: FakeSystem

    @property
    def layout(self) -> Layout:
        return Layout(self.home)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    home = tmp_path / "home"
    home.mkdir()
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "uv", STUB_UV)
    _write_executable(tmp_path / "stub-quality", STUB_QUALITY)
    global_git_config = tmp_path / "global-gitconfig"
    global_git_config.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n")
    uv_calls = tmp_path / "uv-calls"
    unit_directory = tmp_path / "checkout-units"
    unit_directory.mkdir()
    for name, value in {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_GLOBAL": str(global_git_config),
        "GIT_CONFIG_SYSTEM": os.devnull,
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "STUB_QUALITY": str(tmp_path / "stub-quality"),
        "STUB_UV_LOG": str(uv_calls),
        "STUB_RECEIPT_GIT": RECORDED_RECEIPT_GIT,
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("STUB_UV_FAIL", "STUB_ENGINES_FAIL", "STUB_SCAN_EXIT", "HOME_GIT_HOOK_CHAINED"):
        monkeypatch.delenv(name, raising=False)
    # A real ~/git/edge in the sandbox home would be a repository the hook check and tidy report on.
    monkeypatch.setattr(cli, "edge_units_directory", lambda _layout: unit_directory)
    # The packaged manifest names secrets this sandbox home lacks; the secrets step of `hal setup` looks at none unless a test names some.
    empty_manifest = tmp_path / "empty-secrets-manifest.toml"
    empty_manifest.write_text("secret = []\n")
    monkeypatch.setattr(secrets_view, "MANIFEST_PATH", empty_manifest)
    system = FakeSystem(FakePlaybook())
    monkeypatch.setattr(system_step, "TERMINAL_BOUNDARIES", system.boundaries)
    return Sandbox(home, global_git_config, uv_calls, system)


class RunnerWithCleanLogs(RunnerWithRealGitAndQuality):
    """The logs check reads docker and journalctl; this machine runs no container and its journal is young."""

    container_listing = ""
    platform_listing = ""  # what `docker inspect` answers the platform check: name, state, health, exit code, compose project or nothing per line

    def __call__(self, argv: Sequence[str]) -> str | None:
        if argv[0] == "journalctl":
            return f'[{{"index":0,"first_entry":{int(time.time() * 1_000_000)}}}]'
        if argv[:2] == ["docker", "inspect"] and argv[3] != logs_check.INSPECT_FORMAT:
            self.calls.append(list(argv))
            return self.platform_listing if argv[3] == ALLOWED_PLATFORM_INSPECT_FORMAT else CANARY_INSPECT_LINE
        if argv[0] == "docker" and (argv[1] in {"ps", "inspect"} or "{{.LoggingDriver}}" in argv):
            self.calls.append(list(argv))
            return {"ps": "id0\n" if self.container_listing or self.platform_listing else "", "info": "journald\n", "inspect": self.container_listing or "/quiet-1\tjournald\t\n"}[argv[1]]
        if argv[0] == "sh":
            self.calls.append(list(argv))
            return run_command(argv)
        return super().__call__(argv)


def write_backup_status(home: Path, state: str = "ok", last_backup: datetime | None = None) -> None:
    last_backup = last_backup or datetime.now().astimezone()
    document = {"schema": 2, "state": state, "last_attempt": last_backup.isoformat(), "failing_app": None, "exit_category": None}
    document["apps"] = {"songmaker": {"last_successful_backup": last_backup.isoformat()}}
    status_file = home / backup_check.STATUS_FILE
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(document))


@pytest.fixture
def quiet_pc(sandbox: Sandbox, clean_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check's reporting and news are tested on a PC whose other checks are clean: no pending reboot (this machine's flag is not read), a backup that ran today and a set-up hook."""
    monkeypatch.setattr(check, "REBOOT_REQUIRED_FILE", tmp_path / "reboot-required")
    write_backup_status(clean_home)
    assert cli.main(["setup", "--apply"], home=clean_home) == 0


def home_check(home: Path, runner, probe: FakeProbe | None = None) -> int:
    return cli.main(["check"], home=home, run=runner, probe=probe or FakeProbe())


REPORT_DISPLAY = "~/.local/state/home/check-report.md"


def restarting_containers(runner, *names: str) -> None:
    """A finding at Handlungsbedarf: each container restarts all the time (#415 Z3)."""
    runner.platform_listing = "".join(f"/{name}-1\trestarting\t\t0\t{name}\n" for name in names or ("edge-a",))


def no_container_restarts(runner) -> None:
    runner.platform_listing = ""


@pytest.fixture
def mail_stub(clean_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MailStub:
    return set_up_mail(clean_home, tmp_path, monkeypatch)


def message_of(sent: SentMail) -> email.message.EmailMessage:
    return email.message_from_string(sent.stdin, policy=email.policy.default)  # type: ignore[return-value]
