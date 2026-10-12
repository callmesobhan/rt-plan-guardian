"""Cross-object linkage rules: do the plan, structure set and dose actually belong together?"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..context import PlanContext
from ..errors import NotApplicable
from ..models import Check
from .base import Rule, register


def _referenced_uids(ds: Any, sequence: str) -> list[str]:
    return [str(item.get("ReferencedSOPInstanceUID", "")) for item in ds.get(sequence, [])]


@register
class PlanReferencesStructureSet(Rule):
    id = "LINK-001"
    title = "RT Plan references the supplied RT Structure Set"
    category = "linkage"
    description = (
        "Beam isocentres and dose-reference ROIs are only meaningful against the structure set "
        "the plan was made on. A different structure set silently changes what 'target' means."
    )
    reference = "RT General Plan Module - ReferencedStructureSetSequence (300C,0060)"
    requires = ("rtplan", "rtstruct")

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        refs = _referenced_uids(ctx.plan, "ReferencedStructureSetSequence")
        uid = str(ctx.structure_set.get("SOPInstanceUID", ""))
        yield Check(
            "RTPLAN.ReferencedStructureSetSequence contains RTSTRUCT SOPInstanceUID",
            uid in refs,
            refs or "(no ReferencedStructureSetSequence)",
            uid,
        )


@register
class DoseReferencesPlan(Rule):
    id = "LINK-002"
    title = "RT Dose references the supplied RT Plan"
    category = "linkage"
    description = (
        "A dose grid computed for a different plan (or an earlier revision) is the classic "
        "stale-export failure: numbers look plausible but describe the wrong beams."
    )
    reference = "RT Dose Module - ReferencedRTPlanSequence (300C,0002)"
    requires = ("rtplan", "rtdose")

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        refs = _referenced_uids(ctx.dose, "ReferencedRTPlanSequence")
        uid = str(ctx.plan.get("SOPInstanceUID", ""))
        yield Check(
            "RTDOSE.ReferencedRTPlanSequence contains RTPLAN SOPInstanceUID",
            uid in refs,
            refs or "(no ReferencedRTPlanSequence)",
            uid,
        )


def structure_set_frames(struct: Any) -> set[str]:
    return {
        str(i.get("FrameOfReferenceUID", ""))
        for i in struct.get("ReferencedFrameOfReferenceSequence", [])
        if i.get("FrameOfReferenceUID")
    }


@register
class DoseSharesFrameOfReference(Rule):
    id = "LINK-003"
    title = "RT Dose and RT Structure Set share a Frame of Reference"
    category = "linkage"
    description = (
        "Contours and dose are only spatially comparable if they use the same coordinate "
        "system. A mismatch means dose-to-structure statistics would be computed in the wrong place."
    )
    reference = "Frame of Reference Module - FrameOfReferenceUID (0020,0052)"
    requires = ("rtstruct", "rtdose")

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        frames = structure_set_frames(ctx.structure_set)
        dose_for = str(ctx.dose.get("FrameOfReferenceUID", "") or "")
        yield Check(
            "RTDOSE FrameOfReferenceUID is one of the RTSTRUCT frames of reference",
            bool(dose_for) and dose_for in frames,
            dose_for or "(empty)",
            sorted(frames) or "(none declared by RTSTRUCT)",
        )


@register
class DoseReferenceRoisExist(Rule):
    id = "LINK-004"
    title = "ROIs referenced by the plan's dose references exist in the structure set"
    category = "linkage"
    description = (
        "Prescriptions and constraints point at ROIs by number. A dangling number means the "
        "prescription is attached to nothing (or to the wrong structure after a renumber)."
    )
    reference = "RT Prescription Module - DoseReferenceSequence / ReferencedROINumber (3006,0084)"
    requires = ("rtplan", "rtstruct")

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        known = {
            int(r.ROINumber)
            for r in ctx.structure_set.get("StructureSetROISequence", [])
            if "ROINumber" in r
        }
        found = False
        for ref in ctx.plan.get("DoseReferenceSequence", []):
            if (
                str(ref.get("DoseReferenceStructureType", "")) != "VOLUME"
                or "ReferencedROINumber" not in ref
            ):
                continue
            found = True
            num = int(ref.ReferencedROINumber)
            label = str(ref.get("DoseReferenceDescription", "") or "")
            yield Check(
                f"DoseReference {ref.get('DoseReferenceNumber', '?')}"
                + (f" '{label}'" if label else "")
                + f" -> ROI {num} exists in RTSTRUCT",
                num in known,
                num,
                "one of " + str(sorted(known)),
            )
        if not found:
            raise NotApplicable("plan has no volume-type dose references")
