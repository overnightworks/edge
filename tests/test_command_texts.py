"""The program name has one owner: no operator-facing text in `src/` types a command by hand."""

import ast
import re
import tomllib

import pytest
from git_support import SOURCE

from hal import cli
from hal_output.command import PROGRAM, Verb, command

OWNER = SOURCE / "hal_output" / "command.py"
HAND_WRITTEN_COMMAND = re.compile(
    rf"(?<![\w/.\-]){re.escape(PROGRAM)} (?:(?:{'|'.join(Verb)})\b|--)"
)
DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def docstrings(tree: ast.AST) -> set[ast.expr]:
    return {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, DOCUMENTED)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def hand_written_command_lines(source: str) -> list[int]:
    """The lines of string constants and f-string parts that type the program by hand."""
    tree = ast.parse(source)
    skipped = docstrings(tree)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node not in skipped
        and (HAND_WRITTEN_COMMAND.search(node.value) or node.value.endswith(f"{PROGRAM} "))
    ]


@pytest.mark.parametrize(
    "source",
    [
        'MESSAGE = "run hal setup --apply"',
        'def f(x):\n    return f"hal backup {x}"',
        'NEXT = "run: hal " + verb',
        'NEXT = f"hal {verb}"',
        'HELP = "hal --help"',
    ],
    ids=[
        "constant",
        "f-string part",
        "constant ending in the program and a space",
        "f-string head",
        "flag",
    ],
)
def test_a_hand_written_command_text_is_found(source):
    assert hand_written_command_lines(source)


@pytest.mark.parametrize(
    "source",
    [
        'def f():\n    """Runs `hal apply` and `hal check`."""',
        'class C:\n    """hal setup --apply"""',
        'PATH = ".venv/bin/hal"',
        'UNIT = "hal-check.service"',
        'STATE = "~/.local/state/hal"',
        'PROSE = "the hal process"',
    ],
    ids=["function docstring", "class docstring", "executable path", "unit", "state path", "prose"],
)
def test_text_that_is_not_a_typed_command_passes(source):
    assert hand_written_command_lines(source) == []


def test_a_command_joins_the_program_its_verb_and_its_arguments():
    assert command(Verb.SETUP, "--apply") == "hal setup --apply"


def test_no_text_in_src_types_a_command_by_hand():
    sites = [
        f"{path.relative_to(SOURCE.parent)}:{line}"
        for path in sorted(SOURCE.rglob("*.py"))
        if path != OWNER
        for line in hand_written_command_lines(path.read_text())
    ]

    assert sites == []


def test_the_registry_holds_exactly_the_verbs():
    assert sorted(command.name for command in cli.COMMANDS) == sorted(Verb)


def test_the_parser_and_the_console_script_carry_the_program_name():
    scripts = tomllib.loads((SOURCE.parent / "pyproject.toml").read_text())["project"]["scripts"]

    assert cli.build_parser(cli.COMMANDS).prog == PROGRAM
    assert PROGRAM in scripts
