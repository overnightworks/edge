# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""The repository's structural secret check (CI step "No secret in any file").

Reads the YAML structure, so block, flow and list style are one case:
- in keycloak/*.yaml every value whose key ends in secret or password is a $(env:NAME) reference;
- in every compose file Compose may load, every PASSWORD/SECRET-named environment entry is a
  *_FILE path or a ${...} interpolation.
Literal-looking secrets anywhere are gitleaks' job. Exit 1 names each offending file and key.

    uv run --script keycloak/secret-check/secret_check.py [repository root]
"""

import re
import sys
from collections.abc import Iterator
from pathlib import Path

import yaml

ENV_REFERENCE = re.compile(r"\$\(env:[A-Za-z_][A-Za-z0-9_]*\)")
INTERPOLATION = re.compile(r"\$\{[^}]+\}")
COMPOSE_FILE = re.compile(r"(?:[\w-]+\.)*(?:docker-)?compose(?:\.[\w-]+)*\.ya?ml")
SECRET_KEY_SUFFIX = re.compile(r"(?:secret|password)$", re.IGNORECASE)
SECRET_ENV_NAME = re.compile(r"PASSWORD|SECRET", re.IGNORECASE)
SKIPPED_DIRECTORIES = {".git", ".venv", "node_modules"}


def keycloak_violations(document: object) -> Iterator[str]:
    if isinstance(document, dict):
        for key, value in document.items():
            if isinstance(value, dict | list):
                yield from keycloak_violations(value)
            elif (
                isinstance(key, str)
                and SECRET_KEY_SUFFIX.search(key)
                and value is not None
                and not ENV_REFERENCE.fullmatch(str(value))
            ):
                yield key
    elif isinstance(document, list):
        for item in document:
            yield from keycloak_violations(item)


def environment_entries(environment: object) -> Iterator[tuple[str, str | None]]:
    if isinstance(environment, dict):
        for name, value in environment.items():
            yield str(name), None if value is None else str(value)
    elif isinstance(environment, list):
        for entry in environment:
            name, _, value = str(entry).partition("=")
            yield name, value if "=" in entry else None


def compose_violations(document: object) -> Iterator[str]:
    if isinstance(document, dict):
        for key, value in document.items():
            if key == "environment":
                for name, entry_value in environment_entries(value):
                    if (
                        SECRET_ENV_NAME.search(name)
                        and entry_value is not None
                        and not name.upper().endswith("_FILE")
                        and not INTERPOLATION.fullmatch(entry_value)
                    ):
                        yield name
            else:
                yield from compose_violations(value)
    elif isinstance(document, list):
        for item in document:
            yield from compose_violations(item)


def repository_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_file() and not SKIPPED_DIRECTORIES & set(relative.parts):
            yield path


def violations(root: Path) -> Iterator[str]:
    for path in repository_files(root):
        relative = path.relative_to(root)
        if COMPOSE_FILE.fullmatch(path.name):
            found = compose_violations
        elif relative.parent == Path("keycloak") and path.suffix in {".yaml", ".yml"}:
            found = keycloak_violations
        else:
            continue
        for key in found(yaml.safe_load(path.read_text())):
            yield f"{relative}: {key} holds a literal"


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    found = list(violations(root))
    print("\n".join(found) if found else "no literal secret in keycloak/*.yaml or any compose file")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
