"""`hal setup`: the exit numbers of the command, as a table. The steps run against a sandboxed HOME
in tests/home/test_setup.py, where the sandbox lives."""

from hal import setup_commands
from hal.grammar import Asks, ExitCode


def test_a_successful_home_setup_is_hals_0_and_a_failed_step_is_hals_3():
    assert setup_commands.EXIT_OF_SETUP == {0: ExitCode.OK, 1: ExitCode.FAILURE}


def test_setup_is_a_command_that_waits_for_the_person_and_prints_no_json():
    (command,) = setup_commands.COMMANDS

    assert (command.name, command.json, command.asks) == ("setup", False, Asks.YES)
