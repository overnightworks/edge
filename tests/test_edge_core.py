"""The core of the stack and its applied record (#85 rules 1, 2, 6): which files of the core are
recorded, their digests, and `applied.json`, over a `tmp_path` checkout and state directory."""

import hashlib
import json
from pathlib import Path

import pytest

from edge import core

TRAEFIK_FILE = "traefik.yml"


def bind(source: Path, target: str) -> dict:
    """A bind as `docker compose config --format json` resolves the short syntax `./x:/y:ro`."""
    return {
        "type": "bind",
        "source": str(source),
        "target": target,
        "read_only": True,
        "bind": {"create_host_path": True},
    }


def bind_core_files(config: dict, checkout: Path) -> dict:
    """compose.yaml's mounts of socket-proxy and traefik, resolved for `checkout` (cell P7)."""
    services = config["services"]
    services["socket-proxy"]["volumes"] = [
        bind(Path("/var/run/docker.sock"), "/var/run/docker.sock"),
        {
            "type": "volume",
            "source": "socket-proxy-run",
            "target": "/run/socket-proxy",
            "volume": {},
        },
    ]
    services["traefik"]["volumes"] = [
        bind(checkout / TRAEFIK_FILE, "/etc/traefik/traefik.yml"),
        bind(checkout / "dynamic", "/dynamic"),
        {
            "type": "volume",
            "source": "socket-proxy-run",
            "target": "/run/socket-proxy",
            "read_only": True,
            "volume": {},
        },
    ]
    return config


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    tree = tmp_path / "git" / "edge"
    (tree / "dynamic").mkdir(parents=True)
    (tree / TRAEFIK_FILE).write_text("entryPoints: {}\n")
    return tree


@pytest.fixture
def config(checkout: Path) -> dict:
    return bind_core_files(
        {
            "services": {
                "socket-proxy": {},
                "traefik": {},
                "hello": {"profiles": ["probe"], "volumes": [bind(checkout / "x.yml", "/x")]},
            }
        },
        checkout,
    )


@pytest.fixture
def record(tmp_path: Path) -> core.Record:
    return core.Record(tmp_path / "state")


def test_the_core_is_every_service_without_a_profile(config):
    """#85 rule 1: the core is derived from the resolved configuration, never a second list."""
    assert core.core_services(config) == ("socket-proxy", "traefik")


def test_only_the_traefik_yml_of_the_core_is_a_file_bind(config, checkout):
    """#85 rule 1, cell P7: a bind outside the checkout, a directory bind and a volume are none."""
    file_binds = {
        service: core.file_binds(config["services"][service], checkout)
        for service in core.core_services(config)
    }

    assert file_binds == {"socket-proxy": (), "traefik": (checkout / TRAEFIK_FILE,)}


def test_a_missing_file_bind_cannot_be_planned_and_names_its_path(config, checkout, record):
    """#85 rule 1, cell U12: a deleted traefik.yml never passes silently."""
    (checkout / TRAEFIK_FILE).unlink()

    with pytest.raises(core.CoreError, match=str(checkout / TRAEFIK_FILE)):
        core.plan_core(config, {"traefik": {"running"}}, checkout, record)


def test_the_digest_is_the_sha256_of_the_files_bytes(checkout):
    """#85 rule 2: a file bind's digest is its content's sha256, keyed by its checkout path."""
    found = core.digests([checkout / TRAEFIK_FILE], checkout)

    assert found == {TRAEFIK_FILE: hashlib.sha256(b"entryPoints: {}\n").hexdigest()}


