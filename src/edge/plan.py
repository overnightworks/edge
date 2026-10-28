"""The planned run as a value: what `hal apply` would do, before it does any of it."""

from collections.abc import Mapping
from dataclasses import dataclass

from edge import core, switches
from edge.appkit_step import AppkitPlan
from edge.apps import AppsPlan
from edge.built_image import BuiltRecreate
from edge.legal_step import LegalPlan
from edge.realm import reconcile
from edge.unit_plans import Action, Fleet, KeptImage, Recreate, UnitPlan


@dataclass(frozen=True)
class Plan:
    fleet: Fleet
    read: Mapping[str, switches.Mark | None]
    core: list[core.CorePlan]
    followers: list[core.FollowerPlan]
    built: list[BuiltRecreate]
    recreates: list[Recreate]
    plans: list[UnitPlan]
    images: list[KeptImage]
    realm: reconcile.RealmPlan
    app_plans: AppsPlan
    appkit: AppkitPlan
    legal: LegalPlan

    @property
    def changes_nothing(self) -> bool:
        return (
            self.realm.settled
            and all(plan.action is Action.STAYS_STOPPED for plan in self.plans)
            and all(plan.action is core.CoreAction.NOT_STARTED for plan in self.core)
            and all(plan.action is core.FollowerAction.HELD for plan in self.followers)
            and not self.built
            and not self.recreates
            and self.app_plans.changes_nothing
            and self.appkit.changes_nothing
            and self.legal.changes_nothing
        )

    def describe(self) -> list[str]:
        return [
            *self.appkit.describe(),
            *self.legal.describe(),
            *(plan.describe() for plan in (*self.core, *self.followers)),
        ]
