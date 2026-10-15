"""`edge-check`: exit 0 when nothing reaches beyond loopback, 1 on findings, 2 if unable."""

import argparse
import json
import sys
from enum import IntEnum
from pathlib import Path

from edge_check import host
from edge_check.allowlist import AllowListError, parse_allow_list
from edge_check.check import check
from edge_check.exposure import Finding, UnreadableOutputError

DEFAULT_ALLOW_LIST = Path("edge-check.toml")
_TABLE_HEADER = ("PROBLEM", "SUBJECT", "PROTO", "ADDRESS", "PORT", "EDGE.NAME")
_NOT_A_SOCKET = "-"


class ExitCode(IntEnum):
    CLEAN = 0
    EXPOSED = 1
    CANNOT_CHECK = 2


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        allow_list = parse_allow_list(_read_allow_list(arguments.allow_list))
        findings = check(host.docker_inspect_json(), host.socket_listeners(), allow_list)
    except (AllowListError, host.HostCommandError, UnreadableOutputError) as error:
        print(f"edge-check: {error}", file=sys.stderr)
        return ExitCode.CANNOT_CHECK
    print(_as_json(findings) if arguments.json else _as_table(findings))
    return ExitCode.EXPOSED if findings else ExitCode.CLEAN


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="edge-check",
        description="Report Docker publications and host listeners that reach beyond loopback.",
    )
    parser.add_argument("--json", action="store_true", help="print the findings as a JSON list")
    parser.add_argument(
        "--allow-list",
        type=Path,
        default=DEFAULT_ALLOW_LIST,
        help=f"known listeners beyond loopback (default: {DEFAULT_ALLOW_LIST})",
    )
    return parser


def _read_allow_list(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise AllowListError(f"cannot read {path}: {error.strerror}") from error


def _as_json(findings: list[Finding]) -> str:
    return json.dumps([finding.as_json() for finding in findings], indent=2)


def _as_table(findings: list[Finding]) -> str:
    if not findings:
        return "edge-check: nothing reaches beyond loopback"
    rows = [_TABLE_HEADER] + [_table_row(finding) for finding in findings]
    widths = [max(len(row[column]) for row in rows) for column in range(len(_TABLE_HEADER))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def _table_row(finding: Finding) -> tuple[str, ...]:
    if finding.protocol is None:
        no_socket = (_NOT_A_SOCKET,) * 3
        return (finding.problem, finding.subject, *no_socket, finding.edge_name or "(none)")
    return (
        finding.problem,
        finding.subject,
        finding.protocol,
        finding.address or "(none)",
        "(random)" if finding.port is None else str(finding.port.as_json()),
        "",
    )
