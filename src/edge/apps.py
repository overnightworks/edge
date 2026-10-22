"""The apps of the catalog: what `hal apply` plans for them and how it runs the plan (#255).

An app is switched on at the newest release of its repository: a `vYYYY.MM.DD.N` tag on origin's
default branch (`releases.py`), checked out in `~/srv/<key>` (`deploy_checkout.py`) and run by
Compose as project `<key>` (`app_compose.py`). The plan shows tag and full sha and waits for the
operator's `yes`, which is the gate: whoever can put a tag on a merged commit offers code to run
here (E1). Nothing of an app is read, created or printed that is a secret, and no output of
Compose is printed (E7).

What was last deployed is the record `<state>/deployed/<key>` (tag, sha, time), written only once
every container is observed healthy. What runs is observed separately, file-free, from the
project's containers. "Nothing to change" holds only when the record, the newest release and the
checkout's HEAD agree and every service has exactly one healthy container.

The compose file of the release is held to a minimal contract before anything is built: present at
the root, self-contained (no `include`, no `extends.file`, no `profiles`), named as the key or not
at all, with no `depends_on` condition that waits for health or completion and no one-shot service
(restart `no`). So `create` and `start` run together under the switch lock like the units', and
only the health wait runs outside it. A new tag that fails stays: the run is partial and the fix is
a newer tag.
"""

import json
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from edge import app_compose, catalog, checkout, compose, deploy_checkout, releases, switches
from edge.unit_plans import Progress

HEALTH_SECONDS = 300.0
HEALTH_POLL_SECONDS = 2.0
_SHA = 40
_ONE_SHOT_CONDITIONS = {"service_healthy", "service_completed_successfully"}


class AppError(Exception):
    """An app cannot be planned; the message is one line that names the entry."""


class Reason(StrEnum):
    FIRST = "on"
    UPDATE = "update"
    START_AGAIN = "start again"


@dataclass(frozen=True)
class Deployment:
    """The last successful deployment of an app."""

    tag: str
    sha: str
    at: str


