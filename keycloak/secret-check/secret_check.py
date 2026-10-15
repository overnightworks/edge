# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml==6.0.2"]
# ///
"""The repository's structural secret check (CI step "No secret in any file").

Reads the YAML structure, so block, flow and list style are one case:
- in keycloak/*.yaml every value whose key ends in secret or password is a $(env:NAME) reference;
- in every compose file Compose may load, every PASSWORD/SECRET-named environment entry is a
  *_FILE path (an interpolation would put the value into Config.Env).
Literal-looking secrets anywhere are gitleaks' job. Exit 1 names each offending file and key.

    uv run --script keycloak/secret-check/secret_check.py [repository root]
"""

import re
import sys
from collections.abc import Iterator
from pathlib import Path

import yaml

ENV_REFERENCE = re.compile(r"\$\(env:[A-Za-z_][A-Za-z0-9_]*\)")
COMPOSE_FILE = re.compile(r"(?:[\w-]+\.)*(?:docker-)?compose.*\.ya?ml", re.DOTALL)
SECRET_KEY_SUFFIX = re.compile(r"(?:secret|password)$", re.IGNORECASE)
SECRET_ENV_NAME = re.compile(r"PASSWORD|SECRET", re.IGNORECASE)
SKIPPED_DIRECTORIES = {".git", ".venv", "node_modules"}


def keycloak_violations(document: object) -> Iterator[str]:
    if isinstance(document, dict):
        for key, value in document.items():
            if isinstance(key, str) and SECRET_KEY_SUFFIX.search(key):
                if not (isinstance(value, str) and ENV_REFERENCE.fullmatch(value)):
                    yield key
            else:
                yield from keycloak_violations(value)
    elif isinstance(document, list):
        for item in document:
            yield from keycloak_violations(item)


def environment_names(environment: object) -> Iterator[str]:
    if isinstance(environment, dict):
        for name in environment:
            yield str(name)
    elif isinstance(environment, list):
        for entry in environment:
            yield str(entry).partition("=")[0]


def compose_violations(document: object) -> Iterator[str]:
    if isinstance(document, dict):
        for key, value in document.items():
            if key == "environment":
                for name in environment_names(value):
                    if SECRET_ENV_NAME.search(name) and not name.upper().endswith("_FILE"):
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
            found, rule = compose_violations, "must be a _FILE path"
        elif relative.parent == Path("keycloak") and path.suffix in {".yaml", ".yml"}:
            found, rule = keycloak_violations, "holds a literal"
        else:
            continue
        for key in found(yaml.safe_load(path.read_text())):
            yield f"{relative}: {key} {rule}"


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    found = list(violations(root))
    print("\n".join(found) if found else "no literal secret in keycloak/*.yaml or any compose file")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
