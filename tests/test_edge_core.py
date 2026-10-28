"""The core of the stack and its applied record (#85 rules 1, 2, 6; #87 rules 1-3, 6): which files
of the core are recorded, their digests, `applied.json`, and which core services a changed compose
definition recreates in which order, over a `tmp_path` checkout and state directory."""

import dataclasses
import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from edge import compose, core

TRAEFIK_FILE = "traefik.yml"
CORE = ("socket-proxy", "traefik")
HASHES = {"socket-proxy": "a" * 64, "traefik": "b" * 64}  # `docker compose config --hash` now
STALE = "0" * 64  # a label from a definition the checkout no longer has
FOLLOWED = frozenset({"traefik"})


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
    services["traefik"]["depends_on"] = {
        "socket-proxy": {"condition": "service_started", "required": True}
    }
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


def containers(
    states: dict[str, str], labels: dict[str, str | None] | None = None
) -> list[compose.Container]:
    """One container per service in `states`; its label is the current hash unless `labels` says
    otherwise."""
    labels = HASHES | (labels or {})
    return [
        compose.Container(service, state, labels.get(service), "image")
        for service, state in states.items()
    ]


def plan_of(
    config: dict,
    checkout: Path,
    record: core.Record,
    states: dict[str, str] | None = None,
    labels: dict[str, str | None] | None = None,
) -> list[core.CorePlan]:
    """`plan_core` over these containers; by default only traefik runs, as created from now."""
    running = {"traefik": "running"} if states is None else states
    return core.plan_core(config, containers(running, labels), HASHES, checkout, record)


DEFINITION = core.Cause(core.CauseKind.DEFINITION_CHANGED)
UNFINISHED = core.Cause(core.CauseKind.UNFINISHED)


def file_changed(name: str) -> core.Cause:
    return core.Cause(core.CauseKind.FILE_CHANGED, name)


def file_unrecorded(name: str) -> core.Cause:
    return core.Cause(core.CauseKind.FILE_UNRECORDED, name)


def causes_of(plans: list[core.CorePlan]) -> list[tuple[str, tuple[core.Cause, ...]]]:
    return [(planned.service, planned.why) for planned in plans]


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

    with pytest.raises(core.CoreError, match=re.escape(str(checkout / TRAEFIK_FILE))):
        plan_of(config, checkout, record)


@pytest.fixture
def locked_directory(tmp_path: Path):
    """A directory this user cannot enter; it is opened again so pytest can remove it."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    yield locked
    locked.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters a directory with mode 0")
def test_a_file_bind_behind_a_directory_it_cannot_enter_cannot_be_planned_and_names_its_path(
    config, checkout, record, locked_directory
):
    """#85 rule 1: a traefik.yml linked into a directory edge cannot enter is cannot plan."""
    (checkout / TRAEFIK_FILE).unlink()
    (checkout / TRAEFIK_FILE).symlink_to(locked_directory / TRAEFIK_FILE)

    with pytest.raises(core.CoreError, match=re.escape(str(checkout / TRAEFIK_FILE))):
        plan_of(config, checkout, record)


def test_the_digest_is_the_sha256_of_the_files_bytes(checkout):
    """#85 rule 2: a file bind's digest is its content's sha256, keyed by its checkout path."""
    found = core.digests([checkout / TRAEFIK_FILE], checkout)

    assert found == {TRAEFIK_FILE: hashlib.sha256(b"entryPoints: {}\n").hexdigest()}


def write_record(record: core.Record, content: str) -> None:
    record.directory.mkdir(parents=True)
    record.path.write_text(content)


