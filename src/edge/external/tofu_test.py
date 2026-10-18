"""edge's one way to run the OpenTofu tests: pinned tofu, exact environment, mocked providers.

`python -m edge.external.tofu_test ROOT` installs or reuses the pinned tofu under ROOT and runs
`tofu test` in `external/`. It is a build helper, not an `edge` subcommand. The tests may never act
on the live accounts, so it refuses a test that does not mock both providers or names a live
object, and it gives tofu no token: with none, the github provider would ask the operator's `gh`.
"""

import itertools
import re
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import yaml

from edge.external import secrets, tofu, tofu_binary
from edge.external.tofu import EXTERNAL_ROOT, PROJECT_ROOT, TofuError

FLEET_FILE = PROJECT_ROOT / "fleet.yaml"

TESTS_FOLDER = "tests"
ACCEPTED_TEST_FILE = "*.tftest.hcl"
# Every form `tofu test` runs: in the root folder and in `tests/`.
TOFU_TEST_FILE_PATTERNS = ("*.tftest.hcl", "*.tofutest.hcl", "*.tftest.json", "*.tofutest.json")
MOCKED_PROVIDERS = ("github", "cloudflare")
# Encrypts nothing real: the tests keep no state.
THROWAWAY_STATE_PASSPHRASE = "edge-tofu-test-throwaway-passphrase"
# Makes the github provider's `gh auth token` fallback fail instead of finding the operator's login.
UNREACHABLE_GH = "/nonexistent"
INIT_ARGUMENTS = ["init", "-backend=false", "-lockfile=readonly", "-input=false"]


def live_names(fleet_file: Path = FLEET_FILE) -> tuple[str, str]:
    fleet = yaml.safe_load(fleet_file.read_text())
    return fleet["github"]["org"].lower(), fleet["cloudflare"]["zone"].lower()


def _unaliased_mock(provider: str, content: str) -> bool:
    """A mock with an alias would leave the real default provider in place."""
    return (
        re.search(rf'^mock_provider "{provider}" \{{(?![^}}]*\balias\b)', content, re.M) is not None
    )


def _is_tofu_test_file(path: Path) -> bool:
    return any(path.match(pattern) for pattern in TOFU_TEST_FILE_PATTERNS)


def _files_to_guard(external_directory: Path) -> Iterator[Path]:
    tests_directory = external_directory / TESTS_FOLDER
    in_tests = (path for path in sorted(tests_directory.rglob("*")) if path.is_file())
    in_root = (path for path in sorted(external_directory.iterdir()) if _is_tofu_test_file(path))
    return itertools.chain(in_root, in_tests)


def refusals(external_directory: Path) -> list[str]:
    """One line per way a test file `tofu test` would run could reach a live account."""
    found = []
    names = live_names()
    tests_directory = external_directory / TESTS_FOLDER
    for path in _files_to_guard(external_directory):
        content = path.read_text()
        if _is_tofu_test_file(path):
            if path.parent != tests_directory or not path.match(ACCEPTED_TEST_FILE):
                found.append(
                    f"{path} is a test form the guard does not read: use tests/{ACCEPTED_TEST_FILE}"
                )
            else:
                found += [
                    f"{path} does not mock_provider {provider!r} without an alias"
                    for provider in MOCKED_PROVIDERS
                    if not _unaliased_mock(provider, content)
                ]
        found += [f"{path} names the live {name!r}" for name in names if name in content.lower()]
    return found


def test_environment(tofu_path: Path, home: Path, temporary_directory: Path) -> dict[str, str]:
    """The live wrapper's allowlist with a throwaway passphrase and no way to a token."""
    return secrets.child_environment(
        tofu=tofu_path,
        home=home,
        temporary_directory=temporary_directory,
        tofurc=tofu.TOFURC,
        secrets={
            "TF_VAR_state_passphrase": THROWAWAY_STATE_PASSPHRASE,
            "GH_PATH": UNREACHABLE_GH,
        },
    )


def run_tests(tofu_path: Path, external_directory: Path = EXTERNAL_ROOT) -> int:
    problems = refusals(external_directory)
    if problems:
        print("\n".join(f"edge: {problem}" for problem in problems), file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="edge-tofu-test-") as scratch:
        environment = test_environment(tofu_path, Path(scratch), Path(scratch))
        for arguments in (INIT_ARGUMENTS, ["test"]):
            completed = tofu.run(tofu_path, arguments, environment)
            print(completed.output, end="")
            if completed.exit_code != 0:
                return completed.exit_code
    return 0


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: python -m edge.external.tofu_test ROOT", file=sys.stderr)
        return 2
    try:
        installed = tofu_binary.ensure(Path(arguments[0]))
    except TofuError as error:
        print(f"edge: {error}", file=sys.stderr)
        return 2
    return run_tests(installed)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
