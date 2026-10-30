"""#340: the step-ca healthcheck of compose.yaml reports healthy after proof 2."""

from __future__ import annotations

HEALTHY_SECONDS = 100  # three healthcheck intervals of compose.yaml


def health_of(probe, service: str) -> str:
    container = probe.container_id(service)
    result = probe.run(["docker", "inspect", "--format", "{{.State.Health.Status}}", container])
    return result.stdout.decode().strip()


def proof_2_health(probe) -> None:
    probe.poll_until(
        "step-ca becoming healthy",
        HEALTHY_SECONDS,
        lambda: health_of(probe, "step-ca") == "healthy",
    )
    print(f"proof 2 health held: the healthcheck of step-ca reports {health_of(probe, 'step-ca')}")  # noqa: T201 - the cell verdict line of this probe
