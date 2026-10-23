"""The agent service image recipe's static facts (#192). CI never starts containers, so these pins
hold what the recipe says; the build and the start of the image are the probe's
(`probe/agent-service/image.py`)."""

import argparse
import hashlib
import importlib.util
import re
import subprocess
from pathlib import Path
from types import ModuleType

import agent_service_lock
import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
PROBE = REPOSITORY / "probe" / "agent-service" / "image.py"
IMAGE_DIRECTORY = REPOSITORY / "agent-service" / "image"
DOCKERFILE = (IMAGE_DIRECTORY / "Dockerfile").read_text(encoding="utf-8")
LOCK = (IMAGE_DIRECTORY / "requirements.txt").read_text(encoding="utf-8")
AGENT_SERVICE_DOCUMENT = (REPOSITORY / "docs" / "agent-service.md").read_text(encoding="utf-8")

RELEASE_WHEELS = ("agent-runtime", "agent-runtime-client", "agent-runtime-service")
DIGEST_PINNED_IMAGE = re.compile(r"^FROM \S+@sha256:[0-9a-f]{64}\b", re.MULTILINE)


def dockerfile_argument(name: str) -> str:
    return re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE, re.MULTILINE).group(1)


def lock_requirements() -> list[str]:
    """Each requirement of the lock with its continuation lines, comments left out."""
    body = "\n".join(line for line in LOCK.splitlines() if not line.lstrip().startswith("#"))
    blocks = re.split(r"\n(?=\S)", body.strip())
    return [" ".join(block.replace("\\\n", " ").split()) for block in blocks]


def release_tag() -> str:
    return re.match(r"# agent-runtime (v\S+), commit [0-9a-f]{40}\n", LOCK).group(1)


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


def test_the_recipe_runs_only_the_download_and_the_hashed_install():
    steps = re.findall(r"^RUN (\S+ \S+)", DOCKERFILE, re.MULTILINE)
    assert steps == ["curl --fail", "pip install"]


@pytest.mark.parametrize("package_manager", ["apt", "apt-get", "apk"])
def test_the_recipe_installs_no_system_package(package_manager):
    assert not re.search(rf"\b{package_manager}\b", DOCKERFILE)


@pytest.mark.parametrize("tag", ["v0.7.0", "v10.0.1", "v10.20.30"])
def test_the_lock_script_accepts_a_release_tag(tag):
    assert agent_service_lock.release_tag(tag) == tag


@pytest.mark.parametrize(
    "tag",
    [
        "--upload-pack=x", "v0.4", "0.4.0", "v0.4.0 ", "v0.4.0\n", "v0.4.0/../x", "main", "",
        "v00.4.0", "v0.04.0", "v0.4.00",
    ],
)  # fmt: skip
def test_the_lock_script_refuses_anything_but_a_release_tag(tag):
    with pytest.raises(argparse.ArgumentTypeError):
        agent_service_lock.release_tag(tag)


def composed_lock() -> str:
    names = agent_service_lock.release_wheel_urls("v1.2.3")
    digests = {name: f"{index:064x}" for index, name in enumerate(names)}
    return agent_service_lock.lock_text(
        "v1.2.3", "c0ffee", "pkg==1.0 \\\n    --hash=sha256:ab\n", digests
    )


def test_the_composed_lock_names_the_tag_and_the_commit_in_its_header():
    assert composed_lock().startswith("# agent-runtime v1.2.3, commit c0ffee\n")


def test_the_composed_lock_holds_the_closure_then_each_wheel_by_url_and_hash():
    lines = composed_lock().splitlines()
    assert "pkg==1.0 \\" in lines
    wheels = [line for line in lines if " @ " in line]
    assert [line.split(" @ ")[0] for line in wheels] == list(
        agent_service_lock.release_wheel_urls("v1.2.3")
    )
    assert all("/releases/download/v1.2.3/" in line for line in wheels)
    assert [line.strip() for line in lines if "--hash=sha256:0" in line] == [
        f"--hash=sha256:{index:064x}" for index in range(3)
    ]


def test_the_composed_lock_is_the_same_text_each_time():
    first, second = composed_lock(), composed_lock()
    assert second == first


def test_nothing_is_cloned_for_a_tag_that_is_not_a_release_tag():
    with pytest.raises(argparse.ArgumentTypeError):
        agent_service_lock.released_commit_and_closure("--upload-pack=x")


