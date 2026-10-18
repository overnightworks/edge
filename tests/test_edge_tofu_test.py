"""`tofu_test`: the OpenTofu tests run on the pinned tofu, in an exact environment, never live."""

import os
import stat
from pathlib import Path

import pytest

from edge.external import tofu_test
from edge.external.tofu import TofuError

MOCKS = 'mock_provider "github" {}\nmock_provider "cloudflare" {}\n'
INHERITED_FROM_THE_CALLER = {
    "GITHUB_TOKEN": "caller-github-token",
    "GH_TOKEN": "caller-gh-token",
    "CLOUDFLARE_API_TOKEN": "caller-cloudflare-token",
    "TF_LOG": "TRACE",
    "TF_CLI_ARGS": "-no-color",
}


def write_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)
    return tmp_path


COMMENTED_MOCK = '# mock_provider "github" {}\nmock_provider "cloudflare" {}\n'
ALIASED_MOCK = 'mock_provider "github" {\n  alias = "fake"\n}\nmock_provider "cloudflare" {}\n'


@pytest.mark.parametrize(
    ("files", "named_file", "named_reason"),
    [
        ({"tests/a.tftest.hcl": 'mock_provider "cloudflare" {}\n'}, "a.tftest.hcl", "github"),
        ({"tests/a.tftest.hcl": 'mock_provider "github" {}\n'}, "a.tftest.hcl", "cloudflare"),
        ({"tests/a.tftest.hcl": COMMENTED_MOCK}, "a.tftest.hcl", "github"),
        ({"tests/a.tftest.hcl": ALIASED_MOCK}, "a.tftest.hcl", "github"),
        (
            {"tests/a.tftest.hcl": MOCKS, "tests/fixtures/org.yaml": "org: OvernightWorks"},
            "org.yaml",
            "overnightworks",
        ),
        (
            {"tests/a.tftest.hcl": MOCKS + 'zone = "hallucinai.de"'},
            "a.tftest.hcl",
            "hallucinai.de",
        ),
        ({"root.tftest.hcl": MOCKS}, "root.tftest.hcl", "does not read"),
        ({"tests/other.tofutest.hcl": MOCKS}, "other.tofutest.hcl", "does not read"),
        ({"tests/j.tftest.json": "{}"}, "j.tftest.json", "does not read"),
    ],
    ids=[
        "no github mock", "no cloudflare mock", "commented mock", "aliased mock",
        "live org in a fixture", "live zone in a test",
        "test in the root folder", "tofutest form", "json form",
    ],
)  # fmt: skip
def test_refuses_a_test_that_could_reach_a_live_account(tmp_path, files, named_file, named_reason):
    """#123 S3/S4: no test file tofu runs may reach a live account."""
    problems = tofu_test.refusals(write_tree(tmp_path, files))

    assert len(problems) == 1
    assert named_file in problems[0]
    assert named_reason in problems[0]


def test_accepts_mocked_tests_with_fixture_names(tmp_path):
    """#123 S3: mocked tests with invalid fixture names pass the guard."""
    files = {"tests/a.tftest.hcl": MOCKS, "tests/fixtures/empty.yaml": "org: edge-fixture.invalid"}

    assert tofu_test.refusals(write_tree(tmp_path, files)) == []


def test_the_real_tests_pass_their_own_guards():
    """#123 S3: the repository's own tests satisfy the guard."""
    assert tofu_test.refusals(tofu_test.EXTERNAL_ROOT) == []


def test_live_names_come_from_the_fleet_file(tmp_path):
    """#123 S4: the guarded zone and org are the ones fleet.yaml names."""
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text("cloudflare:\n  zone: Example.Test\ngithub:\n  org: Acme\n")

    assert tofu_test.live_names(fleet) == ("acme", "example.test")


def test_environment_is_exactly_the_allowlist(tmp_path):
    """#123 S5: tofu sees exactly the allowlist."""
    environment = tofu_test.test_environment(
        tmp_path / "bin" / "tofu", tmp_path / "home", tmp_path / "tmp"
    )

    assert set(environment) == {
        "PATH", "HOME", "LANG", "TMPDIR", "TF_INPUT", "TF_CLI_CONFIG_FILE",
        "TF_VAR_state_passphrase", "GH_PATH",
    }  # fmt: skip
    assert environment["GH_PATH"] == "/nonexistent"
    assert environment["TF_INPUT"] == "0"
    assert environment["PATH"].startswith(f"{tmp_path / 'bin'}:")


def test_a_refused_test_stops_before_tofu_runs(tmp_path, capsys):
    """#123 S3: a refused test stops before tofu runs."""
    external = write_tree(tmp_path, {"tests/a.tftest.hcl": "run {}\n"})
    never_run = tmp_path / "tofu"

    assert tofu_test.run_tests(never_run, external) == 2
    assert "a.tftest.hcl" in capsys.readouterr().err


def test_tofu_gets_a_fresh_home_and_nothing_inherited(tmp_path, monkeypatch):
    """#123 S5: tofu gets a fresh home and nothing inherited from the caller."""
    for name, value in INHERITED_FROM_THE_CALLER.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    dump = tmp_path / "seen.env"
    fake_tofu = tmp_path / "tofu"
    fake_tofu.write_text(f"#!/bin/sh\nenv > {dump}\nexit 3\n")
    fake_tofu.chmod(fake_tofu.stat().st_mode | stat.S_IXUSR)

    exit_code = tofu_test.run_tests(fake_tofu, write_tree(tmp_path / "t", {}))

    seen = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert exit_code == 3
    assert not set(INHERITED_FROM_THE_CALLER) & set(seen)
    assert seen["GH_PATH"] == "/nonexistent"
    assert seen["HOME"] != str(tmp_path / "real-home")
    assert not os.path.exists(seen["HOME"])


def _fake_tofu(tmp_path: Path, exit_code: int) -> Path:
    fake = tmp_path / "tofu"
    fake.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def _ensure_raising(error: TofuError):
    def ensure(root: Path) -> Path:
        raise error

    return ensure


@pytest.mark.parametrize(
    ("arguments", "ensure", "exit_code", "stderr_names"),
    [
        ([], None, 2, "usage"),
        (["root"], _ensure_raising(TofuError("no pinned tofu")), 2, "no pinned tofu"),
        (["root"], None, 0, ""),
    ],
    ids=["usage error", "tofu cannot be installed", "init and test succeed"],
)
def test_main_exits_with_the_outcome(
    tmp_path, monkeypatch, capsys, arguments, ensure, exit_code, stderr_names
):
    """#123 S3: main reports usage and install errors with 2 and success with 0."""
    monkeypatch.setattr(
        tofu_test.tofu_binary, "ensure", ensure or (lambda root: _fake_tofu(tmp_path, 0))
    )

    assert tofu_test.main(arguments) == exit_code
    assert stderr_names in capsys.readouterr().err