def untrusted(sentence: str) -> core.Cause:
    return core.Cause(core.CauseKind.RECORD_UNTRUSTED, f"RECORD is malformed: {sentence}")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, file_unrecorded(TRAEFIK_FILE)),
        ("{}", file_unrecorded(TRAEFIK_FILE)),
        ("not json", untrusted("it is not JSON")),
        ("[]", untrusted("it is not an object of services")),
        ('{"traefik": "abc"}', untrusted("the traefik entry is not an object of files")),
        (
            '{"traefik": {"traefik.yml": 1}}',
            untrusted("traefik.yml of traefik is not a digest string"),
        ),
        ('{"traefik": {"traefik.yml": "0000"}}', file_changed(TRAEFIK_FILE)),
        ('{"traefik": {"old.yml": "0000"}}', file_unrecorded(TRAEFIK_FILE)),
    ],
)
def test_a_record_that_does_not_match_counts_as_changed_and_says_why(
    content, expected, config, checkout, record
):
    """#85 rule 2, cells U1, U3, U4: missing, malformed or other digests count as changed."""
    if content is not None:
        write_record(record, content)

    [plan] = plan_of(config, checkout, record)

    assert plan.why == (
        dataclasses.replace(expected, subject=expected.subject.replace("RECORD", str(record.path))),
    )
    assert plan.digests == {TRAEFIK_FILE: sha256(checkout / TRAEFIK_FILE)}


def test_a_record_that_cannot_be_read_counts_as_changed(config, checkout, record):
    """#85 rule 2, cell U4: an unreadable record is changed, never a reason to stop."""
    record.path.mkdir(parents=True)  # a directory where the file belongs cannot be read

    [plan] = plan_of(config, checkout, record)

    assert plan.why == (
        core.Cause(
            core.CauseKind.RECORD_UNTRUSTED, f"{record.path} cannot be read (Is a directory)"
        ),
    )


def test_an_entry_naming_another_file_beside_the_current_one_counts_as_changed(
    config, checkout, record
):
    """#85 rule 2, cell U4: an unknown key in the entry is changed, so a renamed bind is too."""
    digest = sha256(checkout / TRAEFIK_FILE)
    write_record(record, json.dumps({"traefik": {TRAEFIK_FILE: digest, "old.yml": digest}}))

    [plan] = plan_of(config, checkout, record)

    assert plan.why == (core.Cause(core.CauseKind.RECORD_NAMES_OTHERS, "old.yml"),)


def test_a_current_record_plans_nothing(config, checkout, record):
    """#85 rule 2, cell U2: the file as recorded is no change."""
    record.write("traefik", {TRAEFIK_FILE: sha256(checkout / TRAEFIK_FILE)}, FOLLOWED)

    assert plan_of(config, checkout, record) == []


@pytest.mark.parametrize(
    ("states", "action"),
    [
        ({"traefik": "running"}, core.CoreAction.RECREATE),
        ({"traefik": "restarting"}, core.CoreAction.RECREATE),
        ({"traefik": "paused"}, core.CoreAction.RECREATE),
        ({"traefik": "exited"}, core.CoreAction.NOT_STARTED),
        ({}, core.CoreAction.NOT_STARTED),
    ],
)
def test_only_a_core_service_with_a_live_container_is_recreated(
    states, action, config, checkout, record
):
    """#85 rules 3 and 4, cells U5, U6: running, restarting or paused is recreated; else named."""
    [plan] = plan_of(config, checkout, record, states)

    assert plan.action is action


def test_a_core_service_that_is_not_running_is_named_with_its_cause_and_not_started(
    config, checkout, record
):
    """#85 rule 4, cell U5: the non-action keeps the cause."""
    record.write("traefik", {TRAEFIK_FILE: "0000"}, FOLLOWED)

    [plan] = plan_of(config, checkout, record, {})

    assert plan.action is core.CoreAction.NOT_STARTED
    assert plan.why == (file_changed(TRAEFIK_FILE),)


def test_the_record_is_written_into_a_new_state_directory_whole(record, tmp_path):
    """#85 rule 6, cell U15: the missing directory is created; only the finished file remains."""
    record.write("traefik", {TRAEFIK_FILE: "abc"}, CORE)
    record.write("socket-proxy", {"proxy.yml": "def"}, CORE)

    assert json.loads(record.path.read_text()) == {
        "socket-proxy": {"proxy.yml": "def"},
        "traefik": {TRAEFIK_FILE: "abc"},
    }
    assert [path.name for path in record.directory.iterdir()] == [core.APPLIED_FILE]


def test_writing_replaces_a_malformed_record(record):
    """#85 rule 6: an entry is written even where the record could not be trusted."""
    write_record(record, "not json")

    record.write("traefik", {TRAEFIK_FILE: "abc"}, FOLLOWED)

    assert record.read() == core.Reading({"traefik": {TRAEFIK_FILE: "abc"}})


