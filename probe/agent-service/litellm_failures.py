"""Probe of the API route's failures, keys and rotation (#807 S1b cells F0-F21): the stack of
litellm.py (the real service, the one-route front, LiteLLM and the gateway's second entrypoint,
against a fake Anthropic) with the fake steered through its control file. Nothing live and nothing
paid: the probe refuses to start unless `internet` is internal and holds only the fakes.

    flock -o -w 1200 /tmp/probe-stack.lock setpriv --pdeathsig TERM timeout -k 30 1800 \\
        python3 probe/agent-service/litellm_failures.py --scratch DIR

Measured: a run takes about 16 minutes (F10 waits for the fake's 120 s hold limit) and its
teardown a few seconds (K 30 s, CAP 1800 s).

Evidence: F is the fake provider's request log, R the canary's verdict lines, I `docker inspect`,
L the container logs, LA LiteLLM's access log. F0 is the baseline run that proves the zero
observations of F2 and F21 could have been something else. The probe differs from production as
litellm.py does, plus: the fake's control file; F5 and F2 only, LiteLLM's lower `request_timeout`;
F9 only, the fake's certificate from a second scratch CA. Exit 0: every cell held.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from litellm import (
    CANARY_SECONDS,
    DEFAULT_APP,
    FAKE_PROVIDER,
    FRONT,
    GATEWAY,
    LABEL,
    LISTENING,
    LITELLM,
    PROJECT,
    PROVIDER_HOST,
    REPO,
    ROUTE,
    SERVICE,
    LitellmProbe,
    ProbeFailed,
    expect,
    run_probe,
    scratch_directory,
)
from scratch_secrets import issue_certificate, write_new, write_scratch_secrets

UNAVAILABLE = "provider_unavailable"
LOWER_TIMEOUT_SECONDS = 5
HOLD_END_SECONDS = 150
CENSUS_PROGRAM = """\
import json, os, sys
names = [name.encode() + b"=" for name in sys.argv[1:]]
rows = {}
for entry in os.listdir("/proc"):
    if entry.isdigit() and int(entry) != os.getpid():
        fields = open(f"/proc/{entry}/stat").read().rpartition(")")[2].split()
        environ = open(f"/proc/{entry}/environ", "rb").read().split(b"\\0")
        holds = [n[:-1].decode() for n in names if any(item.startswith(n) for item in environ)]
        rows[entry] = {"parent": int(fields[1]), "holds": holds}
