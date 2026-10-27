"""#415 Z3 and Z1: every rule a check can report has exactly one level, and the title says the PC's state from them."""

from __future__ import annotations

import ast
import inspect
from types import ModuleType

import pytest

from home_tools import backup_check, check, hook_check, logs_check, platform_check, secrets_view
from home_tools.report import Level

FINDING_CONSTRUCTORS = {"Finding", "HookFinding"}


def rules_named_in(module: ModuleType) -> set[str]:
    """The first argument of every finding the module builds with a literal rule."""
    calls = (node for node in ast.walk(ast.parse(inspect.getsource(module))) if isinstance(node, ast.Call))
    return {
        call.args[0].value
        for call in calls
        if isinstance(call.func, ast.Name) and call.func.id in FINDING_CONSTRUCTORS and call.args and isinstance(call.args[0], ast.Constant)
    }


CHECK_MODULES = {"platform": platform_check, "backup": backup_check, "hook": hook_check, "logs": logs_check}
RULES_BY_CHECK = {
    **{name: rules_named_in(module) for name, module in CHECK_MODULES.items()},
    "secrets": {rule.value for rule in secrets_view.FindingRule} | {check.MISSING_PASSWORD_RULE},
}
RULES_BY_CHECK["platform"] |= set(platform_check.BROKEN_STATES.values())
RULES = [(name, rule) for name, rules in RULES_BY_CHECK.items() for rule in sorted(rules)]


@pytest.mark.parametrize(("check_name", "rule"), RULES, ids=[f"{name}/{rule}" for name, rule in RULES])
def test_every_rule_of_a_check_has_a_level(check_name: str, rule: str) -> None:
    """#415 Z3: every rule of the five checks has a level."""
    assert (check_name, rule) in check.RULE_REPORTS


def test_no_level_is_kept_for_a_rule_no_check_reports() -> None:
    """#415 Z3: the level table holds no rule a check never reports."""
    assert set(check.RULE_REPORTS) == set(RULES)


@pytest.mark.parametrize(("check_name", "rule"), RULES, ids=[f"{name}/{rule}" for name, rule in RULES])
def test_an_action_has_exactly_one_next_step_and_a_note_has_none(check_name: str, rule: str) -> None:
    """#415 Z2: an action carries exactly one next step, a note none."""
    report = check.RULE_REPORTS[check_name, rule]
    assert bool(report.next_step) == (report.level is Level.ACTION)


@pytest.mark.parametrize(("check_name", "rule"), RULES, ids=[f"{name}/{rule}" for name, rule in RULES])
def test_only_a_container_or_an_app_is_named_in_a_sentence(check_name: str, rule: str) -> None:
    """#415 Z6: no sentence of secrets, hook, logs takes the finding's subject, a path or a file name."""
    report = check.RULE_REPORTS[check_name, rule]
    assert ("{subject}" in report.sentence + report.next_step) <= (check_name in {"platform", "backup"})


@pytest.mark.parametrize(
    ("identities", "title"),
    [
        (set(), "PC-Check: alles in Ordnung"),
        ({"secrets\tmissing\tgitlab", "platform\treboot-pending\treboot"}, "PC-Check: in Ordnung – nur Hinweise"),
        ({"platform\tcontainer-restarting\tedge-a-1"}, "PC-Check: Handlungsbedarf – Dienste"),
        (
            {"logs\tjournal-age\tjournal", "platform\tcontainer-dead\tedge-a-1", "secrets\tmissing\tgitlab"},
            "PC-Check: Handlungsbedarf – Dienste, Server-Logs",
        ),
        ({"hook\tcheck-failed"}, "PC-Check: Handlungsbedarf – Commit-Schutz"),
    ],
    ids=["clean", "only notes", "one area", "two areas and a note", "a check that broke"],
)
def test_the_title_names_the_state_and_at_action_the_areas(identities: set[str], title: str) -> None:
    """#415 Z1: the title names the state and, at an action, the areas."""
    assert check.title(frozenset(identities)) == title


@pytest.mark.parametrize(
    ("identity", "level"),
    [
        ("secrets\tmissing-password\trestic/password", Level.ACTION),
        ("secrets\tmissing\tgitlab", Level.NOTE),
    ],
    ids=["human password", "other secret"],
)
def test_the_level_of_a_missing_secret_follows_its_kind(identity: str, level: Level) -> None:
    """#415 Z3: a missing secret is an action only for a human password."""
    assert check.title(frozenset({identity})) == ("PC-Check: Handlungsbedarf – Geheimnisse" if level is Level.ACTION else "PC-Check: in Ordnung – nur Hinweise")
