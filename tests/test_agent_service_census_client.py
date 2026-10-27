"""The host side of the census (#457): what a mount shows of a skipped root, and that a failing
census relays nothing of its output."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from types import ModuleType

import pytest
from probe_support import probe_imports

KEY_MARKER = "KEY-MARKER-IN-STDERR"


@pytest.fixture(scope="module")
def census_client() -> Iterator[ModuleType]:
    with probe_imports():
        import census_client as module

        yield module


@pytest.mark.parametrize(
    ("listings", "skips", "listed"),
    [
        ((), (("refused", 1),), False),
        ((), (), True),
        ((("a", "file"),), (("refused", 1),), True),
        ((("a", "file"),), (), True),
    ],
    ids=["no listing and a skip", "no listing and no skip", "listings and a skip", "listings"],
)
def test_a_mount_is_listed_unless_it_shows_nothing_and_skipped_something(
    census_client, listings, skips, listed
):
    assert census_client.Mount(listings, skips).listed is listed


@pytest.mark.parametrize("returncode", [1, 0], ids=["exit status", "census error"])
def test_a_failing_census_relays_neither_stderr_nor_its_key(census_client, returncode):
    class Probe:
        def run(self, command, *, stdin, check, timeout):
            assert check is False
            stdout = '{"error": "failed"}\n' if returncode == 0 else ""
            return subprocess.CompletedProcess(command, returncode, stdout, KEY_MARKER)

    client = object.__new__(census_client.CensusClient)
    client.probe = Probe()
    client.container = "container"
    client.key = b"\x01" * 16
    with pytest.raises(census_client.ProbeFailed) as raised:
        client.ask({"mode": "census"})
    assert KEY_MARKER not in str(raised.value)
    assert client.key.hex() not in str(raised.value)
