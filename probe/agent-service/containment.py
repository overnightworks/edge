"""The containment cells (`unit.py --cell containment`, agent-runtime#440 B1 and B2): a run whose
app lists tools, in the stand-in issuer's project with a fake app on `agent-songmaker`
(app_tools.py), asked by the repository's own `agent-smoke --tool`.

B1 is the run with the relay: the CLI's start announcement is accepted (no refusal verdict) and
the run reaches the upstream, which refuses the dummy login. B2 plants the run capability's
signature in a tool description: the relay's owner fails with `CapabilityReflected`, and the
runner's failure is `CapabilityReflected` too, with no announcement verdict (a
`RELAY_NOT_CONNECTED` line is tolerated, any other verdict fails the cell) and no run reaching
`CliLoginRefusedError`; the run fails. A cell reads verdicts, counts and event names, never a
token, and none of them prints a log line. The (a) parts, the loopback capture, are #339 C2's."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from egress import LABEL, NotDriven, ProbeFailed, expect
from issuer import ISSUER_SERVICE, SERVICE, IssuerProbe
from refusals import SMOKE_SERVICE
from scratch_secrets import issue_certificate
from smoke import Tokens

APP_SERVICE = "agent-app"
APP_PORT = 8444
APP_ORIGIN = f"https://{APP_SERVICE}:{APP_PORT}"
ORIGIN_LINE = re.compile(r'^mcp_origin = ".*"$', re.MULTILINE)
SMOKE_TOOLS = ("step:read_only", "bare:read_only")
REFUSED_ANNOUNCEMENT = "A CLI run's start announcement was refused ("
RELAY_NOT_CONNECTED = f"{REFUSED_ANNOUNCEMENT}RELAY_NOT_CONNECTED)"
LOGIN_REFUSED = "CliLoginRefusedError"
RELAY_OWNER_FAILED = re.compile(
    r"The owner of the relay of run \S+ failed: CapabilityReflected at "
)
RUNNER_FAILED = re.compile(r"The runner of run \S+ failed: CapabilityReflected at ")
APP_LINE = re.compile(
    r"method=(?:initialize|notifications/initialized|tools/list|other) "
    r"offered=(?:-|step,bare,extra) canary=[01] bearer=[01]"
)
LOG_SECONDS = 30


APP_HANDSHAKE = (
    "method=initialize offered=- canary=0 bearer=1",
    "method=notifications/initialized offered=- canary=0 bearer=1",
)
APP_LISTING = "method=tools/list offered=step,bare,extra canary={canary} bearer=1"
PROVIDER_ERROR = "provider_error"


class LogCounts(NamedTuple):
    """How often the service log holds each kind of verdict line."""

    refused: int
    login_refused: int
    relay_owner_failed: int
    runner_failed: int
    relay_not_connected: int

    @classmethod
    def of(cls, log: str) -> LogCounts:
        return cls(
            log.count(REFUSED_ANNOUNCEMENT),
            log.count(LOGIN_REFUSED),
            len(RELAY_OWNER_FAILED.findall(log)),
            len(RUNNER_FAILED.findall(log)),
            log.count(RELAY_NOT_CONNECTED),
        )

    def since(self, earlier: LogCounts) -> LogCounts:
        return LogCounts(*(now - before for now, before in zip(self, earlier, strict=True)))

    def other_refusals(self) -> int:
        """Announcement verdicts other than RELAY_NOT_CONNECTED."""
        return self.refused - self.relay_not_connected


@dataclass(frozen=True)
class Observed:
    """What one run left behind: the smoke's lines, the fake app's new lines and the new service
    log counts."""

    smoke: list[str]
    app: list[str]
    new: LogCounts

    def failed_as_provider_error(self) -> bool:
        return "event: failed" in self.smoke and any(
            line.startswith(PROVIDER_ERROR) for line in self.smoke
        )


Verdicts = dict[str, tuple[bool, str]]


def smoke_evidence(seen: Observed) -> str:
    return (
        f"event: failed {'seen' if 'event: failed' in seen.smoke else 'missing'}, "
        f"{PROVIDER_ERROR} {'seen' if seen.failed_as_provider_error() else 'missing'}"
    )


def b1_verdicts(seen: Observed) -> Verdicts:
    new = seen.new
    return {
        "B1 (b)": (
            new.refused == 0 and new.login_refused == 1,
            f"{new.refused} announcement refusals and {new.login_refused} refused logins",
        ),
        "B1 (c)": (seen.failed_as_provider_error(), smoke_evidence(seen)),
    }


def b2_verdicts(seen: Observed) -> Verdicts:
    new = seen.new
    return {
        "B2 (b)": (
            new.relay_owner_failed == 1,
            f"{new.relay_owner_failed} lines of the relay's owner failing on the capability",
        ),
        "B2 (c)": (
            new.runner_failed == 1 and new.login_refused == 0 and new.other_refusals() == 0,
            f"{new.runner_failed} lines of the runner failing on the capability, "
            f"{new.login_refused} refused logins, {new.other_refusals()} other announcement "
            f"verdicts, {new.relay_not_connected} RELAY_NOT_CONNECTED verdicts (tolerated)",
        ),
        "B2 (d)": (seen.failed_as_provider_error(), smoke_evidence(seen)),
    }


class Mode(StrEnum):
    RELAY = "relay"
    CANARY = "canary"


def with_containment(text: str) -> str:
    """The service's configuration with the fake app's origin in place of songmaker's; the
    signing key and the CA come from the repository's own `[signing]` and `[mcp]`."""
    expect(len(ORIGIN_LINE.findall(text)) == 1, "service.toml: no single mcp_origin")
    return ORIGIN_LINE.sub(f'mcp_origin = "{APP_ORIGIN}"', text)