@pytest.mark.parametrize(
    "keep",
    [
        lambda record: record.write("socket-proxy", {"proxy.yml": "def"}, FOLLOWED),
        lambda record: record.mark_unfinished(["traefik", "socket-proxy"], FOLLOWED),
    ],
    ids=["write", "mark unfinished"],
)
def test_the_record_keeps_no_entry_for_a_service_edge_does_not_follow(record, keep):
    """#87 rules 4, 6: socket-proxy binds no file, so whoever asks, the record keeps none for it."""
    keep(record)

    assert "socket-proxy" not in record.read().entries


# --- #87: the core follows its compose definition --------------------------------------------

BOTH_RUNNING = {"socket-proxy": "running", "traefik": "running"}


@pytest.fixture
def recorded(config, checkout, record) -> core.Record:
    """traefik.yml as last applied, so only a definition can be changed."""
    record.write("traefik", {TRAEFIK_FILE: sha256(checkout / TRAEFIK_FILE)}, FOLLOWED)
    return record


def test_a_changed_socket_proxy_definition_recreates_it_and_pulls_in_traefik(
    config, checkout, recorded
):
    """#87 rule 2, cell D1: socket-proxy's label differs from its hash, so socket-proxy is
    recreated, then traefik because it depends on it; socket-proxy is followed through no file."""
    plans = plan_of(config, checkout, recorded, BOTH_RUNNING, {"socket-proxy": STALE})

    assert causes_of(plans) == [
        ("socket-proxy", (DEFINITION,)),
        ("traefik", (core.Cause(core.CauseKind.PULLED_IN, "socket-proxy"),)),
    ]
    assert [planned.digests for planned in plans] == [
        {},
        {TRAEFIK_FILE: sha256(checkout / TRAEFIK_FILE)},
    ]


def test_a_changed_traefik_definition_recreates_only_traefik(config, checkout, recorded):
    """#87 rule 2, cell D2: nothing depends on traefik, so socket-proxy is not named."""
    plans = plan_of(config, checkout, recorded, BOTH_RUNNING, {"traefik": STALE})

    assert causes_of(plans) == [("traefik", (DEFINITION,))]


def test_a_changed_definition_and_traefik_yml_are_one_recreate_with_both_reasons(
    config, checkout, recorded
):
    """#87 rule 2, cell D4: one plan line, with the plan-time digest of traefik.yml."""
    (checkout / TRAEFIK_FILE).write_text("entryPoints: {edge: {}}\n")

    [planned] = plan_of(config, checkout, recorded, BOTH_RUNNING, {"traefik": STALE})

    assert planned.why == (DEFINITION, file_changed(TRAEFIK_FILE))
    assert planned.digests == {TRAEFIK_FILE: sha256(checkout / TRAEFIK_FILE)}


def test_socket_proxy_comes_first_though_the_config_lists_traefik_first(config, checkout, recorded):
    """#87 rule 2, cell D10: the order comes from `depends_on`, not from file order."""
    services = config["services"]
    config["services"] = {"traefik": services["traefik"], "socket-proxy": services["socket-proxy"]}

    plans = plan_of(config, checkout, recorded, BOTH_RUNNING, {"socket-proxy": STALE})

    assert [planned.service for planned in plans] == ["socket-proxy", "traefik"]


@pytest.mark.parametrize(
    ("state", "proxy_emptied"), [("exited", False), ("created", False), ("created", True)]
)
def test_a_changed_socket_proxy_that_is_not_running_is_named_and_pulls_in_nothing(
    state, proxy_emptied, config, checkout, recorded
):
    """#87 rules 3 and 6, cell D5: a stopped core service is never started, so its dependents
    keep running as they are; so is one created by hand, which no recreate of edge's left, even
    beside an emptied socket-proxy entry, which edge follows through no file and so ignores."""
    if proxy_emptied:
        entries = json.loads(recorded.path.read_text()) | {"socket-proxy": {}}
        recorded.path.write_text(json.dumps(entries))
    states = {"socket-proxy": state, "traefik": "running"}

    plans = plan_of(config, checkout, recorded, states, {"socket-proxy": STALE})

    assert causes_of(plans) == [("socket-proxy", (DEFINITION,))]
    assert plans[0].action is core.CoreAction.NOT_STARTED
    assert not plans[0].left_unstarted


