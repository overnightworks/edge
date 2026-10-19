"""The host check (#138): what Cloudflare actually holds against each host's declared exposure.

Pure over a `Snapshot`; `check_hosts` is the entry for callers (the command, later `apply` and
`edge start --all`). The `problem` values and the four finding keys are a contract for `hal`.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any

from edge import catalog
from edge.external.cloudflare_read import (
    SELF_HOSTED,
    App,
    CloudflareRead,
    Include,
    Policy,
    Record,
    Snapshot,
    destination_host,
)

OPERATOR_GROUP_NAME = "operator"
ADDRESS_RECORD_TYPES = ("A", "AAAA", "CNAME")
TUNNEL_TARGET_SUFFIX = ".cfargotunnel.com"
ACCOUNT = "account"
ACCESS_NOT_ENABLED_NOTE = "note: Access is not enabled on the account (no declared host needs it)"


class CheckExit(IntEnum):
    CLEAN = 0
    FINDINGS = 1
    CANNOT_CHECK = 2


class Problem(StrEnum):
    """In report order."""

    ACCESS_NOT_ENABLED = "access-not-enabled"
    RECORD_MISSING = "record-missing"
    RECORD_WRONG = "record-wrong"
    APP_MISSING = "app-missing"
    POLICY_WRONG = "policy-wrong"
    APP_UNEXPECTED = "app-unexpected"
    UNDECLARED_RECORD = "undeclared-record"
    UNDECLARED_APP = "undeclared-app"


_APPLY = "edge external apply"
_NEXT = {
    Problem.ACCESS_NOT_ENABLED: (
        "enable Zero Trust on the account in the Cloudflare dashboard and create the Access "
        f"group `{OPERATOR_GROUP_NAME}` (X8a's operator step); then `{_APPLY}`"
    ),
    Problem.RECORD_MISSING: f"`{_APPLY}`",
    Problem.RECORD_WRONG: (
        "`edge external plan`; nothing is converted (D12), so fix a foreign type by hand"
    ),
    Problem.APP_MISSING: f"`{_APPLY}`",
    Problem.POLICY_WRONG: f"`{_APPLY}`",
    Problem.APP_UNEXPECTED: ("remove the app, or declare the host `bypass` or `protected`"),
    Problem.UNDECLARED_RECORD: (
        "declare the host in `fleet.yaml`, or delete the record in the dashboard "
        "(`apply` never deletes it, #30 rule 7)"
    ),
    Problem.UNDECLARED_APP: "declare it, or delete the app in the dashboard",
}
_ORDER = {problem: position for position, problem in enumerate(Problem)}


@dataclass(frozen=True)
class Finding:
    problem: Problem
    subject: str
    detail: str

    @property
    def next(self) -> str:
        return _NEXT[self.problem]

    def as_json(self) -> dict[str, str]:
        return {
            "problem": self.problem.value,
            "subject": self.subject,
            "detail": self.detail,
            "next": self.next,
        }


@dataclass(frozen=True)
class HostCheck:
    findings: tuple[Finding, ...]
    host_count: int
    access_unused: bool = False

    @property
    def exit_code(self) -> CheckExit:
        return CheckExit.FINDINGS if self.findings else CheckExit.CLEAN


def check_hosts(document: Any, cloudflare: CloudflareRead) -> HostCheck:
    """Read the account once and compare it with the catalog's declared hosts."""
    zone = catalog.zone(document)
    return compare(zone, catalog.hosts(document), cloudflare.read(zone))


def compare(zone: str, hosts: Sequence[catalog.Host], snapshot: Snapshot) -> HostCheck:
    declared = {host.fqdn: host for host in hosts}
    findings = [finding for host in hosts for finding in _record_findings(host, snapshot)]
    hosts_without_record = {f.subject for f in findings if f.problem is Problem.RECORD_MISSING}
    findings += _undeclared_records(declared, snapshot.records)
    access_unused = False
    if snapshot.apps is None:
        needs_access = any(host.exposure is not catalog.Exposure.PUBLIC for host in hosts)
        access_unused = not needs_access
        if needs_access:
            findings.append(
                Finding(Problem.ACCESS_NOT_ENABLED, ACCOUNT, "Access is not enabled on the account")
            )
    else:
        findings += [
            finding
            for host in hosts
            if host.fqdn not in hosts_without_record
            for finding in _app_findings(zone, host, snapshot.apps, snapshot)
        ]
        findings += _undeclared_apps(zone, declared, snapshot.apps)
    ordered = sorted(findings, key=lambda f: (_ORDER[f.problem], f.subject, f.detail))
    return HostCheck(tuple(ordered), len(hosts), access_unused)


def _record_findings(host: catalog.Host, snapshot: Snapshot) -> list[Finding]:
    records = [record for record in _address_records(snapshot.records) if record.name == host.fqdn]
    if not records:
        return [Finding(Problem.RECORD_MISSING, host.fqdn, "no A, AAAA or CNAME record")]
    target = f"{snapshot.tunnel_id}{TUNNEL_TARGET_SUFFIX}"
    if len(records) == 1 and _is_proxied_cname_to(records[0], target):
        return []
    held = ", ".join(_describe_record(record) for record in sorted(records, key=_describe_record))
    return [
        Finding(
            Problem.RECORD_WRONG, host.fqdn, f"holds {held}; expected a proxied CNAME to {target}"
        )
    ]


def _is_proxied_cname_to(record: Record, target: str) -> bool:
    return (
        record.type == "CNAME"
        and record.proxied
        and record.content.lower().rstrip(".") == target.lower()
    )


def _describe_record(record: Record) -> str:
    proxy = "proxied" if record.proxied else "not proxied"
    return f"{record.type} {record.content} ({proxy})"


