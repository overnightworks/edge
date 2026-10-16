"""The structural secret check refuses a literal in every YAML style, passes the real files, and refuses a stale script lock."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "keycloak" / "secret-check" / "secret_check.py"

KEYCLOAK_BLOCK = "clients:\n  - clientId: x\n    secret: Tr0ub4dor\n"
KEYCLOAK_FLOW = "clients:\n  - {clientId: x, secret: Tr0ub4dor}\n"
KEYCLOAK_FLOW_SHORT = "{clientId: x, secret: Tr0ub4dor}\n"
KEYCLOAK_CAMEL_CASE = "users:\n  - {adminPassword: Tr0ub4dor}\n"


def compose_with(environment: str) -> str:
    return f"services:\n  a:\n    environment:{environment}\n"


# style: (file, content, offending key)
LITERALS = {
    "block": ("keycloak/realm.yaml", KEYCLOAK_BLOCK, "secret"),
    "flow": ("keycloak/realm.yaml", KEYCLOAK_FLOW, "secret"),
    "flow-short": ("keycloak/realm.yaml", KEYCLOAK_FLOW_SHORT, "secret"),
    "camel-case-key": ("keycloak/master.yaml", KEYCLOAK_CAMEL_CASE, "adminPassword"),
    "compose-flow": ("compose.yaml", compose_with(" {KC_DB_PASSWORD: hunter2}"), "KC_DB_PASSWORD"),
    "compose-list": (
        "compose.yaml",
        compose_with("\n      - KC_DB_PASSWORD=hunter2"),
        "KC_DB_PASSWORD",
    ),
    "compose-lowercase": (
        "compose.yaml",
        compose_with("\n      db_password: hunter2"),
        "db_password",
    ),
    "docker-compose-override": (
        "docker-compose.override.yml",
        compose_with("\n      KC_DB_PASSWORD: hunter2"),
        "KC_DB_PASSWORD",
    ),
    "compose-list-without-value": (
        "compose.yaml",
        compose_with("\n      - KC_DB_PASSWORD"),
        "KC_DB_PASSWORD",
    ),
    "compose-map-without-value": (
        "compose.yaml",
        compose_with("\n      KC_DB_PASSWORD:"),
        "KC_DB_PASSWORD",
    ),
    "compose-name-with-plus": (
        "compose+prod.yaml",
        compose_with("\n      KC_DB_PASSWORD: hunter2"),
        "KC_DB_PASSWORD",
    ),
    "compose-name-with-space": (
        "compose prod.yaml",
        compose_with("\n      KC_DB_PASSWORD: hunter2"),
        "KC_DB_PASSWORD",
    ),
    "compose-overlay": (
        "probe/compose.probe.yaml",
        compose_with("\n      APP_SECRET: hunter2"),
        "APP_SECRET",
    ),
    "compose-interpolated-default": (
        "compose.yaml",
        compose_with("\n      KC_DB_PASSWORD: ${KC_DB_PASSWORD:-hunter2}"),
        "KC_DB_PASSWORD",
    ),
    "compose-interpolated-list-default": (
        "compose.yaml",
        compose_with("\n      - KC_DB_PASSWORD=${X-hunter2}"),
        "KC_DB_PASSWORD",
    ),
    "compose-bare-interpolation": (
        "compose.yaml",
        compose_with("\n      APP_SECRET: ${APP_SECRET}"),
        "APP_SECRET",
    ),
    "compose-hyphenated-overlay": (
        "compose-prod.yaml",
        compose_with("\n      APP_SECRET: hunter2"),
        "APP_SECRET",
    ),
    "docker-compose-hyphenated-overlay": (
        "docker-compose-prod.yml",
        compose_with("\n      APP_SECRET: hunter2"),
        "APP_SECRET",
    ),
    "compose-name-with-newline": (
        "compose\nprod.yaml",
        compose_with("\n      KC_DB_PASSWORD: hunter2"),
        "KC_DB_PASSWORD",
    ),
    "keycloak-null": ("keycloak/realm.yaml", "clients:\n  - {clientId: x, secret: }\n", "secret"),
    "keycloak-list": (
        "keycloak/realm.yaml",
        "clients:\n  - {clientId: x, secret: [Tr0ub4dor]}\n",
        "secret",
    ),
    "keycloak-mapping": (
        "keycloak/realm.yaml",
        "clients:\n  - {clientId: x, secret: {value: Tr0ub4dor}}\n",
        "secret",
    ),
    "compose-non-string-list-entry": (
        "compose.yaml",
        compose_with("\n      - 5\n      - APP_SECRET=hunter2"),
        "APP_SECRET",
    ),
}

REFERENCES = {
    "keycloak/realm.yaml": "clients:\n  - {clientId: x, secret: $(env:RP_SECRET)}\n",
    "compose.yaml": compose_with(
        "\n      DB_PASSWORD_FILE: /run/secrets/db\n      APP_SECRET_FILE: /run/secrets/app"
    ),
}


def run_check(root: Path, script: Path = CHECK) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "--locked", "--no-build", "--script", str(script), str(root)],
        capture_output=True,
        text=True,
    )


def plant(root: Path, name: str, content: str) -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(content)


@pytest.mark.parametrize("style", LITERALS)
def test_a_literal_secret_exits_non_zero_naming_file_and_key(style: str, tmp_path: Path) -> None:
    name, content, key = LITERALS[style]
    plant(tmp_path, name, content)

    result = run_check(tmp_path)

    assert result.returncode == 1
    rule = "holds a literal" if name.startswith("keycloak/") else "must be a _FILE path"
    assert f"{name}: {key} {rule}" in result.stdout


def test_references_and_file_paths_pass(tmp_path: Path) -> None:
    for name, content in REFERENCES.items():
        plant(tmp_path, name, content)

    assert run_check(tmp_path).returncode == 0


def test_the_repository_files_pass() -> None:
    result = run_check(ROOT)

    assert result.returncode == 0, result.stdout


def test_a_stale_script_lock_fails_the_check_and_stays_unwritten(tmp_path: Path) -> None:
    """defect:#12 - a script lock out of step with its script must fail, not be rewritten."""
    script = tmp_path / CHECK.name
    lock = tmp_path / f"{CHECK.name}.lock"
    script.write_text(CHECK.read_text().replace('">=3.12"', '">=3.11"'))
    lock.write_text(CHECK.with_name(lock.name).read_text())

    result = run_check(tmp_path, script)

    assert result.returncode != 0
    assert "--locked" in result.stderr
    assert lock.read_text() == CHECK.with_name(lock.name).read_text()
