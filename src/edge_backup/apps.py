"""The apps to back up, read from `backup/apps.toml`: structured records, never shell hooks."""

import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum

# Names end up in `docker` filters, restic tags and SQL identifiers, so they stay this plain.
_PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,62}")
_SQL_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_APP_KEYS = frozenset(
    {"compose_project", "db_service", "dump", "db_user", "db_name", "volume", "restore_test"}
)
_RESTORE_TEST_KEYS = frozenset({"count_table", "audio_reference_table", "audio_reference_column"})


class AppsError(ValueError):
    """`apps.toml` cannot be trusted; the German message names the offending entry."""


class DumpKind(StrEnum):
    PG_DUMP_CUSTOM = "pg_dump_custom"


@dataclass(frozen=True)
class RestoreTest:
    """What the restore test asks the restored database: a row count and the audio it names."""

    count_table: str
    audio_reference_table: str
    audio_reference_column: str


@dataclass(frozen=True)
class App:
    name: str
    compose_project: str
    db_service: str
    dump: DumpKind
    db_user: str
    db_name: str
    volume: str
    """The compose volume (`com.docker.compose.volume`), backed up read-only and scanned live."""
    restore_test: RestoreTest


def parse_apps(text: str) -> tuple[App, ...]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise AppsError(f"apps.toml ist kein gültiges TOML: {error}") from error
    if unknown := set(document) - {"app"}:
        raise AppsError(f"apps.toml: unbekannter Schlüssel {sorted(unknown)}")
    apps = document.get("app", {})
    if not isinstance(apps, dict) or not apps:
        raise AppsError("apps.toml deklariert keine App ([app.<name>])")
    return tuple(_parse_app(name, record) for name, record in apps.items())


def _parse_app(name: str, record: object) -> App:
    label = f"apps.toml [app.{name}]"
    if not _PLAIN_NAME.fullmatch(name):
        raise AppsError(f"{label}: der App-Name ist nicht schlicht genug")
    if not isinstance(record, dict):
        raise AppsError(f"{label}: muss eine Tabelle sein")
    _require_exact_keys(label, record, _APP_KEYS)
    dump = record["dump"]
    if not isinstance(dump, str) or dump not in {kind.value for kind in DumpKind}:
        raise AppsError(f"{label}: dump muss einer von {sorted(DumpKind)} sein, nicht {dump!r}")
    return App(
        name=name,
        compose_project=_plain(label, record, "compose_project"),
        db_service=_plain(label, record, "db_service"),
        dump=DumpKind(dump),
        db_user=_plain(label, record, "db_user"),
        db_name=_plain(label, record, "db_name"),
        volume=_plain(label, record, "volume"),
        restore_test=_parse_restore_test(f"{label}.restore_test", record["restore_test"]),
    )


def _parse_restore_test(label: str, record: object) -> RestoreTest:
    if not isinstance(record, dict):
        raise AppsError(f"{label}: muss eine Tabelle sein")
    _require_exact_keys(label, record, _RESTORE_TEST_KEYS)
    identifiers = {key: _identifier(label, record, key) for key in sorted(_RESTORE_TEST_KEYS)}
    return RestoreTest(**identifiers)


def _require_exact_keys(label: str, record: dict, keys: frozenset[str]) -> None:
    if unknown := set(record) - keys:
        raise AppsError(f"{label}: unbekannter Schlüssel {sorted(unknown)}")
    if missing := keys - set(record):
        raise AppsError(f"{label}: es fehlt {sorted(missing)}")


def _plain(label: str, record: dict, key: str) -> str:
    return _matching(label, record, key, _PLAIN_NAME)


def _identifier(label: str, record: dict, key: str) -> str:
    return _matching(label, record, key, _SQL_IDENTIFIER)


def _matching(label: str, record: dict, key: str, pattern: re.Pattern[str]) -> str:
    value = record[key]
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AppsError(f"{label}: {key} muss {pattern.pattern} entsprechen, nicht {value!r}")
    return value