def _address_records(records: Sequence[Record]) -> list[Record]:
    return [record for record in records if record.type in ADDRESS_RECORD_TYPES]


def _undeclared_records(
    declared: dict[str, catalog.Host], records: Sequence[Record]
) -> list[Finding]:
    return [
        Finding(Problem.UNDECLARED_RECORD, record.name, f"{record.type} {record.content}")
        for record in _address_records(records)
        if record.name not in declared and not _is_service_label(record.name)
    ]


def _is_service_label(name: str) -> bool:
    return any(label.startswith("_") for label in name.split("."))


def _app_findings(
    zone: str, host: catalog.Host, apps: Sequence[App], snapshot: Snapshot
) -> list[Finding]:
    if host.exposure is catalog.Exposure.PUBLIC:
        covering = _covering_destinations(zone, host.fqdn, apps)
        if not covering:
            return []
        return [
            Finding(
                Problem.APP_UNEXPECTED, host.fqdn, f"covered by the Access app at {covering[0]}"
            )
        ]
    apps_here = [app for app in apps if _destinations_at(app, host.fqdn)]
    if not apps_here:
        return [Finding(Problem.APP_MISSING, host.fqdn, "no Access app has this host")]
    if len(apps_here) > 1:
        return [Finding(Problem.POLICY_WRONG, host.fqdn, "several apps have this host")]
    (own_app,) = apps_here
    if own_app.type != SELF_HOSTED:
        detail = "the app at this host is not a self-hosted app"
        return [Finding(Problem.POLICY_WRONG, host.fqdn, detail)]
    if scoped := [d for d in _destinations_at(own_app, host.fqdn) if "/" in d]:
        detail = f"the app is scoped to the path {scoped[0].removeprefix(host.fqdn)}"
        return [Finding(Problem.POLICY_WRONG, host.fqdn, detail)]
    faults = [
        f"a path rule makes {override.path_pattern} public, bypassing Access"
        for override in own_app.overrides
        if destination_host(override.destination) == host.fqdn
    ]
    if own_app.options_preflight_bypass:
        faults.append("OPTIONS requests bypass Access")
    if policy_fault := _policy_fault(host.exposure, own_app.policies, snapshot):
        faults.append(policy_fault)
    return [Finding(Problem.POLICY_WRONG, host.fqdn, fault) for fault in faults]


def _destinations_at(app: App, fqdn: str) -> list[str]:
    return [d for d in app.destinations if destination_host(d) == fqdn]


def _covering_destinations(zone: str, fqdn: str, apps: Sequence[App]) -> list[str]:
    return [
        destination
        for app in apps
        for destination in app.destinations
        if _covers(zone, destination_host(destination), fqdn)
    ]


def _covers(zone: str, covering_host: str, fqdn: str) -> bool:
    if covering_host == fqdn:
        return True
    one_label_under_zone = fqdn.endswith(f".{zone}") and "." not in fqdn.removesuffix(f".{zone}")
    return covering_host == f"*.{zone}" and one_label_under_zone


def _policy_fault(
    exposure: catalog.Exposure, policies: Sequence[Policy], snapshot: Snapshot
) -> str | None:
    if len(policies) != 1:
        return f"the app has {len(policies)} policies, expected exactly one"
    (policy,) = policies
    if exposure is catalog.Exposure.BYPASS:
        holds = policy.decision == "bypass" and policy.include == (Include("everyone"),)
        expected = "one bypass policy including everyone"
    else:
        holds = policy.decision == "allow" and _includes_only_the_operator_group(policy, snapshot)
        expected = f"one allow policy including only the group {OPERATOR_GROUP_NAME}"
    if not holds:
        return _describe_policy(expected, policy)
    return "the policy has extra conditions" if policy.extra_conditions else None


def _includes_only_the_operator_group(policy: Policy, snapshot: Snapshot) -> bool:
    operator_ids = [group.id for group in snapshot.groups if group.name == OPERATOR_GROUP_NAME]
    return (
        len(operator_ids) == 1
        and len(policy.include) == 1
        and policy.include[0] == Include("group", operator_ids[0])
    )


def _describe_policy(expected: str, policy: Policy) -> str:
    kinds = ", ".join(rule.kind for rule in policy.include) or "nothing"
    return f"the app's policy is {policy.decision} including {kinds}; expected {expected}"


def _undeclared_apps(
    zone: str, declared: dict[str, catalog.Host], apps: Sequence[App]
) -> list[Finding]:
    """Every in-zone destination that is neither a declared Access host nor a public host's."""
    access_hosts = {
        fqdn for fqdn, host in declared.items() if host.exposure is not catalog.Exposure.PUBLIC
    }
    public_hosts = [
        host.fqdn for host in declared.values() if host.exposure is catalog.Exposure.PUBLIC
    ]
    return [
        Finding(Problem.UNDECLARED_APP, destination, "an Access app nobody declared")
        for app in apps
        for destination in app.destinations
        if _in_zone(zone, destination_host(destination))
        and destination_host(destination) not in access_hosts
        and not any(_covers(zone, destination_host(destination), fqdn) for fqdn in public_hosts)
    ]


def _in_zone(zone: str, host: str) -> bool:
    return host == zone or host.endswith(f".{zone}")


def as_json(check: HostCheck) -> str:
    return json.dumps([finding.as_json() for finding in check.findings], indent=2)


def as_text(check: HostCheck) -> str:
    lines = [f"no findings ({check.host_count} hosts)"] if not check.findings else []
    for finding in check.findings:
        lines += [f"{finding.problem.value} {finding.subject}: {finding.detail}"]
        lines += [f"  next: {finding.next}"]
    if check.access_unused:
        lines.append(ACCESS_NOT_ENABLED_NOTE)
    return "\n".join(lines)
