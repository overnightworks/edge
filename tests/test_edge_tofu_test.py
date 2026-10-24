"""`tofu_test`: the OpenTofu tests run on the pinned tofu, in an exact environment, never live."""

import os
import stat
from pathlib import Path

import pytest

from edge.external import tofu_test
from edge.external.tofu import TofuError

MOCKS = 'mock_provider "cloudflare" {}\n'
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


COMMENTED_MOCK = '# mock_provider "cloudflare" {}\n'
ALIASED_MOCK = 'mock_provider "cloudflare" {\n  alias = "fake"\n}\n'
LATE_ALIAS = 'mock_provider "cloudflare" {\n  mock_data "cloudflare_zone" {}\n  alias = "fake"\n}\n'
ALIASED_REAL_PROVIDER = (
    MOCKS + 'provider "github" {\n  alias = "real"\n}\n'
    'run "r" {\n  providers = {\n    github = github.real\n  }\n}\n'
)
REAL_PROVIDER = MOCKS + 'provider "github" {\n  owner = "edge-fixture.invalid"\n}\n'
UNQUOTED_REAL_PROVIDER = MOCKS + 'provider github {\n  owner = "edge-fixture.invalid"\n}\n'
HELPER_PROVIDER = 'provider "github" {}\n'
HELPER_LOADED = MOCKS + 'run "r" {\n  module {\n    source = "./helper"\n  }\n}\n'
RUN_WITH_PROVIDERS = MOCKS + 'run "r" {\n  providers = {\n    github = github\n  }\n}\n'
OUTSIDE_MODULE = MOCKS + 'run "r" {\n  module {\n    source = "../.."\n  }\n}\n'


@pytest.mark.parametrize(
    ("files", "named_file", "named_reason"),
    [
        ({"tests/a.tftest.hcl": 'mock_provider "github" {}\n'}, "a.tftest.hcl", "cloudflare"),
        ({"tests/a.tftest.hcl": COMMENTED_MOCK}, "a.tftest.hcl", "cloudflare"),
        ({"tests/a.tftest.hcl": ALIASED_MOCK}, "a.tftest.hcl", "alias"),
        ({"tests/a.tftest.hcl": LATE_ALIAS}, "a.tftest.hcl", "alias"),
        ({"tests/a.tftest.hcl": ALIASED_REAL_PROVIDER}, "a.tftest.hcl", "real provider"),
        ({"tests/a.tftest.hcl": REAL_PROVIDER}, "a.tftest.hcl", "real provider"),
        ({"tests/a.tftest.hcl": OUTSIDE_MODULE}, "a.tftest.hcl", "loads a module"),
        ({"tests/a.tftest.hcl": UNQUOTED_REAL_PROVIDER}, "a.tftest.hcl", "real provider"),
        (
            {"tests/helper/main.tf": HELPER_PROVIDER, "tests/a.tftest.hcl": HELPER_LOADED},
            "a.tftest.hcl",
            "loads a module",
        ),
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
        "no cloudflare mock", "commented mock", "aliased mock",
        "alias after a nested block",
        "aliased real provider", "real provider beside the mocks", "module outside tests",
        "unquoted real provider", "provider in a helper module",
        "live org in a fixture", "live zone in a test",
        "test in the root folder", "tofutest form", "json form",
    ],
)  # fmt: skip
def test_refuses_a_test_that_could_reach_a_live_account(tmp_path, files, named_file, named_reason):
    """#123 S3/S4: no test file tofu runs may reach a live account."""
    problems = tofu_test.refusals(write_tree(tmp_path, files))

    assert problems
    assert all(named_file in problem for problem in problems)
    assert any(named_reason in problem for problem in problems)


def test_accepts_mocked_tests_with_fixture_names(tmp_path):
    """#123 S3: mocked tests with invalid fixture names pass the guard."""
    files = {"tests/a.tftest.hcl": MOCKS, "tests/fixtures/empty.yaml": "org: edge-fixture.invalid"}

    assert tofu_test.refusals(write_tree(tmp_path, files)) == []


@pytest.mark.parametrize(
    "files",
    [
        {"tests/a.tftest.hcl": RUN_WITH_PROVIDERS},
        {"tests/a.tftest.hcl": MOCKS, "tests/fixtures/hosts.yaml": "reason: the apex's alias"},
    ],
    ids=["run block selecting a mocked provider", "alias in a fixture"],
)
def test_accepts_what_cannot_select_an_unmocked_provider(tmp_path, files):
    """#123 S4: only a test file's own provider and alias lines can reach a provider."""
    assert tofu_test.refusals(write_tree(tmp_path, files)) == []


def test_the_real_tests_pass_their_own_guards():
    """#123 S3: the repository's own tests satisfy the guard."""
    assert tofu_test.refusals(tofu_test.EXTERNAL_ROOT) == []


def test_live_names_come_from_the_fleet_file(tmp_path):
    """#123 S4: the guarded zone and org are the ones fleet.yaml names."""
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text("cloudflare:\n  zone: example.test\ngithub:\n  org: Acme\n")

    assert tofu_test.live_names(fleet) == ("acme", "example.test")


def test_environment_is_exactly_the_allowlist(tmp_path):
    """#123 S5: tofu sees exactly the allowlist."""
    environment = tofu_test.test_environment(
        tmp_path / "bin" / "tofu", tmp_path / "home", tmp_path / "tmp"
    )

    assert set(environment) == {
        "PATH", "HOME", "LANG", "TMPDIR", "TF_INPUT", "TF_CLI_CONFIG_FILE",
        "TF_VAR_state_passphrase",
    }  # fmt: skip
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
    assert seen["HOME"] != str(tmp_path / "real-home")
    assert not os.path.exists(seen["HOME"])


def _fake_tofu(tmp_path: Path, exit_code: int) -> Path:
    fake = tmp_path / "tofu"
    fake.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def _raising(error: TofuError):
    def fail(*_arguments) -> None:
        raise error

    return fail


@pytest.mark.parametrize(
    ("arguments", "locate", "check_version", "exit_code", "stderr_names"),
    [
        (["root"], None, None, 2, "usage"),
        ([], _raising(TofuError("tofu is not on PATH")), None, 2, "not on PATH"),
        ([], None, _raising(TofuError("the pin is")), 2, "the pin is"),
        ([], None, None, 0, ""),
    ],
    ids=["usage error", "tofu is missing", "tofu is not the pin", "init and test succeed"],
)
def test_main_exits_with_the_outcome(
    tmp_path, monkeypatch, capsys, arguments, locate, check_version, exit_code, stderr_names
):
    """#123 S3: main reports usage and tofu problems with 2 and success with 0."""
    monkeypatch.setattr(tofu_test.tofu, "locate_tofu", locate or (lambda: _fake_tofu(tmp_path, 0)))
    monkeypatch.setattr(
        tofu_test.tofu, "require_pinned_version", check_version or (lambda *_arguments: None)
    )

    assert tofu_test.main(arguments) == exit_code
    assert stderr_names in capsys.readouterr().err