class ContainmentProbe(IssuerProbe):
    """The service with the scratch signing key and CA and a fake app that lists tools on
    `agent-songmaker`, its certificate issued by that CA."""

    def __init__(self, scratch: Path) -> None:
        super().__init__(scratch)
        self.shown: list[str] = []
        directory = scratch / "containment"
        directory.mkdir()
        app = directory / "app"
        app.mkdir()
        self.mode_directory = directory / "mode"
        self.mode_directory.mkdir()
        self.write_mode(Mode.RELAY)
        certificate, key = issue_certificate(self.scratch_secrets, APP_SERVICE, app)
        for readable in (app, self.mode_directory):
            readable.chmod(0o755)
        for name in (certificate, key):
            name.chmod(0o644)
        self.configuration.write_text(
            with_containment(self.configuration.read_text(encoding="utf-8")), encoding="utf-8"
        )
        program = Path(__file__).resolve().with_name("app_tools.py")
        overlay = scratch / "compose.containment.yaml"
        overlay.write_text(
            json.dumps({"services": {
                APP_SERVICE: {
                    "image": self.image,
                    "profiles": ["agent-service"],
                    "user": "65534:65534", "read_only": True, "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"], "networks": ["agent-songmaker"],
                    "entrypoint": ["python", "-I", "-B", "/app_tools.py"],
                    "volumes": [
                        {"type": "bind", "source": str(program), "target": "/app_tools.py",
                         "read_only": True},
                        {"type": "bind", "source": str(app), "target": "/app",
                         "read_only": True},
                        {"type": "bind", "source": str(self.mode_directory), "target": "/mode",
                         "read_only": True},
                    ],
                },
            }}),
            encoding="utf-8",
        )  # fmt: skip
        self.overlays.append(overlay)

    def services_to_start(self) -> tuple[str, ...]:
        return ("agent-egress", ISSUER_SERVICE, APP_SERVICE, SERVICE)

    def write_mode(self, mode: Mode) -> None:
        (self.mode_directory / "mode").write_text(mode.value, encoding="utf-8")
        (self.mode_directory / "mode").chmod(0o644)

    def drive(self) -> None:
        self.wait_until_listening()
        tokens = self.tokens()
        dummy = (self.token_path / "token").read_text(encoding="utf-8").strip()
        relay = self.run_with(Mode.RELAY, tokens)
        canary = self.run_with(Mode.CANARY, tokens)
        self.expect_no_token(tokens, dummy)
        self.cell_s2()
        self.expect_driven("B1", relay, [*APP_HANDSHAKE, APP_LISTING.format(canary=0)])
        self.expect_driven("B2", canary, [APP_LISTING.format(canary=1)])
        failed = [*self.judge(b1_verdicts(relay)), *self.judge(b2_verdicts(canary))]
        for cell in ("B1 (a)", "B2 (a)"):
            print(f"{cell} NOT DRIVEN: the loopback capture is #339 C2's", flush=True)
        if failed:
            raise ProbeFailed(f"cells failed: {', '.join(failed)}")

    # --- the cells ----------------------------------------------------------------------------

    def judge(self, verdicts: Verdicts) -> list[str]:
        """Each cell held or failed, reported at once; the failed names come back."""
        for cell, (held, evidence) in verdicts.items():
            if held:
                self.held.append(cell)
            print(f"{cell} {'held' if held else 'FAILED'}: {evidence}", flush=True)
        return [cell for cell, (held, _) in verdicts.items() if not held]

    def expect_driven(self, cell: str, seen: Observed, lines: list[str]) -> None:
        missing = [line for line in lines if line not in seen.app]
        if missing:
            raise NotDriven(
                f"{cell}: the fake app did not log {len(missing)} of {len(lines)} expected "
                f"lines ({', '.join(line.split()[0] for line in missing)}); it logged "
                f"{len(seen.app)} lines; {smoke_evidence(seen)}"
            )

    def cell_s2(self) -> None:
        stray = [line for line in self.app_lines() if not APP_LINE.fullmatch(line)]
        expect(not stray, f"S2: the fake app logged {len(stray)} lines outside its vocabulary")
        self.held.append("S2")

    def expect_no_token(self, tokens: Tokens, dummy: str) -> None:
        shown = "\n".join([*self.shown, self.service_log(), *self.app_lines()])
        for secret in (tokens.person, tokens.workload, dummy):
            expect(secret not in shown, "S1: a token was printed")
        self.held.append("S1")

    # --- one run and what it leaves -----------------------------------------------------------

    def run_with(self, mode: Mode, tokens: Tokens) -> Observed:
        self.write_mode(mode)
        app_before = len(self.app_lines())
        before = self.verdict_counts()
        tools = [argument for tool in SMOKE_TOOLS for argument in ("--tool", tool)]
        smoke = self.run(
            self.compose_command(
                "run", "--rm", "-T", "--no-deps", "--label", LABEL, SMOKE_SERVICE,
                "--timeout", "180", *tools,
            ),
            stdin=f"{tokens.person}\n{tokens.workload}\n", check=False, timeout=240,
        )  # fmt: skip
        self.shown.append(smoke.stdout + smoke.stderr)
        new = self.wait_for_a_verdict(before).since(before)
        return Observed(smoke.stdout.splitlines(), self.app_lines()[app_before:], new)

    def wait_for_a_verdict(self, before: LogCounts) -> LogCounts:
        """The service logs a run's verdict a moment after the smoke saw its end; the runner's
        line comes after the relay owner's."""
        deadline = time.monotonic() + LOG_SECONDS
        counts = self.verdict_counts()
        while (
            counts.login_refused + counts.runner_failed
            == before.login_refused + before.runner_failed
            and time.monotonic() < deadline
        ):
            time.sleep(0.5)
            counts = self.verdict_counts()
        return counts

    def verdict_counts(self) -> LogCounts:
        return LogCounts.of(self.service_log())

    def service_log(self) -> str:
        return self.container_log(SERVICE)

    def app_lines(self) -> list[str]:
        return self.container_log(APP_SERVICE).splitlines()
