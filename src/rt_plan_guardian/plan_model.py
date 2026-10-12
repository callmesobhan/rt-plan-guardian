"""Technique-aware view of an RT Plan.

DICOM stores external-beam photon/electron plans in `BeamSequence` (RT Plan IOD), ion plans in
`IonBeamSequence` (RT Ion Plan IOD) and brachytherapy in `BrachyApplicationSetupSequence`. Rules
should not care which: they use these accessors, and declare which techniques they apply to.
"""

from __future__ import annotations

from typing import Any

from pydicom.dataset import Dataset

PHOTON, ELECTRON, PROTON, ION, BRACHY, NEUTRON = (
    "photon",
    "electron",
    "proton",
    "ion",
    "brachy",
    "neutron",
)

#: Techniques delivered with beams (control points, gantry, MU...).
EXTERNAL_BEAM = frozenset({PHOTON, ELECTRON, PROTON, ION, NEUTRON})
#: Techniques whose beams have a physical collimation system with MLC/jaws.
PHOTON_ONLY = frozenset({PHOTON})
CHARGED_PARTICLE = frozenset({PROTON, ION})
ALL_TECHNIQUES = EXTERNAL_BEAM | {BRACHY}

_RADIATION_TO_TECHNIQUE = {
    "PHOTON": PHOTON,
    "ELECTRON": ELECTRON,
    "PROTON": PROTON,
    "ION": ION,
    "MIXED_ION": ION,
    "NEUTRON": NEUTRON,
}


def plan_beams(plan: Dataset) -> list[Dataset]:
    """All beams, whether the plan stores them as BeamSequence or IonBeamSequence."""
    return [*plan.get("BeamSequence", []), *plan.get("IonBeamSequence", [])]


def control_points(beam: Dataset) -> list[Dataset]:
    """The beam's control points (ControlPointSequence or IonControlPointSequence)."""
    seq = beam.get("IonControlPointSequence")
    if seq is None:
        seq = beam.get("ControlPointSequence", [])
    return list(seq)


def is_ion_beam(beam: Dataset) -> bool:
    return "IonControlPointSequence" in beam


def beam_technique(beam: Dataset) -> str | None:
    return _RADIATION_TO_TECHNIQUE.get(str(beam.get("RadiationType", "") or "").upper())


def brachy_setups(plan: Dataset) -> list[Dataset]:
    return list(plan.get("ApplicationSetupSequence", []))


def plan_techniques(plan: Dataset) -> frozenset[str]:
    found: set[str] = set()
    for beam in plan_beams(plan):
        tech = beam_technique(beam)
        if tech:
            found.add(tech)
    if brachy_setups(plan) or "BrachyTreatmentTechnique" in plan:
        found.add(BRACHY)
    return frozenset(found)


def describe_techniques(techniques: frozenset[str]) -> str:
    return ", ".join(sorted(techniques)) or "no deliverable content"


def dosimeter_unit(beam: Dataset) -> str:
    """MU, MINUTE (cobalt/Gamma Knife timers) or NP (particles). DICOM default is MU."""
    return str(beam.get("PrimaryDosimeterUnit", "") or "MU").upper()


def sequence_item(items: Any, number_attr: str, number: int) -> Dataset | None:
    for item in items or []:
        if number_attr in item and int(item[number_attr].value) == number:
            return item
    return None


#: Elekta GammaPlan stores the prescription isodose (% of the maximum dose) in a private element of
#: each DoseReference item: block creator "ELEKTA GAMMAPLAN", element offset 0x27 of group 0009.
#: This is NOT from vendor documentation: it was established empirically on the 8 plans of 4
#: patients in the public Vestibular-Schwannoma-SEG collection, where sum(BeamDose) x isodose/100
#: reproduces TargetPrescriptionDose exactly (46% for two patients, 50% for the others). Because
#: PLAN-004 re-checks that identity, a wrong reading fails loudly instead of passing silently.
GAMMAPLAN_CREATOR = "ELEKTA GAMMAPLAN"
GAMMAPLAN_ISODOSE_OFFSET = 0x27


def vendor_isodose_percent(plan: Dataset) -> float | None:
    for ref in plan.get("DoseReferenceSequence", []):
        try:
            element = ref.get_private_item(0x0009, GAMMAPLAN_ISODOSE_OFFSET, GAMMAPLAN_CREATOR)
        except (KeyError, ValueError):
            continue
        raw = element.value
        try:
            value = float(raw.decode("ascii").strip() if isinstance(raw, bytes) else raw)
        except (ValueError, TypeError):
            continue
        if 0.0 < value <= 100.0:
            return value
    return None
