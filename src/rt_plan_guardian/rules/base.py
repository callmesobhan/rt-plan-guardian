"""Rule base class and registry.

A rule is a small, stateless class: it receives the PlanContext plus its resolved
parameters and yields `Check`s. The engine - not the rule - decides PASS/FAIL, handles
missing inputs and non-applicable techniques, and converts crashes into ERROR results, so a
rule only has to say "here is what I compared".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

from ..context import PlanContext
from ..errors import GuardianError
from ..models import Check, Severity
from ..plan_model import control_points, plan_beams

CATEGORY_ORDER = (
    "input",
    "identity",
    "linkage",
    "structure",
    "plan",
    "beam",
    "ion",
    "electron",
    "brachy",
    "dose",
    "protocol",
)


class Rule(ABC):
    id: ClassVar[str]
    title: ClassVar[str]
    category: ClassVar[str]
    #: Why the rule exists - shown in the catalog and the report.
    description: ClassVar[str]
    #: Where the expectation comes from (DICOM module/attribute names, guideline).
    reference: ClassVar[str] = ""
    #: Object kinds ("rtplan", "rtstruct", "rtdose") that must be loaded, else SKIPPED.
    requires: ClassVar[tuple[str, ...]] = ()
    #: Techniques this rule applies to (see plan_model). Empty = every plan. Otherwise the rule is
    #: NOT_APPLICABLE unless the plan uses at least one of them.
    applies_to: ClassVar[frozenset[str]] = frozenset()
    default_severity: ClassVar[Severity] = Severity.ERROR
    #: Every tunable parameter and its default. Ruleset files may only set keys listed here.
    default_params: ClassVar[Mapping[str, Any]] = {}

    @abstractmethod
    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        """Yield one Check per concrete comparison.

        Raise NotApplicable if the rule does not apply to this plan, or CannotEvaluate if it
        applies but the needed data is missing/unsupported. Never return a silent pass.
        """

    @classmethod  # noqa: B027 - deliberately a no-op default hook, not an abstract method
    def validate_params(cls, params: Mapping[str, Any]) -> None:
        """Optional deep validation of resolved params; raise ValueError with a clear message.
        Called when the ruleset is loaded, so a malformed constraint fails fast, not mid-review."""


REGISTRY: dict[str, type[Rule]] = {}


def register(cls: type[Rule]) -> type[Rule]:
    """Class decorator adding a rule to the registry (registration order = report order)."""
    if cls.id in REGISTRY:
        raise GuardianError(f"Duplicate rule id {cls.id!r}")
    if cls.category not in CATEGORY_ORDER:
        raise GuardianError(f"{cls.id}: unknown category {cls.category!r}")
    REGISTRY[cls.id] = cls
    return cls


# --------------------------------------------------------------------------- shared helpers
def beam_label(beam: Any) -> str:
    name = str(beam.get("BeamName", "") or "").strip()
    number = beam.get("BeamNumber", "?")
    return f"Beam {number}" + (f" '{name}'" if name else "")


def treatment_beams(plan: Any) -> list[Any]:
    """Beams that deliver dose (setup/verification beams excluded); photon, electron and ion."""
    return [
        b
        for b in plan_beams(plan)
        if str(b.get("TreatmentDeliveryType", "TREATMENT")) == "TREATMENT"
    ]


def first_control_point(beam: Any) -> Any | None:
    cps = control_points(beam)
    return cps[0] if cps else None


def fmt(value: Any, digits: int = 3) -> str:
    if isinstance(value, float):
        return f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return str(value)
