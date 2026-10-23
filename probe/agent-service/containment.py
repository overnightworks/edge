"""The containment cells (`unit.py --cell containment`, agent-runtime#440 B1 and B2): a run whose
app lists tools, in the stand-in issuer's project with a fake app on `agent-songmaker`
(app_tools.py), asked by the repository's own `agent-smoke --tool`.

B1 is the run with the relay: the CLI's start announcement is accepted (no refusal verdict) and
the run reaches the upstream, which refuses the dummy login. B2 plants the run capability's
signature in a tool description: the relay's owner fails with `CapabilityReflected`, the
announcement is refused with `RELAY_NOT_CONNECTED`, and the run fails. A cell reads verdicts,
counts and event names, never a token. The (a) parts, the loopback capture, are #339 C2's."""

from __future__ import annotations

import json
import re
import time
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from egress import LABEL, NotDriven, ProbeFailed, expect
from issuer import ISSUER_SERVICE, SERVICE, IssuerProbe, openssl
from refusals import SMOKE_SERVICE
from smoke import Tokens

APP_SERVICE = "agent-app"
APP_PORT = 8444
APP_ORIGIN = f"https://{APP_SERVICE}:{APP_PORT}"
APP_CERTIFICATE = "/app/cert.pem"
APP_PRIVATE_KEY = "/app/key.pem"
SIGNING_KEY_BITS = "3072"
SERVICE_SIGNING_KEY = "/etc/agent-service/signing.key"
SERVICE_APP_CA = "/etc/agent-service/app-ca.pem"
WORKLOAD_ANCHOR = 'workload_client = "songmaker"\n'
SMOKE_TOOLS = ("step:read_only", "bare:read_only")
REFUSED_ANNOUNCEMENT = "A CLI run's start announcement was refused ("
RELAY_NOT_CONNECTED = f"{REFUSED_ANNOUNCEMENT}RELAY_NOT_CONNECTED)"
LOGIN_REFUSED = "CliLoginRefusedError"
RELAY_OWNER_FAILED = re.compile(
    r"The owner of the relay of run \S+ failed: CapabilityReflected at "
)
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


@dataclass(frozen=True)
class Observed:
    """What one run left behind: the smoke's lines, the fake app's new lines and the new service
    log lines of each kind."""

    smoke: list[str]
    app: list[str]
    refused: int
    login_refused: int
    relay_owner_failed: int
    relay_not_connected: int

    def failed_as_provider_error(self) -> bool:
        return "event: failed" in self.smoke and any(
            line.startswith(PROVIDER_ERROR) for line in self.smoke
        )


class Mode(StrEnum):
    RELAY = "relay"
    CANARY = "canary"


def with_containment(text: str, signing_key: Path, app_ca: Path) -> str:
    """The service's configuration with the signing key, the CA of the app and the app's origin;
    it stops before any container when the repository's own file already declares one of them."""
    document = tomllib.loads(text)
    songmaker = document["apps"]["songmaker"]
    for declared in ("signing", "mcp"):
        expect(declared not in document, f"service.toml already declares [{declared}]")
    expect("mcp_origin" not in songmaker, "service.toml already declares an mcp_origin")
    expect(text.count(WORKLOAD_ANCHOR) == 1, "service.toml: no single songmaker workload client")
    return (
        text.replace(WORKLOAD_ANCHOR, f'{WORKLOAD_ANCHOR}mcp_origin = "{APP_ORIGIN}"\n')
        + f'\n[signing]\nkey_file = "{signing_key}"\n\n[mcp]\nca_file = "{app_ca}"\n'
    )


