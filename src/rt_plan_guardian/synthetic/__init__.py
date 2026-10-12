"""Synthetic DICOM-RT cases (photon, electron, proton, brachytherapy) and fault-injection catalogue.

`make_case(technique=...)` builds a small, internally consistent clean case that passes every
applicable rule; `DEFECTS` maps a defect name to a mutation plus the exact set of rules that must
then fail. The test-suite walks that catalogue, so every rule is proven to catch its failure mode
and to stay quiet otherwise. No patient data is involved anywhere.
"""

from __future__ import annotations

from . import brachy, electron, ion, photon
from .common import DEFECTS, Defect, SyntheticCase
from .photon import PRESCRIPTION_GY, build_dose, build_plan, build_structure_set

_BUILDERS = {
    "photon": photon.make,
    "electron": electron.make,
    "proton": ion.make,
    "brachy": brachy.make,
}
TECHNIQUES = tuple(_BUILDERS)


def make_case(seed: int = 0, technique: str = "photon") -> SyntheticCase:
    """A clean synthetic case: every applicable default rule passes."""
    try:
        return _BUILDERS[technique](seed)
    except KeyError:
        raise ValueError(f"unknown technique {technique!r}; choose from {TECHNIQUES}") from None


__all__ = [
    "DEFECTS",
    "PRESCRIPTION_GY",
    "TECHNIQUES",
    "Defect",
    "SyntheticCase",
    "build_dose",
    "build_plan",
    "build_structure_set",
    "make_case",
]
