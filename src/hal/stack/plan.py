"""The planned run as a value: what `hal apply` would do, before it does any of it."""

from collections.abc import Mapping
from dataclasses import dataclass

from hal.stack import core, switches
from hal.stack.appkit_step import AppkitPlan
from hal.stack.apps import AppsPlan
from hal.stack.built_image import BuiltRecreate
from hal.stack.legal_step import LegalPlan
from hal.stack.realm import reconcile
from hal.stack.unit_plans import Action, Fleet, KeptImage, Recreate, UnitPlan


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
