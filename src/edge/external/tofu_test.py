"""edge's one way to run the OpenTofu tests: pinned tofu, exact environment, mocked providers.

`python -m edge.external.tofu_test ROOT` installs or reuses the pinned tofu under ROOT and runs
`tofu test` in `external/`. It is a build helper, not an `edge` subcommand. The tests may never act
on the live accounts, so it refuses a test that does not mock both providers or names a live
object, and it gives tofu no token: with none, the github provider would ask the operator's `gh`.
"""

import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from edge.external import secrets, tofu, tofu_binary
from edge.external.tofu import EXTERNAL_ROOT, TofuError

TESTS_DIRECTORY = EXTERNAL_ROOT / "tests"
TEST_FILE_PATTERN = "*.tftest.hcl"
MOCKED_PROVIDERS = ("github", "cloudflare")
LIVE_NAMES = ("overnightworks", "hallucinai.de")
# Encrypts nothing real: the tests keep no state.
THROWAWAY_STATE_PASSPHRASE = "edge-tofu-test-throwaway-passphrase"
# Makes the github provider's `gh auth token` fallback fail instead of finding the operator's login.
UNREACHABLE_GH = "/nonexistent"
INIT_ARGUMENTS = ["init", "-backend=false", "-lockfile=readonly", "-input=false"]


def _test_files(tests_directory: Path) -> Iterator[Path]:
    return (path for path in sorted(tests_directory.rglob("*")) if path.is_file())


def refusals(tests_directory: Path) -> list[str]:
    """One line per way a file under `tests_directory` could reach a live account."""
    found = []
    for path in _test_files(tests_directory):
        content = path.read_text()
        if path.match(TEST_FILE_PATTERN):
            found += [
                f"{path} does not mock_provider {provider!r}"
                for provider in MOCKED_PROVIDERS
                if f'mock_provider "{provider}"' not in content
            ]
        found += [
            f"{path} names the live {name!r}" for name in LIVE_NAMES if name in content.lower()
        ]
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


def run_tests(tofu_path: Path, tests_directory: Path = TESTS_DIRECTORY) -> int:
    problems = refusals(tests_directory)
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
