"""The exposure check as one typed entry: the allow-list path in, the findings out, and the
findings as JSON objects."""

from pathlib import Path

from edge_check import host
from edge_check.allowlist import AllowListError, parse_allow_list
from edge_check.check import check
from edge_check.exposure import Finding, UnreadableOutputError

DEFAULT_ALLOW_LIST = Path(__file__).with_name("allow-list.toml")


class ExposureCheckError(Exception):
    """The check could not vouch for anything; the message names the cause."""


def inspect(allow_list_path: Path = DEFAULT_ALLOW_LIST) -> list[Finding]:
    try:
        allow_list = parse_allow_list(_read_allow_list(allow_list_path))
        return check(host.docker_inspect_json(), host.socket_listeners(), allow_list)
    except (AllowListError, host.HostCommandError, UnreadableOutputError) as error:
        raise ExposureCheckError(str(error)) from error


def _read_allow_list(path: Path) -> str:
    try:
        path.stat()
        resolved = path.resolve()
        if not resolved.is_file():
            raise AllowListError(f"cannot read {path}: not a regular file")
        return resolved.read_text(encoding="utf-8")
    except OSError as error:
        raise AllowListError(f"cannot read {path}: {error.strerror}") from error


def as_json_objects(findings: list[Finding]) -> list[dict[str, str | int | None]]:
    return [finding.as_json() for finding in findings]
