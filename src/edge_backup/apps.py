"""The apps to back up, read from `backup/apps.toml`: structured records, never shell hooks."""

import re
import tomllib
from dataclasses import dataclass
from enum import IntEnum, StrEnum

# Names end up in `docker` filters, restic tags and SQL identifiers, so they stay this plain.
_PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,62}")
_SQL_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_APP_KEYS = frozenset(
    {
        "compose_project",
        "db_service",
        "dump",
        "db_user",
        "db_name",
        "postgres_major",
        "restore_test",
    }
)
_AUDIO_VOLUME_KEY = "volume"
_AUDIO_REFERENCE_KEYS = frozenset({"audio_reference_table", "audio_reference_column"})
_RESTORE_TEST_KEYS = frozenset({"count_table"})


class AppsError(ValueError):
    """`apps.toml` cannot be trusted; the German message names the offending entry."""


class DumpKind(StrEnum):
    PG_DUMP_CUSTOM = "pg_dump_custom"


class PostgresMajor(IntEnum):
    """The server major an app's dump needs: a custom dump only restores into the same major."""

    SIXTEEN = 16
    SEVENTEEN = 17


@dataclass(frozen=True)
class Audio:
    """The audio an app keeps beside its database: the volume and the references to its files."""

    volume: str
    """The compose volume (`com.docker.compose.volume`), backed up read-only and scanned live."""
    reference_table: str
    reference_column: str
    """Names files relative to the volume's root."""


@dataclass(frozen=True)
class RestoreTest:
    """What the restore test asks the restored database: a row count, and the audio it names."""

    count_table: str


@dataclass(frozen=True)
class App:
    name: str
    compose_project: str
    db_service: str
    dump: DumpKind
    db_user: str
    db_name: str
    postgres_major: PostgresMajor
    restore_test: RestoreTest
    audio: Audio | None
    """None for a DB-only app: its snapshot holds the dump and manifest and mounts no volume."""


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
    _require_exact_keys(label, record, _APP_KEYS, optional=frozenset({_AUDIO_VOLUME_KEY}))
    restore_test_record = _table(f"{label}.restore_test", record["restore_test"])
    restore_test = _parse_restore_test(f"{label}.restore_test", restore_test_record)
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
        postgres_major=_postgres_major(label, record),
        restore_test=restore_test,
        audio=_parse_audio(label, record, restore_test_record),
    )


def _postgres_major(label: str, record: dict) -> PostgresMajor:
    value = record["postgres_major"]
    if value not in {major.value for major in PostgresMajor}:
        raise AppsError(
            f"{label}: postgres_major muss einer von {[m.value for m in PostgresMajor]} sein, "
            f"nicht {value!r}"
        )
    return PostgresMajor(value)


def _table(label: str, record: object) -> dict:
    if not isinstance(record, dict):
        raise AppsError(f"{label}: muss eine Tabelle sein")
    return record


def _parse_restore_test(label: str, record: dict) -> RestoreTest:
    _require_exact_keys(label, record, _RESTORE_TEST_KEYS, optional=_AUDIO_REFERENCE_KEYS)
    return RestoreTest(count_table=_identifier(label, record, "count_table"))


def _parse_audio(label: str, record: dict, restore_test_record: dict) -> Audio | None:
    """Audio is all or nothing: references name files in a volume, a volume needs references."""
    declared = {_AUDIO_VOLUME_KEY} & set(record) | _AUDIO_REFERENCE_KEYS & set(restore_test_record)
    if not declared:
        return None
    if declared != {_AUDIO_VOLUME_KEY} | _AUDIO_REFERENCE_KEYS:
        raise AppsError(
            f"{label}: Audio braucht volume und audio_reference_table/_column zusammen, "
            f"es steht nur {sorted(declared)}"
        )
    restore_label = f"{label}.restore_test"
    return Audio(
        volume=_plain(label, record, _AUDIO_VOLUME_KEY),
        reference_table=_identifier(restore_label, restore_test_record, "audio_reference_table"),
        reference_column=_identifier(restore_label, restore_test_record, "audio_reference_column"),
    )


def _require_exact_keys(
    label: str, record: dict, keys: frozenset[str], optional: frozenset[str] = frozenset()
) -> None:
    if unknown := set(record) - keys - optional:
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
