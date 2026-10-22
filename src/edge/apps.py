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

The compose file of the release is held to the contract of `app_contract.py` before anything is
built, and the images' volumes are checked after build and pull, before `create`. A one-shot service
and a `depends_on` that waits for health or completion are refused, so `create` and `start` run
together under the switch lock like the units', and only the health wait runs outside it. A new
tag that fails stays: the run is partial and the fix is a newer tag.

An app the catalog disables while containers of its project exist is switched off: file-free
`stop` and `rm -f` of project `<key>`, which reaches every container, whatever the checkout's
compose file says. The plan names the named volumes that stay; they, the checkout and the record
are never touched (E4).
"""

import json
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from edge import (
    app_compose,
    app_contract,
    catalog,
    checkout,
    compose,
    deploy_checkout,
    releases,
    switches,
)
from edge.unit_plans import Progress

HEALTH_SECONDS = 300.0
HEALTH_POLL_SECONDS = 2.0
_SHA = 40


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
class OffPlan:
    """An app the catalog disables while containers of its project exist."""

    app: catalog.App
    line: str


@dataclass(frozen=True)
class AppsPlan:
    runtime: AppRuntime
    plans: tuple[AppPlan, ...] = ()
    notes: tuple[str, ...] = ()
    offs: tuple[OffPlan, ...] = ()

    @property
    def changes_nothing(self) -> bool:
        return not self.plans and not self.offs

    def describe(self) -> list[str]:
        return [*self.notes, *(off.line for off in self.offs), *(plan.line for plan in self.plans)]

    def run(
        self, marks: switches.Switches, progress: Progress
    ) -> Iterator[tuple[AppPlan | OffPlan, str | None]]:
        """Each app in turn, the ones switched off first, with the failure line of its run. An
        app that is on and recorded yields None; one switched off yields only a failure."""
        for off in self.offs:
            if failure := _switch_off(off, self.runtime, progress):
                yield off, failure
        for plan in self.plans:
            yield plan, _run(plan, self.runtime, marks, progress)


def plan_apps(
    repository: Path,
    profile_services: Mapping[str, tuple[str, ...]],
    edge_project: str,
    runtime: AppRuntime,
) -> AppsPlan:
    """The plan of every enabled app: it fetches each app's origin and writes only `.git`. An
    app that is on as it should be is left out; one that cannot be planned raises AppError. A
    disabled app is asked file-free for its containers and fetches nothing."""
    document = catalog.read(repository / catalog.FLEET_FILE)
    every = catalog.apps(document, profile_services, edge_project)
    offs = [off for app in every if not app.enabled if (off := _plan_off(app, runtime))]
    enabled = [app for app in every if app.enabled]
    if not enabled:
        return AppsPlan(runtime, offs=tuple(offs))
    organisation = catalog.github_org(document)
    plans, notes = [], []
    for app in enabled:
        planned, skipped = _plan_one(app, organisation, runtime)
        notes += [f"{app.key}: {line}" for line in skipped]
        if planned:
            plans.append(planned)
    return AppsPlan(runtime, tuple(plans), tuple(notes), tuple(offs))


def _plan_off(app: catalog.App, runtime: AppRuntime) -> OffPlan | None:
    """None when no container of the project exists. The line names the containers as `ps` shows
    them (service and state, never a container name that Compose interpolated) and the named
    volumes that stay."""
    engine = app_compose.AppCompose(app.key, runtime.srv / app.key, runtime.docker)
    if not (containers := engine.containers()):
        return None
    volumes = ", ".join(engine.volumes()) or "none"
    shown = ", ".join(container.describe() for container in containers)
    return OffPlan(
        app,
        f"{app.key}: off (containers {shown}; named volumes {volumes} stay; ~/srv/{app.key} stays)",
    )


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
        services = _contract_services(app, release.tag, app_checkout.compose_file(sha))
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


def _contract_services(app: catalog.App, tag: str, text: str | None) -> tuple[str, ...]:
    try:
        return app_contract.check(text, app.key, tag, app.hosts)
    except app_contract.ContractError as error:
        raise AppError(str(error)) from error


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
        progress.step = f"check the volumes of the images at {tag}"
        app_contract.check_image_volumes(
            app_checkout.compose_file(plan.sha) or "", key, tag,
            engine.image_volumes(plan.services, tag),
        )  # fmt: skip
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
    except app_contract.ContractError as error:
        return str(error)
    except (checkout.GitError, compose.ComposeError, switches.SwitchError) as error:
        return f"{key}: could not {progress.step}, {error}"
    except OSError as error:
        return f"{key}: could not {progress.step} ({error.strerror})"
    return None


def _switch_off(off: OffPlan, runtime: AppRuntime, progress: Progress) -> str | None:
    """Stop, then remove, every container of the app's project: the failure line of the step that
    fails, None when none is left. The checkout, the record and every volume stay."""
    key = off.app.key
    progress.unit = key
    engine = app_compose.AppCompose(key, runtime.srv / key, runtime.docker)
    try:
        progress.step = f"stop the containers of {key}"
        engine.stop()
        progress.step = f"remove the containers of {key}"
        engine.remove()
    except compose.ComposeError as error:
        return f"{key}: could not {progress.step}, {error}"
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