@pytest.mark.parametrize(
    ("label", "why"),
    [(None, (UNFINISHED,)), (STALE, (DEFINITION, UNFINISHED))],
)
def test_a_socket_proxy_a_recreate_left_created_is_recreated_again_and_pulls_in_traefik(
    label, why, config, checkout, recorded
):
    """#87 rule 4: a container created and never started while the record says the recreate that
    took it in did not finish is what edge's recreate whose start failed leaves, with the new
    label; nobody stopped it, so the next run recreates it again, whether the definition was fixed
    since or not. socket-proxy keeps no entry; traefik's, emptied with it, speaks for it. The
    plan says which it found so, and apply's liveness read asks it (#93 rule 2)."""
    recorded.mark_unfinished(["traefik"], FOLLOWED)
    states = {"socket-proxy": "created", "traefik": "running"}
    labels = {"socket-proxy": label} if label else {}

    plans = plan_of(config, checkout, recorded, states, labels)

    assert causes_of(plans) == [
        ("socket-proxy", why),
        ("traefik", (UNFINISHED, core.Cause(core.CauseKind.PULLED_IN, "socket-proxy"))),
    ]
    assert [planned.left_unstarted for planned in plans] == [True, False]


def test_a_traefik_its_recreate_left_created_is_recreated_again_for_one_reason(
    config, checkout, recorded
):
    """#87 rule 4: a traefik whose start failed, as a taken port 80 leaves it, is created and
    marked unfinished; the next run recreates it once, and socket-proxy is not pulled in."""
    recorded.mark_unfinished(["traefik"], FOLLOWED)
    states = {"socket-proxy": "running", "traefik": "created"}

    plans = plan_of(config, checkout, recorded, states)

    assert causes_of(plans) == [("traefik", (UNFINISHED,))]
    assert plans[0].left_unstarted


def test_an_absent_core_container_has_no_definition_to_compare(config, checkout, recorded):
    """#87 rule 3, cell D6: no container, no label, no line; a live one is still compared."""
    assert plan_of(config, checkout, recorded, {"traefik": "running"}) == []
    assert plan_of(config, checkout, recorded, {}) == []


def test_a_core_container_without_the_label_cannot_be_planned(config, checkout, recorded):
    """#87 rule 1, cell D11: a silent fallback would be a change on every run (P15)."""
    with pytest.raises(
        core.CoreError, match=r"a traefik container has no com\.docker\.compose\.config-hash label"
    ):
        plan_of(config, checkout, recorded, BOTH_RUNNING, {"traefik": None})


def test_a_unit_container_without_the_label_is_no_core_concern(config, checkout, recorded):
    """#87 rule 1: only the core's labels are read."""
    states = BOTH_RUNNING | {"hello": "running"}

    assert plan_of(config, checkout, recorded, states, {"hello": None}) == []


def test_a_ghost_entry_is_ignored_and_dropped_at_the_next_write_while_a_followed_one_stays(
    config, checkout, record
):
    """#87 rule 6, cell D13 (#34 finding 51): the prune goes by the services edge follows in the
    config, of every profile, so traefik-mcp's entry survives while step-ca is off."""
    config["services"]["traefik-mcp"] = {
        "profiles": ["step-ca"],
        "volumes": [bind(checkout / "traefik-mcp.yml", "/etc/traefik/traefik.yml")],
    }
    digest = sha256(checkout / TRAEFIK_FILE)
    write_record(
        record,
        json.dumps({"traefik": {TRAEFIK_FILE: digest}, "ghost": {}, "traefik-mcp": {"x.yml": "1"}}),
    )
    followed = core.followed_services(config, checkout)

    assert plan_of(config, checkout, record, BOTH_RUNNING) == []
    record.mark_unfinished(["traefik"], followed)
    assert record.read().entries == {"traefik": {}, "traefik-mcp": {"x.yml": "1"}}
    assert followed == {"traefik", "traefik-mcp", "hello"}


