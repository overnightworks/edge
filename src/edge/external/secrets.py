"""The token files `plan` reads, and the allowlisted environment that carries them to tofu."""

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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


SECRET_FILES = (
    SecretFile(Path(SECRETS_DIRECTORY) / "cloudflare" / "fleet-read", "CLOUDFLARE_API_TOKEN"),
    SecretFile(Path(SECRETS_DIRECTORY) / "github" / "fleet-read", "GITHUB_TOKEN"),
    SecretFile(OPENTOFU_DIRECTORY / "state-passphrase", "TF_VAR_state_passphrase"),
)


class SecretFileError(Exception):
    """A token file is missing or empty; the message names the file, never its content."""


def read_secrets(home: Path) -> dict[str, str]:
    """Variable name to value; a missing file stops the run so no provider falls back to `gh`."""
    values = {}
    for secret_file in SECRET_FILES:
        path = home / secret_file.relative_path
        try:
            value = path.read_text().strip()
        except OSError as error:
            raise SecretFileError(f"cannot read {path}: {error.strerror}") from error
        except UnicodeDecodeError as error:
            raise SecretFileError(f"{path} is not valid UTF-8") from error
        if not value:
            raise SecretFileError(f"{path} is empty")
        values[secret_file.variable] = value
    return values


@contextmanager
def private_temporary_directory(home: Path) -> Iterator[Path]:
    parent = home / TOFU_TEMPORARY_PARENT
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(dir=parent))
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


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
