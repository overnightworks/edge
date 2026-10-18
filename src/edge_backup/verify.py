"""The backup's own manifest, and what a restored copy must show to count as good."""

import json
from dataclasses import dataclass
from pathlib import Path

MANIFEST_SCHEMA = 1
MANIFEST_NAME = "manifest.json"
DUMP_NAME = "db.dump"


class VerificationError(RuntimeError):
    """The restored copy is not what was backed up; the German message says how."""


@dataclass(frozen=True)
class Manifest:
    app: str
    audio_files: int | None
    """None for a DB-only app."""

    def as_json(self) -> str:
        document = {"schema": MANIFEST_SCHEMA, "app": self.app, "audio_files": self.audio_files}
        return json.dumps(document) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        try:
            document = json.loads(text)
            if document["schema"] != MANIFEST_SCHEMA:
                raise ValueError(document["schema"])
            audio_files = document["audio_files"]
            return cls(
                app=document["app"], audio_files=None if audio_files is None else int(audio_files)
            )
        except (ValueError, KeyError, TypeError) as error:
            raise VerificationError("das Manifest im Snapshot ist unlesbar") from error


def check_audio_count(manifest: Manifest, restored: int) -> None:
    if restored != manifest.audio_files:
        raise VerificationError(
            f"{restored} Audiodateien wiederhergestellt, das Manifest nennt {manifest.audio_files}"
        )


def check_row_count(table: str, rows: int, *, require_rows: bool) -> None:
    if require_rows and rows == 0:
        raise VerificationError(f"die wiederhergestellte Tabelle {table} ist leer")


def check_audio_references(references: list[str], restored_audio: Path) -> None:
    """Every audio file the restored database names exists in the copy with a size above zero."""
    if missing := sum(1 for reference in references if not _present(restored_audio, reference)):
        raise VerificationError(
            f"{missing} von {len(references)} referenzierten Audiodateien fehlen oder sind leer"
        )


def _present(root: Path, reference: str) -> bool:
    candidate = (root / reference).resolve()
    return (
        candidate.is_relative_to(root.resolve())
        and candidate.is_file()
        and candidate.stat().st_size > 0
    )