def test_the_wheel_digest_is_the_sha256_of_the_served_bytes(tmp_path):
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"served bytes")
    assert (
        agent_service_lock.wheel_digest(wheel.as_uri())
        == hashlib.sha256(b"served bytes").hexdigest()
    )


def test_the_release_is_the_commit_the_tag_points_at_with_its_exported_closure(
    tmp_path, monkeypatch
):
    origin = tmp_path / "origin"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(origin)]
    subprocess.run(["git", "init", "--quiet", str(origin)], check=True)
    subprocess.run([*git, "commit", "--quiet", "--allow-empty", "-m", "release"], check=True)
    subprocess.run([*git, "tag", "v1.2.3"], check=True)
    tagged = subprocess.run(
        [*git, "rev-parse", "v1.2.3^{commit}"], check=True, capture_output=True, text=True
    ).stdout.strip()
    monkeypatch.setattr(agent_service_lock, "REPOSITORY_URL", origin.as_uri())
    monkeypatch.setattr(agent_service_lock, "UV_EXPORT", ("printf", "closure"))
    assert agent_service_lock.released_commit_and_closure("v1.2.3") == (tagged, "closure")


def test_the_lock_script_writes_the_built_lock(tmp_path, monkeypatch):
    lock = tmp_path / "image" / "requirements.txt"
    monkeypatch.setattr(agent_service_lock, "LOCK_PATH", lock)
    monkeypatch.setattr(agent_service_lock, "built_lock", lambda tag: f"lock for {tag}\n")
    assert agent_service_lock.main(["v1.2.3"]) == 0
    assert lock.read_text(encoding="utf-8") == "lock for v1.2.3\n"


def test_the_image_runs_the_service_as_uid_1000_without_a_login():
    assert "USER 1000:1000" in DOCKERFILE
    assert 'ENTRYPOINT ["agent-runtime-service"]' in DOCKERFILE
    assert "/.claude" not in DOCKERFILE
    assert "managed-settings" not in DOCKERFILE


def test_the_lock_names_its_release_tag_and_commit():
    assert release_tag() == "v0.7.0"


def test_every_requirement_of_the_lock_carries_a_hash_and_is_pinned():
    for requirement in lock_requirements():
        assert "--hash=sha256:" in requirement, requirement
        assert re.match(r"[A-Za-z0-9._-]+(\[\w+\])?(==\S+| @ https://)", requirement), requirement


@pytest.mark.parametrize("package", RELEASE_WHEELS)
def test_the_lock_holds_the_release_wheel_by_url_and_sha256(package):
    tag = release_tag()
    wheel = f"overnightworks_{package.replace('-', '_')}-{tag.removeprefix('v')}-py3-none-any.whl"
    url = f"https://github.com/overnightworks/agent-runtime/releases/download/{tag}/{wheel}"
    assert any(
        requirement.startswith(f"overnightworks-{package} @ {url} --hash=sha256:")
        for requirement in lock_requirements()
    )


def test_the_lock_holds_webauth_by_url_and_sha256():
    assert any(
        requirement.startswith("overnightworks-webauth @ https://")
        for requirement in lock_requirements()
    )


def test_the_agent_service_document_builds_the_name_made_of_the_release_and_the_cli_version():
    version = release_tag().removeprefix("v")
    name = f"edge-agent-service:{version}-{dockerfile_argument('CLAUDE_VERSION')}"
    command = f"docker build -f agent-service/image/Dockerfile -t {name} agent-service/image"
    assert command in AGENT_SERVICE_DOCUMENT


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_service_image_probe", PROBE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLANTED = "/usr/local/planted.bin"


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (2, f"{PLANTED}\n"),
        (1, ""),
        (0, ""),
        (0, f"{PLANTED}\n/usr/other\n"),
    ],
)
def test_the_token_search_bite_check_fails_without_exit_zero_and_exactly_the_planted_path(
    probe, returncode, stdout
):
    bitten = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
    assert probe.bite_check_failure(bitten, PLANTED) is not None


def test_the_token_search_bite_check_passes_on_exit_zero_with_exactly_the_planted_path(probe):
    bitten = subprocess.CompletedProcess([], 0, stdout=f"{PLANTED}\n", stderr="")
    assert probe.bite_check_failure(bitten, PLANTED) is None
