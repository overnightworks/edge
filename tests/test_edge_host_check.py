"""The host check's comparison (#138 cells C and P1): dataclass fixtures, no network."""

import json
import re
from pathlib import Path

import pytest

from edge import catalog
from edge.catalog import Exposure
from edge.external import host_check
from edge.external.cloudflare_read import (
    SELF_HOSTED,
    AccessGroup,
    App,
    Include,
    Policy,
    Record,
    Snapshot,
)
from edge.external.host_check import Problem

ZONE = "example.test"
TUNNEL = "t-1"
TARGET = f"{TUNNEL}.cfargotunnel.com"
OPERATOR = AccessGroup("g-operator", "operator")
GROUPS = (OPERATOR, AccessGroup("g-staff", "staff"))
EXTERNAL = Path(__file__).resolve().parents[1] / "external"


def declared(name: str, exposure: Exposure) -> catalog.Host:
    fqdn = ZONE if name == "@" else f"{name}.{ZONE}"
    return catalog.Host("svc", name, fqdn, exposure, None, None, catalog.DEFAULT_TARGET)


def public(name: str) -> catalog.Host:
    return declared(name, Exposure.PUBLIC)


def bypass(name: str) -> catalog.Host:
    return declared(name, Exposure.BYPASS)


def protected_wildcard() -> catalog.Host:
    return declared("*", Exposure.PROTECTED)


def fqdn(label: str) -> str:
    return ZONE if label == "@" else f"{label}.{ZONE}"


def tunnel_record(label: str, content: str = TARGET, proxied: bool = True) -> Record:
    return Record(fqdn(label), "CNAME", content, proxied)


def address_record(label: str, kind: str = "A", content: str = "192.0.2.1") -> Record:
    return Record(fqdn(label), kind, content, True)


def policy(decision: str, *include: Include) -> Policy:
    return Policy(decision, include)


BYPASS_POLICY = policy("bypass", Include("everyone"))
OPERATOR_POLICY = policy("allow", Include("group", OPERATOR.id))


def app(*destinations: str, policies: tuple[Policy, ...] = (), kind: str = SELF_HOSTED) -> App:
    return App(kind, destinations, policies)


def bypass_app(label: str) -> App:
    return app(fqdn(label), policies=(BYPASS_POLICY,))


def operator_app() -> App:
    return app(f"*.{ZONE}", policies=(OPERATOR_POLICY,))


def snapshot(*records: Record, apps: tuple[App, ...] | None = (), groups=GROUPS) -> Snapshot:
    return Snapshot(TUNNEL, records, groups, apps)


def found(hosts, current: Snapshot) -> list[tuple[str, str]]:
    check = host_check.compare(ZONE, hosts, current)
    return [(finding.problem.value, finding.subject) for finding in check.findings]


LIVE_HOSTS = [public("@"), bypass("www"), protected_wildcard()]
LIVE = snapshot(
    tunnel_record("@"),
    tunnel_record("www"),
    tunnel_record("*"),
    apps=(bypass_app("www"), operator_app()),
)


def test_a_state_that_matches_the_catalog_has_no_findings_in_any_order():
    reordered = snapshot(
        *reversed(LIVE.records), apps=tuple(reversed(LIVE.apps)), groups=tuple(reversed(GROUPS))
    )
    forward = host_check.compare(ZONE, LIVE_HOSTS, LIVE)
    backward = host_check.compare(ZONE, list(reversed(LIVE_HOSTS)), reordered)
    assert forward.findings == () == backward.findings
    assert host_check.as_text(forward) == host_check.as_text(backward) == "no findings (3 hosts)"
    assert host_check.as_json(forward) == host_check.as_json(backward) == "[]"


