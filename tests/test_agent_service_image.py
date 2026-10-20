"""The agent service image recipe's static facts (#192). CI never starts containers, so these pins
hold what the recipe says; the build and the start of the image are the probe's
(`probe/agent-service/image.py`)."""

import re
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
IMAGE_DIRECTORY = REPOSITORY / "agent-service" / "image"
DOCKERFILE = (IMAGE_DIRECTORY / "Dockerfile").read_text(encoding="utf-8")
LOCK = (IMAGE_DIRECTORY / "requirements.txt").read_text(encoding="utf-8")
README = (REPOSITORY / "README.md").read_text(encoding="utf-8")

RELEASE_WHEELS = ("agent-client", "agent-providers", "agent-service")
DIGEST_PINNED_IMAGE = re.compile(r"^FROM \S+@sha256:[0-9a-f]{64}\b", re.MULTILINE)


def dockerfile_argument(name: str) -> str:
    return re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE, re.MULTILINE).group(1)


def lock_requirements() -> list[str]:
    """Each requirement of the lock with its continuation lines, comments left out."""
    body = "\n".join(line for line in LOCK.splitlines() if not line.lstrip().startswith("#"))
    blocks = re.split(r"\n(?=\S)", body.strip())
    return [" ".join(block.replace("\\\n", " ").split()) for block in blocks]


def release_tag() -> str:
    return re.match(r"# agent-providers (v\S+), commit [0-9a-f]{40}\n", LOCK).group(1)


def test_every_base_image_is_pinned_by_digest():
    base_images = re.findall("^FROM ", DOCKERFILE, re.MULTILINE)
    assert len(DIGEST_PINNED_IMAGE.findall(DOCKERFILE)) == len(base_images)


def test_the_runtime_base_is_debian_glibc_python_3_13():
    assert re.search(r"^FROM docker\.io/library/python:3\.13\.\d+-slim-trixie@", DOCKERFILE, re.M)


def test_the_claude_cli_is_fetched_by_its_pinned_version_and_checked_by_sha256():
    version = dockerfile_argument("CLAUDE_VERSION")
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert re.fullmatch(r"[0-9a-f]{64}", dockerfile_argument("CLAUDE_SHA256"))
    assert "curl --fail" in DOCKERFILE
    assert "claude-code-releases/${CLAUDE_VERSION}/linux-x64/claude" in DOCKERFILE
    assert 'echo "${CLAUDE_SHA256}  /tmp/claude" | sha256sum -c -' in DOCKERFILE


@pytest.mark.parametrize("moving_name", ["latest", "stable", "install.sh", "claude.ai/install"])
def test_the_recipe_never_names_a_moving_cli_release(moving_name):
    assert moving_name not in DOCKERFILE


def test_the_cli_is_installed_root_owned_and_not_writable():
    assert "--chown=0:0 --chmod=0555" in DOCKERFILE
    assert DOCKERFILE.count("/opt/claude/claude") == 1


def test_packages_install_only_from_the_hashed_lock_and_are_checked_afterwards():
    assert "pip install --no-cache-dir --no-deps --require-hashes --only-binary :all:" in DOCKERFILE
    assert "-r /tmp/requirements.txt" in DOCKERFILE
    assert "&& pip check" in DOCKERFILE


def test_the_image_runs_the_service_as_uid_1000_without_a_login():
    assert "USER 1000:1000" in DOCKERFILE
    assert 'ENTRYPOINT ["agent-service"]' in DOCKERFILE
    assert "/.claude" not in DOCKERFILE
    assert "managed-settings" not in DOCKERFILE


def test_the_lock_names_its_release_tag_and_commit():
    assert release_tag() == "v0.4.0"


def test_every_requirement_of_the_lock_carries_a_hash_and_is_pinned():
    for requirement in lock_requirements():
        assert "--hash=sha256:" in requirement, requirement
        assert re.match(r"[A-Za-z0-9._-]+(\[\w+\])?(==\S+| @ https://)", requirement), requirement


@pytest.mark.parametrize("package", RELEASE_WHEELS)
def test_the_lock_holds_the_release_wheel_by_url_and_sha256(package):
    tag = release_tag()
    wheel = f"overnightworks_{package.replace('-', '_')}-{tag.removeprefix('v')}-py3-none-any.whl"
    url = f"https://github.com/overnightworks/agent-providers/releases/download/{tag}/{wheel}"
    assert any(
        requirement.startswith(f"overnightworks-{package} @ {url} --hash=sha256:")
        for requirement in lock_requirements()
    )


def test_the_lock_holds_webauth_by_url_and_sha256():
    assert any(
        requirement.startswith("overnightworks-webauth @ https://")
        for requirement in lock_requirements()
    )


def test_the_readme_builds_the_name_made_of_the_release_and_the_cli_version():
    version = release_tag().removeprefix("v")
    name = f"edge-agent-service:{version}-{dockerfile_argument('CLAUDE_VERSION')}"
    command = f"docker build -f agent-service/image/Dockerfile -t {name} agent-service/image"
    assert command in README
