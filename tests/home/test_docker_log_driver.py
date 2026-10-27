"""The Docker log driver `home setup --apply` hands the system play."""

from __future__ import annotations

import json

from conftest import Sandbox

from home_tools import system_step
from home_tools.cli import main


def test_the_playbook_is_given_the_journal_as_docker_log_driver(sandbox: Sandbox) -> None:
    """E1 to E3: the play merges the one driver name `system_step` owns into `daemon.json`, and `home check` reads the same."""
    main(["setup", "--apply"], home=sandbox.home)

    (argv,) = sandbox.system.playbook.calls
    extra_vars = json.loads(argv[argv.index("--extra-vars") + 1])
    assert extra_vars["docker_log_driver"] == system_step.DOCKER_LOG_DRIVER == "journald"
