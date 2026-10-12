"""RT Plan rules: approval, deliverable content, fractionation, prescription arithmetic, machine."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..context import PlanContext
from ..errors import CannotEvaluate, NotApplicable
from ..models import Check, Severity
from ..plan_model import brachy_setups
from .base import Rule, beam_label, fmt, register, treatment_beams


@register
class PlanApproved(Rule):
    id = "PLAN-001"
    title = "Plan approval status is acceptable"
    category = "plan"
    description = (
        "Only plans in an approved state should be exported to delivery or QA systems. "
        "`allowed_statuses` lists the ApprovalStatus values your site accepts. AAPM MPPG 11.a "
        "(Table 8) lists physician and physicist plan approval among the initial-check items."
    )
    reference = "Approval Module - ApprovalStatus (300E,0002); AAPM MPPG 11.a Tables 7-8"
    default_severity = Severity.WARNING
    requires = ("rtplan",)
    default_params = {"allowed_statuses": ["APPROVED"]}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        status = str(ctx.plan.get("ApprovalStatus", "") or "")
        allowed = list(params["allowed_statuses"])
        yield Check("ApprovalStatus is allowed", status in allowed, status or "(missing)", allowed)
        label = str(ctx.plan.get("RTPlanLabel", "") or "")
        yield Check("RTPlanLabel is present", bool(label), label or "(missing)", "non-empty")


@register
class PlanHasDeliverableContent(Rule):
    id = "PLAN-002"
    title = "Plan has deliverable content of a supported technique"
    category = "plan"
    description = (
        "A plan with neither treatment beams nor brachytherapy application setups cannot be "
        "delivered, and would otherwise pass every technique-specific rule as 'not applicable'. "
        "Beams of an unexpected radiation type (neutron...) are flagged because the other checks "
        "do not cover them. `allowed_radiation_types` lists the types your site delivers."
    )
    reference = (
        "RT Beams / RT Ion Beams Modules - RadiationType; RT Brachy Application Setups Module"
    )
    requires = ("rtplan",)
    default_params = {
        "allowed_radiation_types": ["PHOTON", "ELECTRON", "PROTON", "ION", "MIXED_ION"]
    }

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        beams = treatment_beams(ctx.plan)
        setups = brachy_setups(ctx.plan)
        yield Check(
            "Plan has at least one treatment beam or brachytherapy application setup",
            bool(beams or setups),
            f"{len(beams)} beam(s), {len(setups)} application setup(s)",
            ">= 1 in total",
        )
        allowed = [str(a).upper() for a in params["allowed_radiation_types"]]
        for beam in beams:
            rad = str(beam.get("RadiationType", "") or "").upper()
            yield Check(
                f"{beam_label(beam)} radiation type is supported",
                rad in allowed,
                rad or "(missing)",
                allowed,
            )


@register
class FractionationInRange(Rule):
    id = "PLAN-003"
    title = "Number of fractions is within the configured range"
    category = "plan"
    description = "Catches zero/negative fractions and typos such as 300 instead of 30."
    reference = "RT Fraction Scheme Module - NumberOfFractionsPlanned (300A,0078)"
    requires = ("rtplan",)
    default_params = {"min_fractions": 1, "max_fractions": 60}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        lo, hi = int(params["min_fractions"]), int(params["max_fractions"])
        groups = list(ctx.plan.get("FractionGroupSequence", []))
        yield Check("Plan has at least one fraction group", bool(groups), len(groups), ">= 1")
        for fg in groups:
            raw = fg.get("NumberOfFractionsPlanned")
            n = int(raw) if raw is not None else None
            yield Check(
                f"Fraction group {fg.get('FractionGroupNumber', '?')}: "
                f"NumberOfFractionsPlanned in [{lo}, {hi}]",
                n is not None and lo <= n <= hi,
                n if n is not None else "(missing)",
                f"{lo}..{hi}",
            )


@register
class PrescriptionMatchesDeliveredDose(Rule):
    id = "PLAN-004"
    title = "Prescription equals summed per-fraction dose x fractions"
    category = "plan"
    description = (
        "Two independent statements of the same quantity must agree: the prescribed target dose, "
        "and the per-fraction dose of the plan's beams (BeamDose) and/or brachytherapy application "
        "setups (BrachyApplicationSetupDose) multiplied by the number of fractions. A mismatch is "
        "a typo or a stale edit in one of them (AAPM MPPG 11.a: prescription and fractional/total "
        "dose agree with the plan). Only single-fraction-group, single-prescription plans are "
        "evaluated; others are reported as SKIPPED. Radiosurgery plans prescribe to an isodose line "
        "(Gamma Knife: BeamDose is the dose at the maximum-dose point), so the sum is scaled by the "
        "prescription isodose % - from the ruleset, or from the Elekta GammaPlan private element "
        "(an empirical reading, see plan_model.py) - and the identity is still verified."
    )
    reference = (
        "RT Prescription Module - TargetPrescriptionDose; RT Fraction Scheme - BeamDose, "
        "BrachyApplicationSetupDose; AAPM MPPG 11.a Tables 7-8"
    )
    requires = ("rtplan",)
    default_params = {
        "tolerance_percent": 1.0,
        "prescription_isodose_percent": 0.0,
        "use_vendor_isodose": True,
    }

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        plan = ctx.plan
        groups = list(plan.get("FractionGroupSequence", []))
        if len(groups) != 1:
            raise CannotEvaluate(
                f"{len(groups)} fraction groups; only single-group plans are supported"
            )
        rx_levels = {
            round(t.prescription_gy, 4) for t in ctx.declared_targets() if t.prescription_gy
        }
        if not rx_levels:
            raise CannotEvaluate("no TargetPrescriptionDose found in DoseReferenceSequence")
        if len(rx_levels) > 1:
            raise CannotEvaluate(
                f"multiple prescription levels {sorted(rx_levels)} (e.g. SIB) not supported"
            )
        rx = next(iter(rx_levels))
        fg = groups[0]
        fractions = fg.get("NumberOfFractionsPlanned")
        if fractions is None:
            raise CannotEvaluate("NumberOfFractionsPlanned missing")
        doses = [r.get("BeamDose") for r in fg.get("ReferencedBeamSequence", [])]
        doses += [
            r.get("BrachyApplicationSetupDose")
            for r in fg.get("ReferencedBrachyApplicationSetupSequence", [])
        ]
        if not doses or any(d is None for d in doses):
            raise CannotEvaluate(
                "BeamDose / BrachyApplicationSetupDose missing for referenced items"
            )
        isodose, why = ctx.prescription_isodose(
            float(params["prescription_isodose_percent"]), bool(params["use_vendor_isodose"])
        )
        per_fraction = sum(float(d) for d in doses)
        total = per_fraction * int(fractions) * isodose / 100.0
        tol = float(params["tolerance_percent"])
        deviation = 100.0 * (total - rx) / rx
        yield Check(
            f"Sum of per-fraction dose x fractions ({fmt(per_fraction)} Gy x {int(fractions)}) "
            "vs TargetPrescriptionDose",
            abs(deviation) <= tol,
            f"{fmt(total)} Gy",
            f"{fmt(rx)} Gy +/- {fmt(tol)}%",
            note=f"deviation {deviation:+.2f}%"
            + (f"; prescription isodose {fmt(isodose)}% ({why})" if isodose != 100.0 else ""),
        )


@register
class TreatmentMachine(Rule):
    id = "PLAN-005"
    title = "Treatment machine is named, consistent and allowed"
    category = "plan"
    description = (
        "All beams of a plan should be delivered on one machine, and it should be one the site "
        "has commissioned the plan type for (AAPM MPPG 11.a: treatment scheduled on the correct "
        "machine). For brachytherapy the afterloader named in TreatmentMachineSequence is checked. "
        "Set `allowed_machines` to enforce a whitelist."
    )
    reference = (
        "RT Beams Module - TreatmentMachineName (300A,00B2); Brachy - TreatmentMachineSequence"
    )
    requires = ("rtplan",)
    default_params = {"allowed_machines": [], "require_single_machine": True}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        plan = ctx.plan
        labelled: list[tuple[str, str]] = [
            (beam_label(b), str(b.get("TreatmentMachineName", "") or ""))
            for b in treatment_beams(plan)
        ]
        for machine in plan.get("TreatmentMachineSequence", []):
            labelled.append(
                ("Brachytherapy afterloader", str(machine.get("TreatmentMachineName", "") or ""))
            )
        if not labelled:
            raise NotApplicable("no treatment beams or brachytherapy machine (see PLAN-002)")
        allowed = list(params["allowed_machines"])
        for label, name in labelled:
            yield Check(
                f"{label} has a treatment machine name",
                bool(name),
                name or "(missing)",
                "non-empty",
            )
            if allowed:
                yield Check(
                    f"{label} machine is allowed", name in allowed, name or "(missing)", allowed
                )
        if params["require_single_machine"]:
            distinct = sorted({name for _, name in labelled})
            yield Check(
                "All beams use one treatment machine", len(distinct) == 1, distinct, "exactly one"
            )