class ContainmentProbe(IssuerProbe):
    """The service with a signing key and a fake app that lists tools on `agent-songmaker`."""

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
        openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(app / "key.pem"),
            "-out", str(app / "cert.pem"), "-days", "1", "-subj", f"/CN={APP_SERVICE}",
            "-addext", f"subjectAltName=DNS:{APP_SERVICE}",
        )  # fmt: skip
        signing_key = directory / "signing.key"
        openssl("genrsa", "-out", str(signing_key), SIGNING_KEY_BITS)
        signing_key.chmod(0o600)
        for readable in (app, self.mode_directory):
            readable.chmod(0o755)
        for name in ("key.pem", "cert.pem"):
            (app / name).chmod(0o644)
        self.configuration.write_text(
            with_containment(
                self.configuration.read_text(encoding="utf-8"),
                Path(SERVICE_SIGNING_KEY),
                Path(SERVICE_APP_CA),
            ),
            encoding="utf-8",
        )
        program = Path(__file__).resolve().with_name("app_tools.py")
        overlay = scratch / "compose.containment.yaml"
        overlay.write_text(
            json.dumps({"services": {
                SERVICE: {"volumes": [
                    {"type": "bind", "source": str(signing_key), "read_only": True,
                     "target": SERVICE_SIGNING_KEY},
                    {"type": "bind", "source": str(app / "cert.pem"), "read_only": True,
                     "target": SERVICE_APP_CA},
                ]},
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
        failed = [*self.cell_b1(tokens), *self.cell_b2(tokens)]
        self.expect_no_token(tokens, dummy)
        self.cell_s2()
        for cell in ("B1 (a)", "B2 (a)"):
            print(f"{cell} NOT DRIVEN: the loopback capture is #339 C2's", flush=True)
        if failed:
            raise ProbeFailed(f"cells failed: {', '.join(failed)}")

    # --- the cells ----------------------------------------------------------------------------

    def cell_b1(self, tokens: Tokens) -> list[str]:
        seen = self.run_with(Mode.RELAY, tokens)
        self.expect_driven("B1", seen, [*APP_HANDSHAKE, APP_LISTING.format(canary=0)])
        return self.judge({
            "B1 (b)": (
                seen.refused == 0 and seen.login_refused == 1,
                f"{seen.refused} announcement refusals and {seen.login_refused} refused logins",
            ),
            "B1 (c)": (seen.failed_as_provider_error(), f"the smoke printed {seen.smoke}"),
        })  # fmt: skip

    def cell_b2(self, tokens: Tokens) -> list[str]:
        seen = self.run_with(Mode.CANARY, tokens)
        self.expect_driven("B2", seen, [APP_LISTING.format(canary=1)])
        return self.judge({
            "B2 (b)": (
                seen.relay_owner_failed == 1,
                f"{seen.relay_owner_failed} lines of the relay's owner failing on the capability",
            ),
            "B2 (c)": (
                seen.relay_not_connected == 1,
                f"{seen.relay_not_connected} RELAY_NOT_CONNECTED verdicts",
            ),
            "B2 (d)": (seen.failed_as_provider_error(), f"the smoke printed {seen.smoke}"),
        })  # fmt: skip

    def judge(self, verdicts: dict[str, tuple[bool, str]]) -> list[str]:
        """Each cell held or failed, reported at once; the failed names come back."""
        for cell, (held, evidence) in verdicts.items():
            if held:
                self.held.append(cell)
            if not held:
                evidence += f"; the service log ends {self.service_log()[-2500:]!r}"
            print(f"{cell} {'held' if held else 'FAILED'}: {evidence}", flush=True)
        return [cell for cell, (held, _) in verdicts.items() if not held]

    def expect_driven(self, cell: str, seen: Observed, lines: list[str]) -> None:
        missing = [line for line in lines if line not in seen.app]
        if missing:
            raise NotDriven(
                f"{cell}: the fake app did not log {missing}; it logged {seen.app}; the smoke "
                f"printed {seen.smoke}; the service log ends {self.service_log()[-1500:]!r}"
            )

    def cell_s2(self) -> None:
        stray = [line for line in self.app_lines() if not APP_LINE.fullmatch(line)]
        expect(not stray, f"S2: the fake app logged lines outside its vocabulary: {stray[:3]}")
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
        counts = self.wait_for_a_verdict(before)
        refused, login_refused, owner_failed, not_connected = (
            after - earlier for after, earlier in zip(counts, before, strict=True)
        )
        return Observed(
            smoke.stdout.splitlines(),
            self.app_lines()[app_before:],
            refused,
            login_refused,
            owner_failed,
            not_connected,
        )

    def wait_for_a_verdict(self, before: tuple[int, ...]) -> tuple[int, ...]:
        """The service logs a run's verdict a moment after the smoke saw its end."""
        deadline = time.monotonic() + LOG_SECONDS
        counts = self.verdict_counts()
        while counts[1] + counts[2] == before[1] + before[2] and time.monotonic() < deadline:
            time.sleep(0.5)
            counts = self.verdict_counts()
        return counts

    def verdict_counts(self) -> tuple[int, int, int, int]:
        log = self.service_log()
        return (
            log.count(REFUSED_ANNOUNCEMENT),
            log.count(LOGIN_REFUSED),
            len(RELAY_OWNER_FAILED.findall(log)),
            log.count(RELAY_NOT_CONNECTED),
        )

    def service_log(self) -> str:
        logs = self.run(["docker", "logs", self.container(SERVICE)])
        return logs.stdout + logs.stderr

    def app_lines(self) -> list[str]:
        logs = self.run(["docker", "logs", self.container(APP_SERVICE)])
        return (logs.stdout + logs.stderr).splitlines()
