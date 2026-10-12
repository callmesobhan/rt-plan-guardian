"""Input completeness and patient-identity rules."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from ..context import KINDS, PlanContext
from ..errors import NotApplicable
from ..models import Check, Severity
from .base import Rule, register


@register
class RequiredObjectsPresent(Rule):
    id = "INPUT-001"
    title = "Required DICOM-RT objects were supplied"
    category = "input"
    description = (
        "A missing object means downstream cross-checks cannot run. The ruleset states which "
        "objects it expects, so an absent file is a visible failure rather than a silent gap."
    )
    reference = "SOP Class UIDs: RT Plan, RT Structure Set, RT Dose"
    default_params = {"required": ["rtplan", "rtstruct", "rtdose"]}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        for kind in params["required"]:
            if kind not in KINDS:
                raise ValueError(f"unknown object kind {kind!r}; expected one of {KINDS}")
            present = ctx.has(kind)
            yield Check(
                f"{kind.upper()} supplied",
                present,
                "present" if present else "missing",
                "present",
            )


@register
class NoUnassignedObjects(Rule):
    id = "INPUT-002"
    title = "Every supplied RT object belongs to the reviewed case"
    category = "input"
    description = (
        "When a folder holds several plans, doses and structure sets, objects are assembled into "
        "cases by the UID references the files carry. An object that cannot be linked (a dose for "
        "a plan that was not supplied, a structure set no plan uses) is reported here instead of "
        "being silently dropped or guessed into the wrong case."
    )
    reference = "RT Dose - ReferencedRTPlanSequence; RT Plan - ReferencedStructureSetSequence"
    default_severity = Severity.WARNING

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        if not ctx.unassigned:
            yield Check(
                "All supplied RT objects are assigned to a case", True, "none unassigned", "none"
            )
            return
        for item in ctx.unassigned:
            yield Check(f"{item.name} is assigned to a case", False, item.reason, "assigned")


@register
class SinglePlanLevelDose(Rule):
    id = "INPUT-003"
    title = "Exactly one plan-level dose identifies the case dose"
    category = "input"
    description = (
        "Several dose files may belong to one plan (per-beam, per-fraction, plan sum). Dose rules "
        "evaluate the one whose DoseSummationType is PLAN; if there is none or more than one the "
        "choice is ambiguous and the reviewer must say which is meant."
    )
    reference = "RT Dose Module - DoseSummationType (3004,000A)"
    default_severity = Severity.WARNING
    requires = ("rtdose",)

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        doses = ctx.get_all("rtdose")
        if len(doses) < 2:
            raise NotApplicable("only one dose object supplied")
        plan_level = [d for d in doses if str(d.get("DoseSummationType", "")) == "PLAN"]
        yield Check(
            f"{len(doses)} dose objects: exactly one has DoseSummationType PLAN",
            len(plan_level) == 1,
            f"{len(plan_level)} plan-level",
            "exactly 1",
        )


def _ref_label_ds(ctx: PlanContext):
    objs = ctx.labelled()
    if len(objs) < 2:
        raise NotApplicable("fewer than two DICOM-RT objects loaded; nothing to compare")
    return objs[0], objs


@register
class PatientIdConsistent(Rule):
    id = "IDENT-001"
    title = "Patient ID is identical across all objects"
    category = "identity"
    description = (
        "Objects from different patients must never be reviewed or delivered together. "
        "This is the highest-consequence consistency check in the tool."
    )
    reference = "DICOM Patient Module - PatientID (0010,0020)"

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        (ref_label, ref_ds), objs = _ref_label_ds(ctx)
        ref = str(ref_ds.get("PatientID", "") or "")
        yield Check(f"{ref_label} PatientID is present", bool(ref), ref or "(empty)", "non-empty")
        for label, ds in objs[1:]:
            pid = str(ds.get("PatientID", "") or "")
            yield Check(
                f"{label} PatientID matches {ref_label}",
                bool(pid) and pid == ref,
                pid or "(empty)",
                ref,
            )


def _norm_name(value: Any) -> str:
    text = str(value or "").upper().replace("^", " ").replace(",", " ")
    return re.sub(r"\s+", " ", text).strip()


@register
class PatientDemographicsConsistent(Rule):
    id = "IDENT-002"
    title = "Patient name, birth date and sex agree across objects"
    category = "identity"
    description = (
        "A second, independent identity signal: catches the case where an ID was re-keyed "
        "but the demographics reveal different patients."
    )
    reference = "DICOM Patient Module - PatientName, PatientBirthDate, PatientSex"
    default_severity = Severity.WARNING

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        (ref_label, ref_ds), objs = _ref_label_ds(ctx)
        attrs = (
            ("PatientName", _norm_name),
            ("PatientBirthDate", lambda v: str(v or "").strip()),
            ("PatientSex", lambda v: str(v or "").strip().upper()),
        )
        for label, ds in objs[1:]:
            for attr, norm in attrs:
                a, b = norm(ds.get(attr)), norm(ref_ds.get(attr))
                yield Check(
                    f"{label} {attr} matches {ref_label}", a == b, a or "(empty)", b or "(empty)"
                )


@register
class SopInstanceUidsUnique(Rule):
    id = "IDENT-003"
    title = "Each object has its own non-empty SOP Instance UID"
    category = "identity"
    description = (
        "Duplicate UIDs mean the same object was supplied twice under different roles, or a "
        "copy/paste export error - and make UID-based linkage checks meaningless."
    )
    reference = "DICOM SOP Common Module - SOPInstanceUID (0008,0018)"

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        uids = [(label, str(ds.get("SOPInstanceUID", "") or "")) for label, ds in ctx.labelled()]
        if not uids:
            raise NotApplicable("no objects loaded")
        for label, uid in uids:
            clash = [other for other, u in uids if u == uid and other != label]
            yield Check(
                f"{label} SOPInstanceUID is present and unique",
                bool(uid) and not clash,
                uid or "(empty)",
                "unique",
                note=f"also used by {', '.join(clash)}" if clash else "",
            )
