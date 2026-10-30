"""`hal online` over the barrier of an app (#255 P5, D8), against a fake systemctl."""

from collections.abc import Callable

from support.stack_apply import (
    at_terminal,  # noqa: F401 the fixture, requested by name below
    switch_off,
)
from support.stack_global_stop import (
    home,  # noqa: F401 the fixture, requested by name below
    ingress,  # noqa: F401 the fixture, requested by name below
    marks,  # noqa: F401 the fixture, requested by name below
    repository,  # noqa: F401 the fixture, requested by name below
    start_all,  # noqa: F401 the fixture, requested by name below
    stopped_tunnel,  # noqa: F401 the fixture, requested by name below
    systemd,  # noqa: F401 the fixture, requested by name below
)

from hal.stack import barrier

APP_FLEET = (
    "cloudflare: {zone: probe.test}\ngithub: {org: overnightworks}\n"
    "services:\n  probe:\n    run: {enabled: true}\n"
    "  kapp:\n    repo: {default_branch: main}\n    run: {enabled: true}\n"
    "    hosts: [{name: kapp, exposure: public, reason: a public page, ready: /}]\n"
)


def test_start_all_keeps_the_tunnel_closed_while_an_app_barrier_is_unproven_then_reopens(
    start_all,  # noqa: F811
    stopped_tunnel,  # noqa: F811
    marks,  # noqa: F811
    ingress,  # noqa: F811
    repository,  # noqa: F811
    systemd,  # noqa: F811
    monkeypatch,
):
    """#362 D8: an app's barrier is proven like a unit's; Traefik that has not loaded it keeps the
    tunnel closed, and once it answers the barrier's 404 the tunnel reopens."""
    (repository / "fleet.yaml").write_text(APP_FLEET)
    switch_off(marks.directory, unit="kapp")
    loaded = False

    def fetch(_address: str, host: str, _seconds: float, _clock: Callable[[], float]):
        assert host == "kapp.probe.test"
        return barrier.Answer(404, b"404 page not found\n", "kapp" if loaded else None)

    monkeypatch.setattr(ingress, "fetch", fetch)

    refused, output = start_all()

    assert refused == 1
    assert "the tunnel stays closed: the barrier of kapp is not proven" in output
    assert not systemd.started
    assert (repository / "dynamic" / "switch-kapp.yml").exists()

    loaded = True
    reopened, output = start_all()

    assert reopened == 0
    assert "the barrier of kapp is proven" in output
    assert systemd.active