# --- #89: traefik-mcp follows traefik-mcp.yml and its definition while step-ca is on ----------

FOLLOWER = "traefik-mcp"
FOLLOWER_FILE = "traefik-mcp.yml"
FOLLOWER_HASHES = HASHES | {FOLLOWER: "c" * 64}
ON = {"step-ca": core.Gate.ON}


@pytest.fixture
def with_follower(config: dict, checkout: Path) -> dict:
    """compose.yaml's step-ca profile as resolved: traefik-mcp binds its static config at
    Traefik's path and `mcp/dynamic` as a directory; step-ca binds ca.json elsewhere."""
    (checkout / FOLLOWER_FILE).write_text("entryPoints: {}\n")
    (checkout / "mcp" / "dynamic").mkdir(parents=True)
    (checkout / "ca.json").write_text("{}\n")
    config["services"]["step-ca"] = {
        "profiles": ["step-ca"],
        "volumes": [bind(checkout / "ca.json", "/home/step/config/ca.json")],
    }
    config["services"][FOLLOWER] = {
        "profiles": ["step-ca"],
        "depends_on": {"step-ca": {"condition": "service_started"}},
        "volumes": [
            bind(checkout / FOLLOWER_FILE, "/etc/traefik/traefik.yml"),
            bind(checkout / "mcp" / "dynamic", "/mcp/dynamic"),
        ],
    }
    config["services"]["keycloak"] = {"profiles": ["keycloak"]}
    return config


def followers_of(
    config: dict,
    checkout: Path,
    record: core.Record,
    states: dict[str, str],
    gates: dict[str, core.Gate],
    labels: dict[str, str | None] | None = None,
) -> list[core.FollowerPlan]:
    found = [
        compose.Container(service, state, (FOLLOWER_HASHES | (labels or {})).get(service), "i")
        for service, state in states.items()
    ]
    return core.plan_followers(config, found, FOLLOWER_HASHES, checkout, record, gates)


def test_the_followers_are_the_profile_services_binding_traefiks_static_config(
    with_follower, checkout
):
    """#89 head decision, cell F16: exactly traefik-mcp; traefik is core, step-ca binds ca.json
    elsewhere, and the `mcp/dynamic` directory bind is no file it follows."""
    assert core.followers(with_follower) == (FOLLOWER,)
    assert core.file_binds(with_follower["services"][FOLLOWER], checkout) == (
        checkout / FOLLOWER_FILE,
    )


@pytest.mark.parametrize(
    ("states", "gate", "action"),
    [
        ({FOLLOWER: "running"}, core.Gate.ON, core.FollowerAction.RECREATE),
        ({FOLLOWER: "restarting"}, core.Gate.ON, core.FollowerAction.RECREATE),
        ({}, core.Gate.ON, core.FollowerAction.STARTS_WITH_UNIT),
        ({}, core.Gate.OFF, core.FollowerAction.HELD),
        ({FOLLOWER: "running"}, core.Gate.SWITCHED_OFF, core.FollowerAction.HELD),
    ],
    ids=["F2 on", "F10 crash-looping", "F12 switched on", "F5 off", "F7 switched off"],
)
def test_a_changed_follower_is_planned_by_its_units_gate(
    states, gate, action, with_follower, checkout, record
):
    """#89 rules 2-4, cells F2, F5, F7, F10, F12: a follower with any container whose unit is on
    is recreated, one without starts with its unit, and an off or switched-off unit holds it."""
    [plan] = followers_of(with_follower, checkout, record, states, {"step-ca": gate})

    assert plan.action is action
    assert plan.why == (file_unrecorded(FOLLOWER_FILE),)
    assert plan.digests == {FOLLOWER_FILE: sha256(checkout / FOLLOWER_FILE)}


def test_a_follower_as_recorded_and_labelled_plans_nothing(with_follower, checkout, record):
    """#89 rule 2, cell F4: P15."""
    record.write(FOLLOWER, {FOLLOWER_FILE: sha256(checkout / FOLLOWER_FILE)}, {FOLLOWER})

    assert followers_of(with_follower, checkout, record, {FOLLOWER: "running"}, ON) == []