def test_todays_live_state_reports_the_www_record_and_the_stray_one_and_notes_access_off():
    """#134 L1: www is a CNAME to the apex, presentator is a leftover, Access is off."""
    hosts = [public("@"), public("www")]
    live = snapshot(
        tunnel_record("@"),
        tunnel_record("www", content=ZONE),
        tunnel_record("presentator"),
        apps=None,
    )
    check = host_check.compare(ZONE, hosts, live)
    assert [(f.problem.value, f.subject) for f in check.findings] == [
        ("record-wrong", fqdn("www")),
        ("undeclared-record", fqdn("presentator")),
    ]
    assert host_check.ACCESS_NOT_ENABLED_NOTE in host_check.as_text(check)
    assert host_check.ACCESS_NOT_ENABLED_NOTE not in host_check.as_json(check)


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        pytest.param([], [Problem.RECORD_MISSING], id="no record"),
        pytest.param(
            [tunnel_record("www"), tunnel_record("www", "t-2.cfargotunnel.com")],
            [Problem.RECORD_WRONG],
            id="two records",
        ),
        pytest.param([address_record("www")], [Problem.RECORD_WRONG], id="an A record"),
        pytest.param([tunnel_record("www", proxied=False)], [Problem.RECORD_WRONG], id="unproxied"),
        pytest.param([tunnel_record("www", ZONE)], [Problem.RECORD_WRONG], id="a foreign name"),
        pytest.param(
            [tunnel_record("www", "t-2.cfargotunnel.com")],
            [Problem.RECORD_WRONG],
            id="other tunnel",
        ),
        pytest.param([tunnel_record("www")], [], id="the right tunnel"),
    ],
)
def test_a_declared_host_needs_exactly_one_proxied_cname_to_the_tunnel(records, expected):
    result = found([public("www")], snapshot(*records))
    assert result == [(problem.value, fqdn("www")) for problem in expected]


@pytest.mark.parametrize(
    ("apps", "expected"),
    [
        pytest.param((), [Problem.APP_MISSING], id="no app"),
        pytest.param(
            (app(fqdn("www"), policies=(policy("allow", Include("everyone")),)),),
            [Problem.POLICY_WRONG],
            id="an allow policy",
        ),
        pytest.param(
            (app(fqdn("www"), policies=(BYPASS_POLICY, BYPASS_POLICY)),),
            [Problem.POLICY_WRONG],
            id="two policies",
        ),
        pytest.param(
            (app(fqdn("www"), policies=(policy("bypass", Include("everyone"), Include("x")),)),),
            [Problem.POLICY_WRONG],
            id="more than everyone",
        ),
        pytest.param((bypass_app("www"),), [], id="a bypass app"),
    ],
)
def test_a_bypass_host_needs_its_own_app_with_one_bypass_policy_for_everyone(apps, expected):
    result = found([bypass("www")], snapshot(tunnel_record("www"), apps=apps))
    assert result == [(problem.value, fqdn("www")) for problem in expected]


@pytest.mark.parametrize(
    ("wildcard_policies", "groups", "expected"),
    [
        pytest.param((BYPASS_POLICY,), GROUPS, [Problem.POLICY_WRONG], id="a bypass policy"),
        pytest.param(
            (policy("allow", Include("group", "g-staff")),),
            GROUPS,
            [Problem.POLICY_WRONG],
            id="another group",
        ),
        pytest.param(
            (policy("allow", Include("group", "g-operator"), Include("group", "g-staff")),),
            GROUPS,
            [Problem.POLICY_WRONG],
            id="two groups",
        ),
        pytest.param(
            (OPERATOR_POLICY,), GROUPS[1:], [Problem.POLICY_WRONG], id="no operator group"
        ),
        pytest.param(
            (OPERATOR_POLICY,),
            (*GROUPS, AccessGroup("g-twin", "operator")),
            [Problem.POLICY_WRONG],
            id="two operator groups",
        ),
        pytest.param((OPERATOR_POLICY,), GROUPS, [], id="the operator group only"),
    ],
)
def test_a_protected_host_needs_one_allow_policy_for_the_operator_group_only(
    wildcard_policies, groups, expected
):
    wildcard = app(f"*.{ZONE}", policies=wildcard_policies)
    current = snapshot(tunnel_record("*"), apps=(wildcard,), groups=groups)
    result = found([protected_wildcard()], current)
    assert result == [(problem.value, fqdn("*")) for problem in expected]


@pytest.mark.parametrize(
    ("apps", "expected"),
    [
        pytest.param((bypass_app("www"),), 1, id="its own app"),
        pytest.param((app(f"*.{ZONE}", policies=(OPERATOR_POLICY,)),), 1, id="the wildcard app"),
        pytest.param((app("other." + ZONE, fqdn("www")),), 1, id="one of several destinations"),
        pytest.param((app(fqdn("www") + "/admin"),), 1, id="a path-scoped destination"),
        pytest.param((app(f"*.sub.{ZONE}"),), 0, id="a deeper wildcard"),
    ],
)
def test_a_public_host_must_not_be_covered_by_an_access_app(apps, expected):
    result = found([public("www")], snapshot(tunnel_record("www"), apps=apps))
    assert [problem for problem, _ in result if problem == "app-unexpected"] == [
        "app-unexpected"
    ] * expected


def test_the_public_apex_is_not_covered_by_the_wildcard_app():
    current = snapshot(tunnel_record("@"), apps=(operator_app(),))
    assert found([public("@"), protected_wildcard()], current) == [("record-missing", fqdn("*"))]