core = [line for line in open("/proc/1/limits") if line.startswith("Max core file size")]
print(json.dumps({"processes": rows, "core": core[0].split()[-3:-1]}))
"""
SECRET_NAMES = ("ANTHROPIC_API_KEY", "LITELLM_MASTER_KEY")


class FailureProbe(LitellmProbe):
    """litellm.py's stack, with the provider and the keys made to fail."""

    def __init__(self, scratch: Path) -> None:
        super().__init__(scratch)
        self.scratch = scratch
        self.home = Path(self.environment["HOME"])
        self.master_key = (self.home / ".secrets/litellm/master-key").read_text().strip()
        self.every_value = [self.provider_key, self.credential, self.master_key]
        self.corpus: list[str] = []
        self.failures: list[str] = []

    # --- plumbing -----------------------------------------------------------------------------

    def steer(self, **steering: object) -> None:
        self.steer_fake(provider_key=self.provider_key, credential=self.credential, **steering)

    def canary(self, app: str, options: tuple[str, ...] = ()) -> dict[str, str]:
        result = super().canary(app, options)
        self.corpus.append(json.dumps(result))
        return result

    def hold(self, cell: str, how: str) -> None:
        super().hold(cell, how)
        self.corpus.append(f"{cell} held: {how}")

    def containers_of(self, service: str) -> list[str]:
        """Every container of `service`, stopped ones included."""
        return self.run([
            "docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}",
            "--filter", f"label=com.docker.compose.service={service}",
        ]).stdout.split()  # fmt: skip

    def harvest(self, *services: str) -> None:
        """Keeps I and L of the containers of `services` before a recreate takes them away."""
        for container in (found for service in services for found in self.containers_of(service)):
            self.corpus.append(self.run(["docker", "inspect", container]).stdout)
            logs = self.run(["docker", "logs", container], check=False)
            self.corpus.append(logs.stdout + logs.stderr)

    def recreate_litellm(self) -> None:
        self.harvest(LITELLM)
        super().recreate_litellm()

    def recreate(self, service: str) -> None:
        self.harvest(service)
        self.compose("up", "--detach", "--wait", "--force-recreate", "--no-deps", service)

    def start_again(self, service: str) -> None:
        self.compose("up", "--detach", "--no-deps", service)

    def wait_for_front(self) -> None:
        deadline = time.monotonic() + 60
        while True:
            try:
                if self.http("GET", "/") == 404:
                    return
            except ProbeFailed:
                pass
            expect(time.monotonic() < deadline, "the front does not answer")
            time.sleep(1)

    def replace_by_rename(self, relative: str, contents: str, mode: int = 0o600) -> None:
        """A new file at the secret's path: a new inode, as a writer that renames makes it."""
        target = self.home / relative
        draft = target.with_name(target.name + ".new")
        write_new(draft, contents.encode(), mode)
        draft.replace(target)

    def credential_document(self, value: str) -> str:
        return json.dumps({"shared_key": value})

    def counts(self) -> tuple[int, int]:
        return len(self.provider_requests()), len(self.access_lines())

    def failed_run(self, cell: str, *, provider_sees: int | None) -> None:
        """A run that ends `failed: provider_unavailable` on route api; the fake got
        `provider_sees` requests when that is given."""
        before = len(self.provider_requests())
        run = self.canary(DEFAULT_APP)
        expect(run.get("route") == "api" and run.get("code") == UNAVAILABLE, f"{cell}: {run}")
        expect(run["exit"] != "0", f"{cell}: the failed run exited 0")
        seen = len(self.provider_requests()) - before
        expect(provider_sees in (None, seen), f"{cell}: the fake provider got {seen} requests")

    # --- the cells ----------------------------------------------------------------------------

    def drive(self) -> None:
        self.wait_until(SERVICE, LISTENING)
        self.wait_until(LITELLM, LISTENING)
        self.steer()
        self.cell_f0()
        self.cells_stopped_hops()
        self.cells_provider_failures()
        self.cell_f9()
        self.cell_f10()
        self.cells_f11_f12_f18()
        self.cell_f17()
        self.cells_f13_f14()
        self.cell_f20()
        self.cells_f15_f16()
        self.cell_f19()
        expect(not self.failures, "; ".join(self.failures))

    def cell_f0(self) -> None:
        before_f, before_la = self.counts()
        run = self.canary(DEFAULT_APP)
        expect(run.get("exit") == "0" and run.get("route") == "api", f"F0: {run}")
        sent, lines = self.counts()
        expect((sent - before_f, lines - before_la) == (1, 1), f"F0: F {sent - before_f}, LA")
        self.hold("F0", "succeeded on route api, F 1, LA one line")

    def cells_stopped_hops(self) -> None:
        """F1, F2 and F21: a hop of the path stopped, then brought back."""
        self.compose("stop", LITELLM)
        self.failed_run("F1", provider_sees=0)
        self.hold("F1", "LiteLLM stopped: route api, provider_unavailable")
        self.recreate(LITELLM)
        self.wait_until(LITELLM, LISTENING)
        self.compose("stop", FRONT)
        _, before = self.counts()
        self.failed_run("F21", provider_sees=0)
        expect(self.counts()[1] == before, "F21: LiteLLM's log grew")
        self.hold("F21", "the front stopped: provider_unavailable, LiteLLM's log has 0 new lines")
        self.start_again(FRONT)
        self.wait_for_front()
        self.compose("stop", GATEWAY)
        self.failed_run("F2", provider_sees=0)
        self.hold("F2", "the gateway stopped: provider_unavailable, F 0")
        self.start_again(GATEWAY)
        self.wait_until_the_gateway_answers()
        run = self.canary(DEFAULT_APP)
        expect(run["exit"] == "0", f"F2: the stack did not recover: {run}")

    def cells_provider_failures(self) -> None:
        for cell, steering, how in (
            ("F3", {"mode": "status", "status": 500}, "upstream 500"),
            ("F4", {"mode": "status", "status": 529}, "upstream 529"),
            ("F6", {"mode": "reset"}, "connection reset"),
            ("F7", {"mode": "billing"}, "Anthropic's billing refusal"),
            ("F8", {"mode": "broken"}, "200 with a broken body"),
        ):  # fmt: skip
            self.steer(**steering)
            self.failed_run(cell, provider_sees=1)
            self.hold(cell, f"{how}: F 1, provider_unavailable")
        self.cell_f5()

    def cell_f5(self) -> None:
        shipped = self.litellm_config.read_text(encoding="utf-8")
        lower = f"litellm_settings:\n  request_timeout: {LOWER_TIMEOUT_SECONDS}\n"
        self.litellm_config.write_text(
            shipped.replace("litellm_settings:\n", lower, 1), encoding="utf-8"
        )
        self.recreate_litellm()
        self.steer(mode="hold")
        already = len(self.hold_ends())
        self.failed_run("F5", provider_sees=1)
        ended = self.wait_for_hold_end("F5", already)
        expect(ended == "disconnected", "F5: LiteLLM kept the request")
        self.hold("F5", "upstream timeout: F 1, provider_unavailable")
        self.litellm_config.write_text(shipped, encoding="utf-8")
        self.recreate_litellm()
        self.steer()

    def cell_f9(self) -> None:
        other_home = self.scratch / "other-home"
        other_home.mkdir()
        other = write_scratch_secrets(other_home, self.scratch / "other-ca")
        folder = self.fake / PROVIDER_HOST
        issue_certificate(other, PROVIDER_HOST, folder)
        for file in folder.iterdir():
            file.chmod(0o644)
        self.recreate(FAKE_PROVIDER)
        self.failed_run("F9", provider_sees=0)
        self.hold("F9", "a certificate of an untrusted CA: F 0, provider_unavailable")
        issue_certificate(self.scratch_secrets, PROVIDER_HOST, folder)
        for file in folder.iterdir():
            file.chmod(0o644)
        self.recreate(FAKE_PROVIDER)
        run = self.canary(DEFAULT_APP)
        expect(run["exit"] == "0", f"F9: the stack did not recover: {run}")

    def hold_ends(self) -> list[str]:
        return re.findall(r"^held (disconnected|timed out)$", self.log_of(FAKE_PROVIDER), re.M)

    def wait_for_hold_end(self, cell: str, already: int) -> str:
        """How the fake's next held request ended, `disconnected` or `timed out`; one that ends
        neither way within the fake's hold time fails the cell."""
        deadline = time.monotonic() + HOLD_END_SECONDS
        while time.monotonic() < deadline:
            ended = self.hold_ends()
            if len(ended) > already:
                return ended[already]
            time.sleep(1)
        raise ProbeFailed(f"{cell}: the held request never ended")

    def cell_f10(self) -> None:
        self.steer(mode="hold")
        before, already = len(self.provider_requests()), len(self.hold_ends())
        run = self.canary(DEFAULT_APP, ("--cancel",))
        expect(run.get("route") == "api" and run.get("status") == "cancelled", f"F10: {run}")
        sent = len(self.provider_requests()) - before
        expect(sent == 1, f"F10: the provider got {sent} requests")
        ended = self.wait_for_hold_end("F10", already)
        self.steer()
        if ended != "disconnected":
            self.failures.append(
                "F10: the cancelled run reached no disconnect at the provider; it kept the "
                f"request until the fake's own end ({ended}) and would have been billed"
            )
            return
        self.hold("F10", "status cancelled, F 1 request and it saw the disconnect")

    def cells_f11_f12_f18(self) -> None:
        malformed = secrets.token_hex(12)
        self.every_value.append(malformed)
        credential_file = ".secrets/agent-service/api/credential.json"
        for cell, contents, mode, how in (
            ("F11", f"not a credential {malformed}", 0o600, "a malformed credential"),
            ("F12", self.credential_document(malformed), 0o000, "an unreadable credential"),
        ):  # fmt: skip
            self.replace_by_rename(credential_file, contents, mode)
            self.recreate_litellm()
            status = self.http("POST", ROUTE, self.credential, self.good_body())
            expect(status == 401, f"{cell}: the front answered {status}")
            self.failed_run(cell, provider_sees=0)
            expect(malformed not in self.log_of(LITELLM), f"{cell}: a log line holds a value")
            self.hold(cell, f"{how}: 401, provider_unavailable, F 0, no value in LiteLLM's log")
        self.cell_f18(credential_file)

    def cell_f18(self, credential_file: str) -> None:
        old, new = self.credential, secrets.token_urlsafe(32)
        self.every_value.append(new)
        self.replace_by_rename(credential_file, self.credential_document(new))
        self.recreate_litellm()
        self.credential = new
        expect(self.http("POST", ROUTE, old, self.good_body()) == 401, "F18: the old one passed")
        self.steer()
        self.failed_run("F18", provider_sees=0)
        self.recreate(SERVICE)
        self.wait_until(SERVICE, LISTENING)
        run = self.canary(DEFAULT_APP)
        expect(run["exit"] == "0", f"F18: after the service's recreate: {run}")
        self.hold(
            "F18", "old credential 401 after LiteLLM's recreate; the service fails until its own"
        )

    def cell_f17(self) -> None:
        new = secrets.token_urlsafe(32)
        self.every_value.append(new)
        self.replace_by_rename(".secrets/litellm/anthropic", new + "\n")
        self.provider_key = new
        self.steer()
        before = len(self.provider_requests())
        self.canary(DEFAULT_APP)
        stale = self.provider_requests()[before:]
        expect([r["key_ok"] for r in stale] == [False], "F17: the old inode did not hold")
        self.recreate_litellm()
        before = len(self.provider_requests())
        run = self.canary(DEFAULT_APP)
        sent = self.provider_requests()[before:]
        expect(run["exit"] == "0" and [r["key_ok"] for r in sent] == [True], f"F17: {run} {sent}")
        self.hold("F17", "only the new key reaches the provider, after the recreate")

    def cells_f13_f14(self) -> None:
        key = self.home / ".secrets/litellm/anthropic"
        kept = key.read_text()
        key.unlink()
        self.harvest(LITELLM)
        refused = self.compose(
            "up", "--detach", "--force-recreate", "--no-deps", LITELLM, check=False
        )
        text = refused.stderr + refused.stdout
        self.corpus.append(text)
        expect(refused.returncode != 0, "F13: Compose created the container")
        expect(str(key) in text, f"F13: the refusal does not name the secret: {text[-300:]}")
        refusal = text.strip().splitlines()[-1].replace(str(self.home), "$HOME")
        self.hold("F13", f"Compose refuses to create the container: {refusal}")
        write_new(key, b"", 0o600)
        self.compose("up", "--detach", "--force-recreate", "--no-deps", LITELLM, check=False)
        time.sleep(5)
        self.harvest(LITELLM)
        (container,) = self.containers_of(LITELLM)
        state = json.loads(self.run(["docker", "inspect", container]).stdout)[0]
        expect(state["State"]["ExitCode"] != 0, "F14: the container did not exit")
        logs = self.run(["docker", "logs", container], check=False)
        named = [line for line in logs.stderr.splitlines() if line.startswith("litellm: ")]
        expect(
            named and all("/run/secrets/litellm_anthropic_key" in line for line in named),
            f"F14: the log lines name no path: {named[:2]}",
        )
        self.hold("F14", "the container exits non-zero and its line names the path only")
        key.unlink()
        write_new(key, kept.encode(), 0o600)
        self.recreate_litellm()

    def cell_f20(self) -> None:
        self.steer(mode="hold")
        before = len(self.provider_requests())
        process = subprocess.Popen(  # noqa: S603 - argv is built by the program itself
            self.canary_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env=self.environment, cwd=REPO,
        )  # fmt: skip
        process.stdin.write(self.canary_input(DEFAULT_APP))
        process.stdin.close()
        deadline = time.monotonic() + 60
        while len(self.provider_requests()) == before:
            expect(time.monotonic() < deadline, "F20: the run never reached the provider")
            time.sleep(1)
        self.recreate_litellm()
        out, _ = process.communicate(timeout=CANARY_SECONDS)
        run = self.verdicts(out, process.returncode)
        expect(run.get("code") == UNAVAILABLE, f"F20: {run}")
        self.steer()
        again = self.canary(DEFAULT_APP)
        expect(again["exit"] == "0", f"F20: the next run: {again}")
        self.hold("F20", "the run in flight is provider_unavailable, the next one succeeds")

    def cells_f15_f16(self) -> None:
        for service in (SERVICE, FRONT, LITELLM, GATEWAY, FAKE_PROVIDER):
            self.harvest(service)
        text = "\n".join(self.corpus)
        found = [value for value in self.every_value if value in text]
        expect(not found, f"F15: {len(found)} secret values found in I, L, R or the output")
        self.hold("F15", f"{len(self.every_value)} values absent from I, L, R and the output")
        planted = self.every_value[0]
        plant = self.run([
            "docker", "create", "--network", "none", "--label", LABEL,
            "-e", f"PLANT={planted}", self.image, "true",
        ]).stdout.strip()  # fmt: skip
        inspected = self.run(["docker", "inspect", plant]).stdout
        self.run(["docker", "rm", plant])
        expect(planted in inspected, "F16: the planted copy was not found")
        self.hold("F16", "a planted copy is found in I")

    def cell_f19(self) -> None:
        container = self.container(LITELLM)
        result = self.run(
            ["docker", "exec", "-i", container, "python3", "-", *SECRET_NAMES],
            stdin=CENSUS_PROGRAM,
        )  # fmt: skip
        census = json.loads(result.stdout)
        rows = {int(pid): row for pid, row in census["processes"].items()}
        init_children = [pid for pid, row in rows.items() if row["parent"] == 1]
        expect(len(init_children) == 1, f"F19: init has {len(init_children)} children")

        def in_main_tree(pid: int) -> bool:
            while pid not in {1, init_children[0]}:
                pid = rows[pid]["parent"]
            return pid == init_children[0]

        outside = [pid for pid in rows if pid != 1 and not in_main_tree(pid)]
        holders = {pid for pid, row in rows.items() if row["holds"]}
        expect(not outside, f"F19: {len(outside)} processes outside the main tree")
        expect(1 not in holders and holders, f"F19: holders {sorted(holders)}")
        expect(holders <= {pid for pid in rows if in_main_tree(pid)}, "F19: a holder is outside")
        expect(census["core"] == ["0", "0"], f"F19: core {census['core']}")
        self.hold("F19", f"init and a main tree of {len(rows) - 1}; core size 0; holders inside it")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scratch", type=scratch_directory, required=True)
    arguments = parser.parse_args()
    return run_probe(lambda: FailureProbe(arguments.scratch))


if __name__ == "__main__":
    sys.exit(main())
