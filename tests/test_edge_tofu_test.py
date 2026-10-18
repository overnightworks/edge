"""`tofu_test`: the OpenTofu tests run on the pinned tofu, in an exact environment, never live."""

import os
import stat
from pathlib import Path

import pytest

from edge.external import tofu_test

MOCKS = 'mock_provider "github" {}\nmock_provider "cloudflare" {}\n'
INHERITED_FROM_THE_CALLER = {
    "GITHUB_TOKEN": "caller-github-token",
    "GH_TOKEN": "caller-gh-token",
    "CLOUDFLARE_API_TOKEN": "caller-cloudflare-token",
    "TF_LOG": "TRACE",
    "TF_CLI_ARGS": "-no-color",
}


def write_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)
    return tmp_path


@pytest.mark.parametrize(
    ("files", "named_file", "named_reason"),
    [
        ({"a.tftest.hcl": 'mock_provider "cloudflare" {}\n'}, "a.tftest.hcl", "github"),
        ({"a.tftest.hcl": 'mock_provider "github" {}\n'}, "a.tftest.hcl", "cloudflare"),
        (
            {"a.tftest.hcl": MOCKS, "fixtures/org.yaml": "org: OvernightWorks"},
            "org.yaml",
            "overnightworks",
        ),
        ({"a.tftest.hcl": MOCKS + 'zone = "hallucinai.de"'}, "a.tftest.hcl", "hallucinai.de"),
    ],
    ids=["no github mock", "no cloudflare mock", "live org in a fixture", "live zone in a test"],
)
def test_refuses_a_test_that_could_reach_a_live_account(tmp_path, files, named_file, named_reason):
    problems = tofu_test.refusals(write_tree(tmp_path, files))

    assert len(problems) == 1
    assert named_file in problems[0]
    assert named_reason in problems[0]


def test_accepts_mocked_tests_with_fixture_names(tmp_path):
    files = {"a.tftest.hcl": MOCKS, "fixtures/empty.yaml": "org: edge-fixture.invalid"}

    assert tofu_test.refusals(write_tree(tmp_path, files)) == []


def test_the_real_tests_pass_their_own_guards():
    assert tofu_test.refusals(tofu_test.TESTS_DIRECTORY) == []


def test_environment_is_exactly_the_allowlist(tmp_path):
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
    tests_directory = write_tree(tmp_path, {"a.tftest.hcl": "run {}\n"})
    never_run = tmp_path / "tofu"

    assert tofu_test.run_tests(never_run, tests_directory) == 2
    assert "a.tftest.hcl" in capsys.readouterr().err


def test_tofu_gets_a_fresh_home_and_nothing_inherited(tmp_path, monkeypatch):
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
