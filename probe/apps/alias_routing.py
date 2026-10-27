"""The alias of an app's routed service, driven on a running `Probe` of `probe.py` (#445): R3
Traefik routes `Host(name)` and `Host(alias)` to a service labelled with both and only
`Host(name)` without the alias, R4 and logs no error doing so."""

from __future__ import annotations

import re
import time

from edge_copy import expect

ALIASED = "v2026.10.27.1"  # newer than every tag of the probe's other cells
DROPPED = "v2026.10.27.2"
VOLUME = "    volumes: [data:/data]\n"


class AliasRouting:
    def __init__(self, probe, domain: str) -> None:
        self.probe, self.domain = probe, domain

    def compose_text(self, tag: str, alias: str) -> str:
        """The app's generated compose.yaml with `alias` as a second host label of `web`."""
        probe = self.probe
        text = probe.compose_text(tag, ("web",), False, VOLUME)
        return text.replace(
            f"      probe.tag: {tag}\n",
            f"      probe.tag: {tag}\n      {probe.alias_label}: {alias}\n",
        )

    def traefik_errors_since(self, stamp: str) -> list[str]:
        logged = self.probe.docker("compose", "logs", "--no-color", "--since", stamp, "traefik")
        lines = logged.stdout.decode().splitlines()
        return [line for line in lines if re.search(r"\bERR\b", line)]

    def prove(self) -> None:
        """#445 R3, R4: the alias host answers, an unknown host gets 404, and dropping the label
        drops the route without a Traefik error."""
        probe = self.probe
        alias_host = f"{probe.key}-talk.{self.domain}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        probe.release(ALIASED, compose_file=self.compose_text(ALIASED, f"{probe.key}-talk"))
        probe.apply()
        probe.serves(ALIASED)
        probe.wait_until(
            "the alias host answers",
            lambda: probe.ask("/version", host=alias_host)[1] == ALIASED,
        )
        expect(
            probe.ask("/version", host=f"other.{self.domain}")[0] == 404,
            "R3: an unknown host answers",
        )
        print("R3 held: Host = name and Host = alias reach the app, an unknown host gets 404")
        probe.release(DROPPED)
        probe.apply()
        probe.serves(DROPPED)
        expect(
            probe.ask("/version", host=alias_host)[0] == 404, "R3: the alias routes without a label"
        )
        errors = self.traefik_errors_since(stamp)
        expect(not errors, f"R4: Traefik logged {errors[:2]}")
        print(
            "R3 held: without the label only the name routes; R4 held: no ERR line in Traefik's log"
        )
