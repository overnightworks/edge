"""What the `test_stack_apply_apps` tests share with other test modules."""

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from support.app_support import (
    FLEET,
    KEY,
    ScriptedDocker,
    origin_at,
    runtime_of,
)
from support.stack_apply import (
    ZONE,
    at_terminal,  # noqa: F401 the fixture, requested by name below
    home,  # noqa: F401 the fixture, requested by name below
    ingress,  # noqa: F401 the fixture, requested by name below
    missing_setup,  # noqa: F401 the fixture, requested by name below
    realm_backup,  # noqa: F401 the fixture, requested by name below
    repository,  # noqa: F401 the fixture, requested by name below
    scripted,  # noqa: F401 the fixture, requested by name below
    scripted_git,  # noqa: F401 the fixture, requested by name below
    setup_check,  # noqa: F401 the fixture, requested by name below
    setup_run,  # noqa: F401 the fixture, requested by name below
    state,  # noqa: F401 the fixture, requested by name below
    switch,  # noqa: F401 the fixture, requested by name below
    write_fleet,
)


@dataclass
class AppWorld:
    """An app `hopin` in the catalog of the live checkout, a real origin it is checked out from and
    a scripted Docker; the catalog holds no unit, so only the app has anything to change."""

    run: object
    repository: Path
    scripted: object
    origin: object
    docker: ScriptedDocker
    runtime: object
    state: Path
    srv: Path

    def apply(self, answer: str = "yes") -> tuple[int, str, str]:
        return self.run(answer, app_runtime=self.runtime)


@pytest.fixture
def app_world(request, tmp_path):
    needs = {name: request.getfixturevalue(name) for name in ("repository", "state", "home")}
    tree, state_directory, operator_home = needs.values()
    write_fleet(tree, f"cloudflare:\n  zone: {ZONE}\n{FLEET}")
    srv = operator_home / "srv" / KEY
    docker = ScriptedDocker(state_directory, srv)
    return AppWorld(
        request.getfixturevalue("switch"), tree, request.getfixturevalue("scripted"),
        origin_at(tmp_path, operator_home), docker,
        runtime_of(operator_home, state_directory, docker), state_directory, srv,
    )  # fmt: skip


RELEASE = "v2026.10.22.1"


def deployed(app_world: AppWorld) -> None:
    app_world.origin.release(RELEASE)
    stdin, stdout = sys.stdin, sys.stdout
    try:
        assert app_world.apply()[0] == 0
    finally:
        sys.stdin, sys.stdout = stdin, stdout