@dataclass(frozen=True)
class DeploymentRecord:
    directory: Path

    def read(self, key: str) -> Deployment | None:
        path = self.directory / key
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise AppError(f"services.{key}: cannot read {path} ({error.strerror})") from error
        try:
            fields = json.loads(raw)
            deployment = Deployment(fields["tag"], fields["sha"], fields["time"])
            valid = releases.parse(deployment.tag) and _is_sha(deployment.sha)
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise AppError(f"services.{key}: {path} is not a record of a deployment")
        return deployment

    def write(self, key: str, deployment: Deployment) -> None:
        """Atomically, mode 0600; raises OSError."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fields = {"tag": deployment.tag, "sha": deployment.sha, "time": deployment.at}
        switches.write_atomically(self.directory / key, json.dumps(fields), keep_existing=False)


def _now() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True)
class AppRuntime:
    """Where apps are checked out and how git, docker and the clock are reached."""

    srv: Path
    record: DeploymentRecord
    git: checkout.Runner = checkout.run_git
    docker: compose.Runner = compose.run_docker
    health: compose.Watch = field(default_factory=lambda: compose.Watch(seconds=HEALTH_SECONDS))
    now: Callable[[], datetime] = _now

    @classmethod
    def of(cls, home: Path, state_directory: Path) -> "AppRuntime":
        return cls(home / "srv", DeploymentRecord(state_directory / "deployed"))


@dataclass(frozen=True)
class AppPlan:
    app: catalog.App
    release: releases.Release
    sha: str
    services: tuple[str, ...]
    url: str
    line: str


@dataclass(frozen=True)
class AppsPlan:
    runtime: AppRuntime
    plans: tuple[AppPlan, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def changes_nothing(self) -> bool:
        return not self.plans

    def describe(self) -> list[str]:
        return [*self.notes, *(plan.line for plan in self.plans)]

    def run(
        self, marks: switches.Switches, progress: Progress
    ) -> Iterator[tuple[AppPlan, str | None]]:
        """Each plan in turn with the failure line of its run, None when it is on and recorded."""
        for plan in self.plans:
            yield plan, _run(plan, self.runtime, marks, progress)


def plan_apps(
    repository: Path,
    profile_services: Mapping[str, tuple[str, ...]],
    edge_project: str,
    runtime: AppRuntime,
) -> AppsPlan:
    """The plan of every enabled app: it fetches each app's origin and writes only `.git`. An
    app that is on as it should be is left out; one that cannot be planned raises AppError."""
    document = catalog.read(repository / catalog.FLEET_FILE)
    enabled = [app for app in catalog.apps(document, profile_services, edge_project) if app.enabled]
    if not enabled:
        return AppsPlan(runtime)
    organisation = catalog.github_org(document)
    plans, notes = [], []
    for app in enabled:
        planned, skipped = _plan_one(app, organisation, runtime)
        notes += [f"{app.key}: {line}" for line in skipped]
        if planned:
            plans.append(planned)
    return AppsPlan(runtime, tuple(plans), tuple(notes))


def _plan_one(
    app: catalog.App, organisation: str, runtime: AppRuntime
) -> tuple[AppPlan | None, tuple[str, ...]]:
    url = f"https://github.com/{organisation}/{app.key}.git"
    app_checkout = deploy_checkout.AppCheckout(
        checkout.Git(runtime.srv / app.key, runtime.git), url, app.default_branch
    )
    try:
        app_checkout.prepare()
    except deploy_checkout.CheckoutRefused as error:
        raise AppError(f"services.{app.key}: ~/srv/{app.key} {error}") from error
    except checkout.GitError as error:
        raise AppError(f"services.{app.key}: {error}") from error
    try:
        app_checkout.fetch()
        tags = app_checkout.tags()
        choice = releases.choose(tags.mirrored, tags.on_branch, app.default_branch)
        if choice.newest is None:
            raise AppError(
                f"services.{app.key}: no {releases.GRAMMAR} release on origin "
                f"{app.default_branch}; the app's head cuts the first one"
            )
        release = choice.newest
        sha = app_checkout.commit_of(release.tag)
        recorded = runtime.record.read(app.key)
        _refuse_moved_tag(app_checkout, tags.mirrored, app.key, recorded)
        services = contract_services(app_checkout.compose_file(sha), app.key, release.tag)
        engine = app_compose.AppCompose(app.key, runtime.srv / app.key, runtime.docker)
        containers = engine.containers()
        head = app_checkout.head()
    except (checkout.GitError, deploy_checkout.CheckoutRefused) as error:
        raise AppError(f"services.{app.key}: {error}") from error
    if recorded and recorded.sha == sha and head == sha and _settled(containers, services):
        return None, choice.skipped
    line = _describe(app, release, sha, recorded, head, containers)
    return AppPlan(app, release, sha, services, url, line), choice.skipped


def _refuse_moved_tag(
    app_checkout: deploy_checkout.AppCheckout,
    mirrored: Sequence[str],
    key: str,
    recorded: Deployment | None,
) -> None:
    """A tag cannot be planned that names another commit on origin than when it was deployed."""
    if recorded is None or recorded.tag not in mirrored:
        return
    now = app_checkout.commit_of(recorded.tag)
    if now != recorded.sha:
        raise AppError(
            f"services.{key}: tag {recorded.tag} moved on origin; deployed {recorded.sha}, "
            f"origin now {now}"
        )


def _describe(
    app: catalog.App,
    release: releases.Release,
    sha: str,
    recorded: Deployment | None,
    head: str | None,
    containers: Sequence[app_compose.AppContainer],
) -> str:
    key, tag = app.key, release.tag
    now = "; now: " + (", ".join(c.describe() for c in containers) or "no container")
    if recorded is None:
        replacing = ", replacing the running containers; named volumes kept" if containers else ""
        return (
            f"{key}: on at {tag} ({sha}) from origin {app.default_branch}, "
            f"into ~/srv/{key}{replacing}"
        )
    if recorded.sha == sha:
        return f"{key}: start again at {tag} ({sha}, recorded){now}"
    unfinished = ""
    if head is not None and head != recorded.sha:
        unfinished = f"; the update to {tag if head == sha else head} did not finish"
    return (
        f"{key}: update to {tag} ({sha}); last successful deployment {recorded.tag}"
        f"{unfinished}{now}"
    )


def _settled(containers: Sequence[app_compose.AppContainer], services: Sequence[str]) -> bool:
    """Every service has exactly one container, healthy, and no other container exists."""
    per_service = Counter(container.service for container in containers)
    return per_service == Counter(services) and all(c.healthy for c in containers)


def _is_sha(text: str) -> bool:
    return len(text) == _SHA and all(digit in "0123456789abcdef" for digit in text)


def contract_services(text: str | None, key: str, tag: str) -> tuple[str, ...]:
    """The services of a release's compose file when it meets the minimal contract; else AppError
    naming the entry, the tag and the key that is refused, never a value."""

    def refuse(reason: str) -> AppError:
        return AppError(f"services.{key}: compose.yaml at {tag}: {reason}")

    if text is None:
        raise refuse("none at the root of the repository")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise refuse(f"cannot be read ({type(error).__name__})") from None
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict) or not services:
        raise refuse("must be a map with services")
    if "include" in document:
        raise refuse("uses include (include); an app is self-contained")
    if "name" in document and document["name"] != key:
        raise refuse(f"names its project other than {key} (name)")
    for name, definition in services.items():
        if reason := _service_refusal(definition):
            raise refuse(f"service {compose.printable(str(name))} {reason}")
    return tuple(str(name) for name in services)


def _service_refusal(definition: Any) -> str | None:
    """Why a service definition breaks the contract; None when it keeps it."""
    if not isinstance(definition, dict):
        return "must be a map"
    if "profiles" in definition:
        return "uses profiles (profiles); an app's services are all started"
    extends = definition.get("extends")
    if isinstance(extends, dict) and "file" in extends:
        return "extends a file (extends.file); an app is self-contained"
    conditions = definition.get("depends_on")
    if isinstance(conditions, dict):
        waits = [d.get("condition") for d in conditions.values() if isinstance(d, dict)]
        if any(_interpolates(value) for value in waits):
            return "interpolates a depends_on condition ($); not yet supported"
        if any(value in _ONE_SHOT_CONDITIONS for value in waits):
            return "waits for health or completion (depends_on condition); not yet supported"
    restart = definition.get("restart")
    if _interpolates(restart):
        return "interpolates restart ($); not yet supported"
    if restart is False or restart == "no":
        return "is a one-shot (restart no); not yet supported"
    return None


def _interpolates(value: Any) -> bool:
    return isinstance(value, str) and "$" in value


def _run(
    plan: AppPlan, runtime: AppRuntime, marks: switches.Switches, progress: Progress
) -> str | None:
    """Check out the release, build, create and start under the switch lock, wait for health,
    record: the failure line of the first step that fails, None when the app is on and recorded.
    `progress` names the step, so an interrupt can say where the containers may be partly
    replaced."""
    key, tag = plan.app.key, plan.release.tag
    progress.unit = key
    app_checkout = deploy_checkout.AppCheckout(
        checkout.Git(runtime.srv / key, runtime.git), plan.url, plan.app.default_branch
    )
    engine = app_compose.AppCompose(key, runtime.srv / key, runtime.docker)
    try:
        progress.step = f"check out {tag}"
        app_checkout.check_out(plan.sha)
        progress.step = f"build at {tag}"
        engine.build(tag)
        progress.step = f"pull at {tag}"
        engine.pull(tag)
        progress.step = f"create the containers at {tag}; they may be partly replaced"
        with marks.lock():
            engine.create(tag)
            progress.step = f"start at {tag}; the containers may be partly replaced"
            engine.start(tag)
        progress.step = f"wait for {key} to be healthy"
        if unhealthy := _unhealthy_after_waiting(engine, plan.services, runtime.health):
            return (
                f"{key} is not healthy at {tag} after {runtime.health.seconds:g}s ({unhealthy}); "
                "the record stays, the fix is a newer tag"
            )
        progress.step = f"record the deployment of {tag}"
        runtime.record.write(
            key, Deployment(tag, plan.sha, runtime.now().isoformat(timespec="seconds"))
        )
    except (checkout.GitError, compose.ComposeError, switches.SwitchError) as error:
        return f"{key}: could not {progress.step}, {error}"
    except OSError as error:
        return f"{key}: could not {progress.step} ({error.strerror})"
    return None


def _unhealthy_after_waiting(
    engine: app_compose.AppCompose, services: Sequence[str], watch: compose.Watch
) -> str | None:
    """The containers as last seen when they are not all healthy by the deadline; None when
    they are."""
    deadline = watch.clock() + watch.seconds
    while True:
        containers = engine.containers()
        if _settled(containers, services):
            return None
        if watch.clock() >= deadline:
            return ", ".join(c.describe() for c in containers) or "no container"
        watch.sleep(HEALTH_POLL_SECONDS)
