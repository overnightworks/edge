"""What waits on origin's `main` for the live checkout, and how `edge apply` reads it (#98)."""

from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import StrEnum

from edge import checkout, core


class Effect(StrEnum):
    """What updating the live checkout does with a changed file (#98 rule 4), in shown order."""

    LIVE = "live once updated"
    RECREATED = "recreated by edge apply"
    REALM = "applied by hal apply (the realm step)"
    UV_SYNC = "stops for `uv sync`"
    NOT_APPLIED = "not applied by edge apply (block 6)"
    OTHER = "other"


_CORE_DEFINITIONS = "compose.yaml"  # what the core is recreated from when its definition changed
_UV_SYNC_FILES = frozenset({"pyproject.toml", "uv.lock"})  # edge's own code needs `uv sync` (S10)


@dataclass(frozen=True)
class Waiting:
    """What waits on origin's `main` for the live checkout; nothing while it is current."""

    origin: checkout.Origin
    commits: tuple[checkout.Commit, ...]
    files: tuple[checkout.ChangedFile, ...]

    def describe(self, binds: core.CheckoutBinds, realm_files: Collection[str]) -> list[str]:
        """The waiting commits, their files by effect, the interim way to bring them in and what
        the plan below applies (#98 rule 4)."""
        one = len(self.commits) == 1
        commits = "1 commit" if one else f"{len(self.commits)} commits"
        lines = [f"{commits} {'waits' if one else 'wait'} on origin {checkout.MAIN}:"]
        lines += [f"  {commit.short} {commit.subject}" for commit in self.commits]
        lines.append("their files, by what an update does with them:")
        by_effect = {effect: [] for effect in Effect}
        for file in sorted(self.files, key=lambda changed: changed.path):
            by_effect[_effect(file.path, binds, realm_files)].append(
                f"    {file.status} {file.shown}"
            )
        for effect, files in by_effect.items():
            if files:
                lines += [f"  {effect}:", *files]
        return [
            *lines,
            f"the live checkout is {commits} behind; to bring them: git -C "
            f"{checkout.LIVE_CHECKOUT} merge --ff-only {self.origin.sha}",
            f"the plan below applies HEAD {self.origin.head}, without the waiting commits",
        ]


def _effect(path: str, binds: core.CheckoutBinds, realm_files: Collection[str]) -> Effect:
    if path == _CORE_DEFINITIONS or core.is_bound(path, binds.recreated):
        return Effect.RECREATED
    if core.is_bound(path, binds.live):
        return Effect.LIVE
    if path in _UV_SYNC_FILES:
        return Effect.UV_SYNC
    if path in realm_files:
        return Effect.REALM
    if core.is_bound(path, binds.units):
        return Effect.NOT_APPLIED
    return Effect.OTHER


def fetched(
    git: checkout.Git, show: Callable[[str], None], say: Callable[[str], None]
) -> Waiting | None:
    """origin's `main` fetched and pinned, and what waits on it; None after saying why the apply
    stops there: the fetch failed (S4), or HEAD is ahead of or beside it (S3) (#98 rules 2, 3)."""
    try:
        origin = checkout.fetch_origin(git)
    except checkout.GitError as error:
        say(f"cannot fetch {checkout.MAIN} from origin, {error}; nothing changed")
        return None
    show(f"fetched {checkout.MAIN} from {origin.url}")
    standing = origin.standing
    if standing is checkout.Standing.CURRENT:
        show(f"live checkout current ({origin.short})")
        return Waiting(origin, (), ())
    if standing is not checkout.Standing.BEHIND:
        stands = "is ahead of" if standing is checkout.Standing.AHEAD else "has diverged from"
        say(
            f"the live checkout {stands} origin {checkout.MAIN} (HEAD {origin.head}, origin "
            f"{checkout.MAIN} {origin.short}); nothing reset, a change reaches it through a pull "
            "request"
        )
        return None
    try:
        commits = tuple(git.waiting_commits(origin.sha))
        return Waiting(origin, commits, tuple(git.waiting_files(origin.sha)))
    except checkout.GitError as error:
        say(f"cannot read what waits on origin {checkout.MAIN}, {error}; nothing changed")
        return None
