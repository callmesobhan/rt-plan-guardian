"""Beam-level rules shared by photon, electron and ion plans.

Written against `plan_model` accessors so the same checks run on RT Plan (BeamSequence) and
RT Ion Plan (IonBeamSequence) objects. Collimator hardware checks (MLC/jaws) are photon-only.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from ..context import PlanContext
from ..errors import CannotEvaluate, NotApplicable
from ..models import Check, Severity
from ..plan_model import (
    CHARGED_PARTICLE,
    EXTERNAL_BEAM,
    PHOTON_ONLY,
    beam_technique,
    control_points,
    dosimeter_unit,
    plan_beams,
)
from .base import Rule, beam_label, first_control_point, fmt, register, treatment_beams


def _note(problems: list[str], limit: int = 3) -> str:
    return "; ".join(problems[:limit]) + (
        f" (+{len(problems) - limit} more)" if len(problems) > limit else ""
    )


@register
class BeamReferencesConsistent(Rule):
    id = "BEAM-001"
    title = "Beam numbers are unique and match the fraction groups"
    category = "beam"
    description = (
        "Dose and MU are attached to beams by number via the fraction groups. A duplicated beam "
        "number, a referenced beam that does not exist, or a treatment beam that no fraction "
        "group delivers means the plan as exported is not the plan as intended."
    )
    reference = (
        "RT Beams / RT Ion Beams Module - BeamNumber; RT Fraction Scheme - ReferencedBeamSequence"
    )
    requires = ("rtplan",)
    applies_to = EXTERNAL_BEAM

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        plan = ctx.plan
        numbers = [int(b.BeamNumber) for b in plan_beams(plan) if "BeamNumber" in b]
        dups = sorted(n for n, c in Counter(numbers).items() if c > 1)
        yield Check("Beam numbers are unique", not dups, dups or "all unique", "unique")
        groups = list(plan.get("FractionGroupSequence", []))
        referenced: set[int] = set()
        for fg in groups:
            refs = [
                int(r.ReferencedBeamNumber)
                for r in fg.get("ReferencedBeamSequence", [])
                if "ReferencedBeamNumber" in r
            ]
            referenced.update(refs)
            missing = sorted(set(refs) - set(numbers))
            twice = sorted(n for n, c in Counter(refs).items() if c > 1)
            fgn = fg.get("FractionGroupNumber", "?")
            yield Check(
                f"Fraction group {fgn}: referenced beams exist in the plan",
                not missing,
                missing or "all exist",
                "all exist",
            )
            yield Check(
                f"Fraction group {fgn}: no beam is referenced twice",
                not twice,
                twice or "none",
                "none",
            )
        if not groups:
            yield Check(
                "Plan has a fraction group referencing its beams",
                False,
                "no FractionGroupSequence",
                ">= 1 group",
            )
            return
        undelivered = sorted(
            int(b.BeamNumber)
            for b in treatment_beams(plan)
            if int(b.get("BeamNumber", -1)) not in referenced
        )
        yield Check(
            "Every treatment beam is referenced by a fraction group",
            not undelivered,
            undelivered or "all referenced",
            "all referenced",
        )


@register
class ControlPointsIntact(Rule):
    id = "BEAM-002"
    title = "Control points are complete and meterset weights are monotonic"
    category = "beam"
    description = (
        "Control points define how the beam is delivered. They must be numbered 0..N-1, the "
        "count must match the declaration, and cumulative meterset weight must run from 0 to "
        "FinalCumulativeMetersetWeight without decreasing; otherwise MU would be delivered "
        "at the wrong gantry/MLC/energy state."
    )
    reference = "RT Beams / RT Ion Beams Module - Control Point Sequence, CumulativeMetersetWeight (300A,0134)"
    requires = ("rtplan",)
    applies_to = EXTERNAL_BEAM
    default_params = {"weight_tolerance": 1e-4}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        tol = float(params["weight_tolerance"])
        beams = treatment_beams(ctx.plan)
        if not beams:
            raise NotApplicable("no treatment beams (see PLAN-002)")
        for beam in beams:
            cps = control_points(beam)
            problems: list[str] = []
            declared = beam.get("NumberOfControlPoints")
            if declared is None or int(declared) != len(cps):
                problems.append(f"declares {declared} control points, has {len(cps)}")
            if len(cps) < 2:
                problems.append("fewer than 2 control points")
            idx = [int(cp.get("ControlPointIndex", -1)) for cp in cps]
            if idx != list(range(len(cps))):
                problems.append("ControlPointIndex is not 0..N-1 in order")
            weights = [
                float(cp.CumulativeMetersetWeight) if "CumulativeMetersetWeight" in cp else math.nan
                for cp in cps
            ]
            if not weights or any(math.isnan(w) for w in weights):
                problems.append("CumulativeMetersetWeight missing on one or more control points")
            else:
                final = float(beam.get("FinalCumulativeMetersetWeight", 1.0))
                if abs(weights[0]) > tol:
                    problems.append(f"first weight is {fmt(weights[0])}, expected 0")
                if abs(weights[-1] - final) > tol * max(1.0, abs(final)):
                    problems.append(
                        f"last weight is {fmt(weights[-1])}, expected FinalCumulativeMetersetWeight {fmt(final)}"
                    )
                if np.any(np.diff(weights) < -tol * max(1.0, abs(final))):
                    problems.append("weights decrease between control points")
            yield Check(
                f"{beam_label(beam)}: {len(cps)} control points consistent",
                not problems,
                "consistent" if not problems else f"{len(problems)} problem(s)",
                "consistent",
                note=_note(problems),
            )


@register
class MachineAnglesInRange(Rule):
    id = "BEAM-003"
    title = "Gantry, collimator and couch angles are present and in [0, 360)"
    category = "beam"
    description = (
        "Angles outside the DICOM range, or absent on the first control point, mean the "
        "geometry is undefined or was edited by hand. Required angles depend on the technique: "
        "ion beams have no collimator angle, so only gantry is required there."
    )
    reference = "RT Beams Module - GantryAngle, BeamLimitingDeviceAngle, PatientSupportAngle"
    requires = ("rtplan",)
    applies_to = EXTERNAL_BEAM
    _TAGS = (
        ("GantryAngle", "gantry"),
        ("BeamLimitingDeviceAngle", "collimator"),
        ("PatientSupportAngle", "couch"),
    )

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        beams = treatment_beams(ctx.plan)
        if not beams:
            raise NotApplicable("no treatment beams (see PLAN-002)")
        for beam in beams:
            cps = control_points(beam)
            charged = beam_technique(beam) in CHARGED_PARTICLE
            required = {"GantryAngle"} if charged else {t for t, _ in self._TAGS}
            problems: list[str] = []
            if cps:
                for tag, label in self._TAGS:
                    if tag in required and tag not in cps[0]:
                        problems.append(f"{label} angle missing at control point 0")
            for i, cp in enumerate(cps):
                for tag, label in self._TAGS:
                    if tag in cp:
                        value = float(cp.get(tag))
                        if not (0.0 <= value < 360.0):
                            problems.append(f"CP {i} {label} angle {fmt(value)} outside [0, 360)")
            yield Check(
                f"{beam_label(beam)} angles valid",
                not problems,
                "valid" if not problems else f"{len(problems)} problem(s)",
                "all in [0, 360)",
                note=_note(problems),
            )


@register
class BeamEnergyAllowed(Rule):
    id = "BEAM-004"
    title = "Nominal beam energy is present, positive and allowed"
    category = "beam"
    description = (
        "A zero or missing energy cannot be delivered. `allowed_energies` maps a radiation type "
        "(PHOTON, ELECTRON, PROTON, ION) to the energies commissioned on your machines "
        "(MV for photons, MeV for electrons, MeV/u for ions) to catch an energy that does not exist."
    )
    reference = "RT Beams Module - NominalBeamEnergy (300A,0114)"
    requires = ("rtplan",)
    applies_to = EXTERNAL_BEAM
    default_params = {"allowed_energies": {}}

    @classmethod
    def validate_params(cls, params: Mapping[str, Any]) -> None:
        for radiation, energies in params["allowed_energies"].items():
            if not isinstance(energies, list) or not all(
                isinstance(e, int | float) and not isinstance(e, bool) for e in energies
            ):
                raise ValueError(f"allowed_energies[{radiation}] must be a list of numbers")

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        allowed_by_type = {
            str(k).upper(): [float(e) for e in v] for k, v in params["allowed_energies"].items()
        }
        beams = treatment_beams(ctx.plan)
        if not beams:
            raise NotApplicable("no treatment beams (see PLAN-002)")
        for beam in beams:
            cp0 = first_control_point(beam)
            raw = cp0.get("NominalBeamEnergy") if cp0 is not None else None
            energy = float(raw) if raw is not None else None
            allowed = allowed_by_type.get(str(beam.get("RadiationType", "")).upper(), [])
            ok = energy is not None and energy > 0 and (not allowed or energy in allowed)
            yield Check(
                f"{beam_label(beam)} energy",
                ok,
                energy if energy is not None else "(missing)",
                f"> 0 and in {allowed}" if allowed else "> 0",
            )


@register
class BeamMetersetLimits(Rule):
    id = "BEAM-005"
    title = "Beam meterset is present, positive and within limits for its unit"
    category = "beam"
    description = (
        "Meterset per beam must exist and be plausible. Limits are set per dosimeter unit because "
        "the unit is not always MU: cobalt and Gamma Knife plans use MINUTE, particle systems "
        "may use NP. Zero/negative values and values beyond the configured limit indicate a "
        "calculation or export error."
    )
    reference = "RT Fraction Scheme Module - BeamMeterset (300A,0086); RT Beams - PrimaryDosimeterUnit (300A,00B3)"
    requires = ("rtplan",)
    applies_to = EXTERNAL_BEAM
    default_params = {"limits": {"MU": [1.0, 2000.0], "MINUTE": [0.0, 120.0], "NP": [0.0, 1.0e15]}}

    @classmethod
    def validate_params(cls, params: Mapping[str, Any]) -> None:
        for unit, pair in params["limits"].items():
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(x, int | float) and not isinstance(x, bool) for x in pair)
                or pair[0] > pair[1]
            ):
                raise ValueError(f"limits[{unit}] must be [min, max] with min <= max")

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        limits = {str(k).upper(): (float(v[0]), float(v[1])) for k, v in params["limits"].items()}
        beams = {int(b.BeamNumber): b for b in plan_beams(ctx.plan) if "BeamNumber" in b}
        any_ref = False
        for fg in ctx.plan.get("FractionGroupSequence", []):
            for ref in fg.get("ReferencedBeamSequence", []):
                any_ref = True
                number = int(ref.get("ReferencedBeamNumber", -1))
                beam = beams.get(number)
                unit = dosimeter_unit(beam) if beam is not None else "MU"
                if unit not in limits:
                    raise CannotEvaluate(
                        f"no meterset limits configured for dosimeter unit {unit!r}"
                    )
                lo, hi = limits[unit]
                raw = ref.get("BeamMeterset")
                value = float(raw) if raw is not None else None
                yield Check(
                    f"Fraction group {fg.get('FractionGroupNumber', '?')}, beam {number}: "
                    f"{unit} in ({fmt(lo)}, {fmt(hi)}]",
                    value is not None and value > 0 and lo <= value <= hi,
                    f"{fmt(value)} {unit}" if value is not None else "(missing)",
                    f"> 0 and {fmt(lo)}..{fmt(hi)} {unit}",
                )
        if not any_ref:
            raise NotApplicable("no referenced beams in any fraction group (see BEAM-001)")


@register
class IsocentreInsideTarget(Rule):
    id = "BEAM-006"
    title = "Beam isocentre lies inside a target volume"
    category = "beam"
    description = (
        "Cross-checks plan geometry against the structure set: the isocentre should normally be "
        "inside the target. Legitimate exceptions exist (multi-target SBRT, fixed-point "
        "isocentres, electron SSD set-ups), hence WARNING severity. Targets come from the plan's "
        "DoseReferenceSequence (matched to ROIs by number, or by description for SITE references); "
        "`target_patterns` (regex on ROI names) is the fallback. Electron beams are skipped by "
        "default via `radiation_types`. Multi-isocentre plans (e.g. Gamma Knife shots) check each one."
    )
    reference = "RT Beams Module - IsocenterPosition (300A,012C); Structure Set contours"
    default_severity = Severity.WARNING
    requires = ("rtplan", "rtstruct")
    applies_to = EXTERNAL_BEAM
    default_params = {
        "target_patterns": ["^PTV"],
        "radiation_types": ["PHOTON", "PROTON", "ION", "MIXED_ION"],
    }

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        geoms = [g for _, g in ctx.target_geometries(list(params["target_patterns"]))]
        names = [g.name for g in geoms]
        types = {str(t).upper() for t in params["radiation_types"]}
        beams = [
            b for b in treatment_beams(ctx.plan) if str(b.get("RadiationType", "")).upper() in types
        ]
        if not beams:
            raise NotApplicable("no treatment beams of the configured radiation types")
        for beam in beams:
            cp0 = first_control_point(beam)
            iso = cp0.get("IsocenterPosition") if cp0 is not None else None
            if iso is None or len(iso) != 3:
                yield Check(
                    f"{beam_label(beam)} isocentre inside target",
                    False,
                    "(missing)",
                    f"inside one of {names}",
                )
                continue
            x, y, z = (float(v) for v in iso)
            inside = [g.name for g in geoms if g.contains(x, y, z)]
            yield Check(
                f"{beam_label(beam)} isocentre inside target",
                bool(inside),
                [round(x, 2), round(y, 2), round(z, 2)],
                f"inside one of {names}",
                note=f"inside {inside}" if inside else "",
            )


def _device_positions(beam: Any) -> Iterable[tuple[int, str, np.ndarray]]:
    for i, cp in enumerate(control_points(beam)):
        for dev in cp.get("BeamLimitingDevicePositionSequence", []):
            kind = str(dev.get("RTBeamLimitingDeviceType", ""))
            pos = np.asarray([float(v) for v in dev.get("LeafJawPositions", [])], dtype=float)
            yield i, kind, pos


@register
class MlcAndJawSanity(Rule):
    id = "BEAM-007"
    title = "MLC leaves and jaws are physically sane (no crossed pairs, within travel)"
    category = "beam"
    description = (
        "Checks every control point: opposing leaves/jaws do not cross (bank A <= bank B, with at "
        "most `min_leaf_gap_mm` closure) and positions stay within a configured travel limit. "
        "Crossed leaves are a physically impossible delivery. The check pairs the first half of "
        "`LeafJawPositions` with the second half, independent of how the export declares the "
        "device (see BEAM-008 for declaration conformance)."
    )
    reference = "RT Beams Module - BeamLimitingDevicePositionSequence, LeafJawPositions (300A,011C)"
    requires = ("rtplan",)
    applies_to = PHOTON_ONLY
    default_params = {"min_leaf_gap_mm": 0.0, "max_abs_position_mm": 200.0}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        min_gap = float(params["min_leaf_gap_mm"])
        max_abs = float(params["max_abs_position_mm"])
        evaluated = False
        for beam in treatment_beams(ctx.plan):
            problems: list[str] = []
            n_positions = 0
            for i, kind, pos in _device_positions(beam):
                evaluated = True
                n_positions += 1
                if len(pos) == 0 or len(pos) % 2:
                    problems.append(
                        f"CP {i} {kind or '?'}: {len(pos)} positions (need an even number > 0)"
                    )
                    continue
                n = len(pos) // 2
                gap = pos[n:] - pos[:n]
                crossed = np.flatnonzero(gap < min_gap - 1e-6)
                if crossed.size:
                    problems.append(
                        f"CP {i} {kind}: pair(s) {crossed.tolist()} closed beyond {fmt(min_gap)} mm gap"
                    )
                if np.abs(pos).max(initial=0.0) > max_abs:
                    problems.append(f"CP {i} {kind}: position beyond +/-{fmt(max_abs)} mm")
            if n_positions:
                yield Check(
                    f"{beam_label(beam)} MLC/jaw positions valid",
                    not problems,
                    "valid" if not problems else f"{len(problems)} problem(s)",
                    "valid",
                    note=_note(problems),
                )
        if not evaluated:
            raise NotApplicable("no beam-limiting device positions present")


@register
class BeamLimitingDeclarations(Rule):
    id = "BEAM-008"
    title = "Beam-limiting devices are declared consistently with their positions"
    category = "beam"
    description = (
        "DICOM requires every positioned device to be declared in BeamLimitingDeviceSequence and "
        "its position count to equal 2 x NumberOfLeafJawPairs. Some vendors export jaws/collimators "
        "with a pair count of 0; that is a conformance deviation (a warning) rather than a physical "
        "hazard, so it is reported separately from BEAM-007."
    )
    reference = "RT Beams Module - BeamLimitingDeviceSequence, NumberOfLeafJawPairs (300A,00BC)"
    default_severity = Severity.WARNING
    requires = ("rtplan",)
    applies_to = PHOTON_ONLY

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        evaluated = False
        for beam in treatment_beams(ctx.plan):
            pairs = {
                str(d.get("RTBeamLimitingDeviceType", "")): int(d.get("NumberOfLeafJawPairs", 0))
                for d in beam.get("BeamLimitingDeviceSequence", [])
            }
            problems: list[str] = []
            seen = False
            for i, kind, pos in _device_positions(beam):
                seen = evaluated = True
                n = pairs.get(kind)
                if n is None:
                    problems.append(
                        f"CP {i}: device {kind or '?'} not declared in BeamLimitingDeviceSequence"
                    )
                elif len(pos) != 2 * n:
                    problems.append(
                        f"CP {i} {kind}: {len(pos)} positions, declared pairs {n} (expected {2 * n})"
                    )
            if seen:
                unique = sorted(set(problems))
                yield Check(
                    f"{beam_label(beam)} device declarations conform",
                    not problems,
                    "conform" if not problems else f"{len(unique)} distinct issue(s)",
                    "conform",
                    note=_note(unique),
                )
        if not evaluated:
            raise NotApplicable("no beam-limiting device positions present")
