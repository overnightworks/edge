"""H1 of #444, driven on a running `Probe` of `probe.py`: a running service without a healthcheck,
or with `disable: true`, is settled, and the app is recorded and then quiet in the plan."""

from __future__ import annotations

from collections.abc import Mapping

from edge_copy import expect


class HelperWithoutHealthcheck:
    def __init__(self, probe, tags: Mapping[str, str]) -> None:
        self.probe, self.tags = probe, tags

    def prove(self) -> None:
        """Docker reports such a helper no health at all."""
        probe = self.probe
        probe.set_enabled(True)
        for key, disabled in (("h1_no_check", False), ("h1_disabled", True)):
            tag = self.tags[key]
            probe.release(
                tag, services=("web", "helper"), unchecked=("helper",), check_disabled=disabled
            )
            probe.apply()
            rows = {row["service"]: row for row in probe.containers()}
            expect(sorted(rows) == ["helper", "web"], f"H1: containers {rows}")
            expect(
                rows["helper"]["state"] == "running" and rows["helper"]["health"] == "",
                f"H1: the helper is {rows['helper']}",
            )
            expect(rows["web"]["health"] == "healthy", "H1: web is not healthy")
            expect(probe.deployed()["tag"] == tag, "H1: the app was not recorded")
            out = probe.apply(typed=b"no\n")
            expect("nothing to change" in out, f"H1: the next plan said: {out[-200:]}")
        print("H1 held: a running helper without a healthcheck (absent, disabled) is recorded")