def write_record(record: core.Record, content: str) -> None:
    record.directory.mkdir(parents=True)
    record.path.write_text(content)


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (None, "traefik.yml has no applied record"),
        ("{}", "traefik.yml has no applied record"),
        ("not json", "applied.json is malformed"),
        ('{"traefik": "abc"}', "applied.json is malformed"),
        ('{"traefik": {"traefik.yml": "0000"}}', "traefik.yml changed"),
        ('{"traefik": {"old.yml": "0000"}}', "traefik.yml has no applied record"),
    ],
)
def test_a_record_that_does_not_match_counts_as_changed_and_says_why(
    content, why, config, checkout, record
):
    """#85 rule 2, cells U1, U3, U4: missing, malformed or other digests count as changed."""
    if content is not None:
        write_record(record, content)

    [plan] = core.plan_core(config, {"traefik": {"running"}}, checkout, record)

    assert plan.describe() == f"traefik: recreate ({why})"
    assert plan.digests == {TRAEFIK_FILE: sha256(checkout / TRAEFIK_FILE)}


def test_a_record_that_cannot_be_read_counts_as_changed(config, checkout, record):
    """#85 rule 2, cell U4: an unreadable record is changed, never a reason to stop."""
    record.path.mkdir(parents=True)  # a directory where the file belongs cannot be read

    [plan] = core.plan_core(config, {"traefik": {"running"}}, checkout, record)

    assert plan.why == "applied.json cannot be read (Is a directory)"


def test_an_entry_naming_another_file_beside_the_current_one_counts_as_changed(
    config, checkout, record
):
    """#85 rule 2, cell U4: an unknown key in the entry is changed, so a renamed bind is too."""
    digest = sha256(checkout / TRAEFIK_FILE)
    write_record(record, json.dumps({"traefik": {TRAEFIK_FILE: digest, "old.yml": digest}}))

    [plan] = core.plan_core(config, {"traefik": {"running"}}, checkout, record)

    assert plan.why == "applied.json names old.yml, which traefik does not mount"


def test_a_current_record_plans_nothing(config, checkout, record):
    """#85 rule 2, cell U2: the file as recorded is no change."""
    record.write("traefik", {TRAEFIK_FILE: sha256(checkout / TRAEFIK_FILE)})

    assert core.plan_core(config, {"traefik": {"running"}}, checkout, record) == []


@pytest.mark.parametrize(
    ("states", "action"),
    [
        ({"traefik": {"running"}}, core.CoreAction.RECREATE),
        ({"traefik": {"restarting"}}, core.CoreAction.RECREATE),
        ({"traefik": {"paused"}}, core.CoreAction.RECREATE),
        ({"traefik": {"exited"}}, core.CoreAction.NOT_STARTED),
        ({}, core.CoreAction.NOT_STARTED),
    ],
)
def test_only_a_core_service_with_a_live_container_is_recreated(
    states, action, config, checkout, record
):
    """#85 rules 3 and 4, cells U5, U6: running, restarting or paused is recreated; else named."""
    [plan] = core.plan_core(config, states, checkout, record)

    assert plan.action is action


def test_a_core_service_that_is_not_running_is_named_and_not_started(config, checkout, record):
    """#85 rule 4, cell U5: the wording of the non-action."""
    record.write("traefik", {TRAEFIK_FILE: "0000"})

    [plan] = core.plan_core(config, {}, checkout, record)

    assert plan.describe() == "traefik.yml changed; traefik is not running and is not started"


def test_the_record_is_written_into_a_new_state_directory_whole(record, tmp_path):
    """#85 rule 6, cell U15: the missing directory is created; only the finished file remains."""
    record.write("traefik", {TRAEFIK_FILE: "abc"})
    record.write("socket-proxy", {"proxy.yml": "def"})

    assert json.loads(record.path.read_text()) == {
        "socket-proxy": {"proxy.yml": "def"},
        "traefik": {TRAEFIK_FILE: "abc"},
    }
    assert [path.name for path in record.directory.iterdir()] == [core.APPLIED_FILE]


def test_writing_replaces_a_malformed_record(record):
    """#85 rule 6: an entry is written even where the record could not be trusted."""
    write_record(record, "not json")

    record.write("traefik", {TRAEFIK_FILE: "abc"})

    assert record.read() == core.Reading({"traefik": {TRAEFIK_FILE: "abc"}})