def test_two_apps_for_one_host_make_its_policy_undecidable():
    apps = (bypass_app("www"), bypass_app("www"))
    findings = host_check.compare(ZONE, [bypass("www")], snapshot(tunnel_record("www"), apps=apps))
    assert [(f.problem, "several apps" in f.detail) for f in findings.findings] == [
        (Problem.POLICY_WRONG, True)
    ]


def test_an_app_with_a_second_destination_still_is_the_hosts_own_app():
    own = app(fqdn("www"), fqdn("other"), policies=(BYPASS_POLICY,))
    assert found([bypass("www")], snapshot(tunnel_record("www"), apps=(own,))) == []


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        pytest.param(
            [address_record("stray"), tunnel_record("other")],
            [("undeclared-record", fqdn("other")), ("undeclared-record", fqdn("stray"))],
            id="an A and a CNAME",
        ),
        pytest.param([Record(fqdn("stray"), "TXT", "v=spf1", False)], [], id="a TXT"),
        pytest.param([Record(fqdn("@"), "MX", "mail.example", False)], [], id="an MX"),
        pytest.param([tunnel_record("_dmarc")], [], id="an underscore label"),
        pytest.param(
            [tunnel_record("disabled")],
            [("undeclared-record", fqdn("disabled"))],
            id="the host of a disabled entry",
        ),
    ],
)
def test_records_at_undeclared_names_are_reported_unless_they_are_not_hosts(extra, expected):
    result = found([public("@")], snapshot(tunnel_record("@"), *extra))
    assert result == expected


@pytest.mark.parametrize(
    ("stray_app", "expected"),
    [
        pytest.param(
            app("tool." + ZONE), [("undeclared-app", "tool." + ZONE)], id="in-zone domain"
        ),
        pytest.param(app(), [], id="no domain"),
        pytest.param(app("tool." + ZONE, kind="app_launcher"), [], id="app launcher"),
        pytest.param(app("tool." + ZONE, kind="warp"), [], id="warp"),
        pytest.param(app("elsewhere.invalid"), [], id="outside the zone"),
        pytest.param(app(fqdn("www"), policies=(BYPASS_POLICY,)), [], id="a declared host"),
    ],
)
def test_self_hosted_apps_nobody_declared_are_reported(stray_app, expected):
    current = snapshot(tunnel_record("www"), apps=(stray_app,))
    result = [item for item in found([bypass("www")], current) if item[0] == "undeclared-app"]
    assert result == expected


def test_access_off_is_one_finding_when_a_declared_host_needs_access():
    hosts = [bypass("www"), protected_wildcard(), public("@")]
    current = snapshot(tunnel_record("@"), address_record("stray"), apps=None)
    assert found(hosts, current) == [
        ("access-not-enabled", "account"),
        ("record-missing", fqdn("*")),
        ("record-missing", fqdn("www")),
        ("undeclared-record", fqdn("stray")),
    ]


def test_access_off_is_only_a_note_when_every_declared_host_is_public():
    check = host_check.compare(ZONE, [public("@")], snapshot(tunnel_record("@"), apps=None))
    assert (check.findings, check.access_unused) == ((), True)


def test_one_host_with_several_faults_is_sorted_with_one_finding_per_cause():
    hosts = [bypass("www"), public("@")]
    current = snapshot(
        address_record("zzz"), address_record("aaa"), tunnel_record("@", proxied=False), apps=()
    )
    assert found(hosts, current) == [
        ("record-missing", fqdn("www")),
        ("record-wrong", fqdn("@")),
        ("app-missing", fqdn("www")),
        ("undeclared-record", fqdn("aaa")),
        ("undeclared-record", fqdn("zzz")),
    ]


def test_every_finding_serialises_as_four_string_values():
    result = host_check.compare(ZONE, [public("www")], snapshot())
    document = json.loads(host_check.as_json(result))
    assert [set(item) for item in document] == [{"problem", "subject", "detail", "next"}]
    assert all(isinstance(value, str) for value in document[0].values())


def test_the_operator_group_and_address_types_are_those_of_the_tofu_root():
    """P1: one fact, two homes (#138 rule 14)."""
    locals_block = (EXTERNAL / "cloudflare.tf").read_text()
    group = re.search(r'operator_group_name\s*=\s*"([^"]+)"', locals_block)
    types = re.search(r"address_record_types\s*=\s*\[([^\]]*)\]", locals_block)
    assert group
    assert types
    assert group.group(1) == host_check.OPERATOR_GROUP_NAME
    assert re.findall(r'"(\w+)"', types.group(1)) == list(host_check.ADDRESS_RECORD_TYPES)
