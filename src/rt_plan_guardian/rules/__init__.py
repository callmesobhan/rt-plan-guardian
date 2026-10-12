"""Rule catalog. Importing this package registers every built-in rule."""

from . import (  # noqa: F401  (registration side effects)
    beams,
    brachy,
    dose,
    electron,
    intake,
    ion,
    linkage,
    plan,
    protocol,
    structure,
)
from .base import CATEGORY_ORDER, REGISTRY, Rule, register

# Report order = CATEGORY_ORDER, then definition order within a category. Sorting here (rather
# than relying on import order, which formatters reshuffle) keeps reports stable and readable:
# input -> identity -> linkage -> structure -> plan -> beam -> ion -> electron -> brachy -> dose
# -> protocol.
_ordered = sorted(REGISTRY.items(), key=lambda item: CATEGORY_ORDER.index(item[1].category))
REGISTRY.clear()
REGISTRY.update(_ordered)

__all__ = ["CATEGORY_ORDER", "REGISTRY", "Rule", "register"]
