"""The token files `plan`, `apply` and `check` read; the allowlisted environment carrying them."""

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

SECRETS_DIRECTORY = ".secrets"
OPENTOFU_DIRECTORY = Path(SECRETS_DIRECTORY) / "opentofu"
TOFU_TEMPORARY_PARENT = OPENTOFU_DIRECTORY / "tmp"
TOFU_LANG = "C.UTF-8"
SYSTEM_PATH = "/usr/bin:/bin"


@dataclass(frozen=True)
class SecretFile:
    relative_path: Path
    variable: str


_STATE_PASSPHRASE = SecretFile(OPENTOFU_DIRECTORY / "state-passphrase", "TF_VAR_state_passphrase")


class Access(Enum):
    """`plan` and `check` read with the read tokens; only `apply` is given the write tokens.

    `check` reads only the Cloudflare read file (#138 rule 13), never the rest of the entry.
    """

    READ = "fleet-read"
    WRITE = "fleet-write"

    @property
    def cloudflare_file(self) -> SecretFile:
        return SecretFile(
            Path(SECRETS_DIRECTORY) / "cloudflare" / self.value, "CLOUDFLARE_API_TOKEN"
        )

    @property
    def secret_files(self) -> tuple[SecretFile, ...]:
        secrets_root = Path(SECRETS_DIRECTORY)
        return (
            self.cloudflare_file,
            SecretFile(
                secrets_root / "github" / ("fleet-admin" if self is Access.WRITE else self.value),
                "GITHUB_TOKEN",
            ),
            _STATE_PASSPHRASE,
        )


class SecretFileError(Exception):
    """A token file is missing or empty; the message names the file, never its content."""


def read_secrets(home: Path, access: Access) -> dict[str, str]:
    """Variable name to value; a missing file stops the run so no provider falls back to `gh`."""
    return {
        secret_file.variable: _read_secret_file(home, secret_file)
        for secret_file in access.secret_files
    }


def read_cloudflare_token(home: Path) -> str:
    """The Cloudflare read token alone, for the host check's own client."""
    return _read_secret_file(home, Access.READ.cloudflare_file)


def _read_secret_file(home: Path, secret_file: SecretFile) -> str:
    path = home / secret_file.relative_path
    try:
        value = path.read_text().strip()
    except OSError as error:
        raise SecretFileError(f"cannot read {path}: {error.strerror}") from error
    except UnicodeDecodeError as error:
        raise SecretFileError(f"{path} is not valid UTF-8") from error
    if not value:
        raise SecretFileError(f"{path} is empty")
    return value


@contextmanager
def private_temporary_directory(home: Path) -> Iterator[Path]:
    parent = home / TOFU_TEMPORARY_PARENT
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(dir=parent))
    in_flight: BaseException | None = None
    try:
        yield directory
    except BaseException as error:
        in_flight = error
        raise
    finally:
        try:
            shutil.rmtree(directory)
        except OSError as cleanup_failure:
            if in_flight is None:
                raise
            in_flight.add_note(f"also cannot remove {directory}: {cleanup_failure.strerror}")


def child_environment(
    *, tofu: Path, home: Path, temporary_directory: Path, tofurc: Path, secrets: dict[str, str]
) -> dict[str, str]:
    """Exactly these variables and no inherited one: an allowlist cannot miss the next TF_*."""
    return {
        "PATH": f"{tofu.parent}{os.pathsep}{SYSTEM_PATH}",
        "HOME": str(home),
        "LANG": TOFU_LANG,
        "TMPDIR": str(temporary_directory),
        "TF_INPUT": "0",
        "TF_CLI_CONFIG_FILE": str(tofurc),
        **secrets,
    }


def redact(text: str, secrets: dict[str, str]) -> str:
    for value in secrets.values():
        text = text.replace(value, "***")
    return text