def test_a_followers_changed_label_is_its_definition_and_step_cas_is_none_of_its_concern(
    with_follower, checkout, record
):
    """#89 rule 2, cell F3: only the follower's own label is compared."""
    record.write(FOLLOWER, {FOLLOWER_FILE: sha256(checkout / FOLLOWER_FILE)}, {FOLLOWER})
    states = {FOLLOWER: "running", "step-ca": "running"}

    [plan] = followers_of(
        with_follower, checkout, record, states, ON, {FOLLOWER: STALE, "step-ca": STALE}
    )

    assert plan.why == (DEFINITION,)


def test_a_follower_without_a_container_gets_no_definition_line(with_follower, checkout, record):
    """#89 rule 2: no container, no label to compare."""
    record.write(FOLLOWER, {FOLLOWER_FILE: sha256(checkout / FOLLOWER_FILE)}, {FOLLOWER})

    assert followers_of(with_follower, checkout, record, {}, {"step-ca": core.Gate.OFF}) == []


def test_a_follower_whose_profile_is_no_catalog_unit_is_skipped(with_follower, checkout, record):
    """#89 rule 5, cell F14: no line, no hash asked, whatever the file holds."""
    (checkout / FOLLOWER_FILE).unlink()

    assert core.follower_units(with_follower, {}) == {}
    assert followers_of(with_follower, checkout, record, {FOLLOWER: "running"}, {}) == []


@pytest.mark.parametrize("gate", list(core.Gate))
def test_a_missing_follower_file_cannot_be_planned_and_names_its_path(
    gate, with_follower, checkout, record
):
    """#89 rule 6, cell F15: whether step-ca is on, off or switched off."""
    (checkout / FOLLOWER_FILE).unlink()

    with pytest.raises(core.CoreError, match=re.escape(str(checkout / FOLLOWER_FILE))):
        followers_of(with_follower, checkout, record, {FOLLOWER: "running"}, {"step-ca": gate})


def test_the_checkouts_binds_are_read_by_what_edge_apply_does_with_them(with_follower, checkout):
    """#98 rule 4, B13 (C2): the core's and the followers' directory binds are live, their file
    binds recreated; every bind of another service is a unit's; a bind outside the checkout is
    none. All from the resolved configuration, never a second list."""
    (checkout / "keycloak").mkdir()
    (checkout / "keycloak" / "entrypoint.sh").touch()
    with_follower["services"]["keycloak"]["volumes"] = [
        bind(checkout / "keycloak" / "entrypoint.sh", "/opt/edge/entrypoint.sh"),
        bind(checkout / "keycloak", "/config"),
    ]

    binds = core.checkout_binds(with_follower, checkout, {"step-ca", "keycloak"})

    assert binds == core.CheckoutBinds(
        live=frozenset({"dynamic", "mcp/dynamic"}),
        recreated=frozenset({TRAEFIK_FILE, FOLLOWER_FILE}),
        units=frozenset({"x.yml", "ca.json", "keycloak/entrypoint.sh", "keycloak"}),
    )


def test_a_follower_whose_profile_is_no_catalog_unit_has_no_applied_binds(with_follower, checkout):
    """#89 rule 5 with #98 rule 4: what edge apply never plans, it does not recreate."""
    binds = core.checkout_binds(with_follower, checkout, {"keycloak"})

    assert binds.recreated == frozenset({TRAEFIK_FILE})
    assert {FOLLOWER_FILE, "mcp/dynamic"} <= binds.units


@pytest.mark.parametrize(
    ("path", "binds", "bound"),
    [
        pytest.param("dynamic/x.yml", {"dynamic"}, True, id="under a directory bind"),
        pytest.param("dynamic", {"dynamic"}, True, id="the bind itself"),
        pytest.param("dynamic-old/x.yml", {"dynamic"}, False, id="a sibling sharing the prefix"),
        pytest.param("README.md", {"."}, True, id="the whole checkout bound"),
        pytest.param("README.md", set(), False, id="nothing bound"),
    ],
)  # fmt: skip
def test_a_changed_path_is_bound_when_it_lies_at_or_under_a_bind(path, binds, bound):
    """#98 rule 4: a file is bound by a bind of itself or of a directory it lies in."""
    assert core.is_bound(path, frozenset(binds)) is bound
