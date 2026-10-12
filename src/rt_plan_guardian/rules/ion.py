"""Proton / ion beam rules (RT Ion Plan IOD).

The expectations encoded here come from the DICOM Standard, PS3.3 section C.8.8.25 (RT Ion Beams
Module) and C.8.8.25.7 (Ion Control Point Sequence): each irradiation segment is two control
points with identical spot positions; the first carries the spot weights, which sum to the
cumulative-meterset difference to the next control point; the second carries zero weights.
Machine limits (energy range, field size) are site/vendor specific and therefore parameters.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from ..context import PlanContext
from ..errors import NotApplicable
from ..models import Check, Severity
from ..plan_model import CHARGED_PARTICLE, control_points
from .base import Rule, beam_label, fmt, register, treatment_beams

_SCANNING = ("MODULATED", "MODULATED_SPEC")


def _ion_beams(ctx: PlanContext) -> list[Any]:
    beams = [b for b in treatment_beams(ctx.plan) if "IonControlPointSequence" in b]
    if not beams:
        raise NotApplicable("no ion beams in this plan")
    return beams


def _scanning(beam: Any) -> bool:
    return str(beam.get("ScanMode", "")).upper() in _SCANNING


def _note(problems: list[str], limit: int = 3) -> str:
    return "; ".join(problems[:limit]) + (
        f" (+{len(problems) - limit} more)" if len(problems) > limit else ""
    )


def _floats(cp: Any, keyword: str) -> np.ndarray:
    return np.asarray([float(v) for v in cp.get(keyword, [])], dtype=float)


@register
class IonBeamDefinition(Rule):
    id = "ION-001"
    title = "Ion beam definition and scanning parameters are complete"
    category = "ion"
    description = (
        "A scanned ion beam is only deliverable if the scan mode is known, the control points come "
        "in pairs (two per irradiation segment), the machine geometry is given (virtual source-axis "
        "distances, dosimeter unit), and every modulated control point carries its spot-tune ID, "
        "spot count, spot map, weights and painting count. Ions heavier than protons must also "
        "identify the species."
    )
    reference = (
        "DICOM PS3.3 C.8.8.25 RT Ion Beams Module (Scan Mode, Number of Control Points, ...)"
    )
    requires = ("rtplan",)
    applies_to = CHARGED_PARTICLE
    default_params = {"allowed_scan_modes": ["NONE", "UNIFORM", "MODULATED", "MODULATED_SPEC"]}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        modes = {str(m).upper() for m in params["allowed_scan_modes"]}
        for beam in _ion_beams(ctx):
            problems: list[str] = []
            cps = control_points(beam)
            mode = str(beam.get("ScanMode", "")).upper()
            if mode not in modes:
                problems.append(f"ScanMode {mode or 'missing'} not in {sorted(modes)}")
            if len(cps) < 2 or len(cps) % 2:
                problems.append(
                    f"{len(cps)} control points (need an even number >= 2: two per segment)"
                )
            if str(beam.get("PrimaryDosimeterUnit", "")).upper() not in ("MU", "NP"):
                problems.append("PrimaryDosimeterUnit must be MU or NP")
            vsad = beam.get("VirtualSourceAxisDistances")
            if vsad is None or len(vsad) != 2 or not all(float(v) > 0 for v in vsad):
                problems.append("VirtualSourceAxisDistances must be two positive values")
            if str(beam.get("RadiationType", "")).upper() in ("ION", "MIXED_ION"):
                for tag in ("RadiationMassNumber", "RadiationAtomicNumber", "RadiationChargeState"):
                    if tag not in beam:
                        problems.append(f"{tag} missing for RadiationType ION")
            if cps:
                for tag in ("NominalBeamEnergy", "GantryAngle"):
                    if tag not in cps[0]:
                        problems.append(f"{tag} missing at control point 0")
            if mode in _SCANNING:
                for i, cp in enumerate(cps):
                    for tag in (
                        "ScanSpotTuneID",
                        "NumberOfScanSpotPositions",
                        "ScanSpotPositionMap",
                        "ScanSpotMetersetWeights",
                        "NumberOfPaintings",
                    ):
                        if tag not in cp:
                            problems.append(f"CP {i}: {tag} missing for {mode} scanning")
            yield Check(
                f"{beam_label(beam)} ion beam definition",
                not problems,
                "complete" if not problems else f"{len(problems)} problem(s)",
                "complete",
                note=_note(problems),
            )


@register
class ScanSpotMapsWellFormed(Rule):
    id = "ION-002"
    title = "Scan spot maps and weights are well-formed"
    category = "ion"
    description = (
        "For N spots the position map holds 2N values (x,y pairs in the isocentric plane), there "
        "are N weights, the declared count matches, weights are finite and non-negative, and the "
        "painting count is at least one. A truncated map or a negative weight corrupts the delivery "
        "silently."
    )
    reference = "DICOM PS3.3 C.8.8.25 - Scan Spot Position Map, Scan Spot Meterset Weights"
    requires = ("rtplan",)
    applies_to = CHARGED_PARTICLE

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        scanning = [b for b in _ion_beams(ctx) if _scanning(b)]
        if not scanning:
            raise NotApplicable("no beam uses a MODULATED scan mode")
        for beam in scanning:
            problems: list[str] = []
            total_spots = 0
            for i, cp in enumerate(control_points(beam)):
                pos, weights = (
                    _floats(cp, "ScanSpotPositionMap"),
                    _floats(cp, "ScanSpotMetersetWeights"),
                )
                declared = cp.get("NumberOfScanSpotPositions")
                n = int(declared) if declared is not None else -1
                total_spots += max(n, 0)
                if n < 1:
                    problems.append(f"CP {i}: NumberOfScanSpotPositions {declared}")
                if len(pos) != 2 * n:
                    problems.append(
                        f"CP {i}: position map has {len(pos)} values, expected {2 * max(n, 0)}"
                    )
                if len(weights) != n:
                    problems.append(f"CP {i}: {len(weights)} weights for {n} spots")
                if not (np.isfinite(pos).all() and np.isfinite(weights).all()):
                    problems.append(f"CP {i}: non-finite spot values")
                if (weights < 0).any():
                    problems.append(f"CP {i}: negative spot weight")
                paintings = cp.get("NumberOfPaintings")
                if paintings is None or int(paintings) < 1:
                    problems.append(f"CP {i}: NumberOfPaintings {paintings}")
            yield Check(
                f"{beam_label(beam)} spot maps ({total_spots} spot entries)",
                not problems,
                "well-formed" if not problems else f"{len(problems)} problem(s)",
                "well-formed",
                note=_note(problems),
            )


@register
class SegmentPairingAndWeights(Rule):
    id = "ION-003"
    title = "Spot weights and segment pairing follow the DICOM scanning model"
    category = "ion"
    description = (
        "Each irradiation segment is a pair of control points with identical spot positions. The "
        "first carries the weights, which must sum to the cumulative-meterset step to the next "
        "control point; the second carries zero weights; the cumulative meterset must not change "
        "between segments. A violation means the planned layer MU and the delivered layer MU "
        "differ."
    )
    reference = "DICOM PS3.3 C.8.8.25.7 Ion Control Point Sequence"
    requires = ("rtplan",)
    applies_to = CHARGED_PARTICLE
    default_params = {"relative_tolerance": 1e-3}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        rtol = float(params["relative_tolerance"])
        scanning = [b for b in _ion_beams(ctx) if _scanning(b)]
        if not scanning:
            raise NotApplicable("no beam uses a MODULATED scan mode")
        for beam in scanning:
            cps = control_points(beam)
            problems: list[str] = []
            cmw = [
                float(cp.CumulativeMetersetWeight) if "CumulativeMetersetWeight" in cp else math.nan
                for cp in cps
            ]
            if len(cps) % 2 or any(math.isnan(w) for w in cmw):
                problems.append(
                    "cannot pair control points (odd count or missing cumulative weight)"
                )
            else:
                scale = max(1.0, abs(cmw[-1]))
                for k in range(0, len(cps), 2):
                    a, b = cps[k], cps[k + 1]
                    seg = k // 2
                    if not np.array_equal(
                        _floats(a, "ScanSpotPositionMap"), _floats(b, "ScanSpotPositionMap")
                    ):
                        problems.append(
                            f"segment {seg}: spot positions differ between its two control points"
                        )
                    if np.any(np.abs(_floats(b, "ScanSpotMetersetWeights")) > 0):
                        problems.append(
                            f"segment {seg}: closing control point has non-zero weights"
                        )
                    step = cmw[k + 1] - cmw[k]
                    total = float(_floats(a, "ScanSpotMetersetWeights").sum())
                    if abs(total - step) > rtol * scale + 1e-9:
                        problems.append(
                            f"segment {seg}: spot weights sum {fmt(total)} != meterset step {fmt(step)}"
                        )
                    if k + 2 < len(cps) and abs(cmw[k + 2] - cmw[k + 1]) > rtol * scale + 1e-9:
                        problems.append(f"segment {seg}: meterset changes between segments")
            yield Check(
                f"{beam_label(beam)} {len(cps) // 2} segments",
                not problems,
                "consistent" if not problems else f"{len(problems)} problem(s)",
                "consistent",
                note=_note(problems),
            )


@register
class IonEnergyRange(Rule):
    id = "ION-004"
    title = "Beam energies are positive and within the machine range"
    category = "ion"
    description = (
        "Every control point's nominal energy (MeV per nucleon at the nozzle) must be positive and "
        "inside the range the accelerator and energy-selection system can deliver. The shipped "
        "bounds are deliberately wide (they only catch zero/absurd values): set the real range of "
        "your facility."
    )
    reference = (
        "DICOM PS3.3 C.8.8.25 - Nominal Beam Energy (300A,0114); facility commissioning data"
    )
    requires = ("rtplan",)
    applies_to = CHARGED_PARTICLE
    default_params = {"min_energy_mev": 0.0, "max_energy_mev": 1000.0}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        lo, hi = float(params["min_energy_mev"]), float(params["max_energy_mev"])
        for beam in _ion_beams(ctx):
            energies = [
                float(cp.NominalBeamEnergy)
                for cp in control_points(beam)
                if "NominalBeamEnergy" in cp
            ]
            bad = sorted({e for e in energies if not (e > 0 and lo <= e <= hi)})
            yield Check(
                f"{beam_label(beam)} {len(set(energies))} distinct layer energies",
                bool(energies) and not bad,
                f"{fmt(min(energies))}..{fmt(max(energies))} MeV" if energies else "(none)",
                f"> 0 and within {fmt(lo)}..{fmt(hi)} MeV",
                note=f"out of range: {bad}" if bad else "",
            )


@register
class ScanSpotsInsideField(Rule):
    id = "ION-005"
    title = "Scan spots lie within the maximum scan field"
    category = "ion"
    description = (
        "Spot positions are in mm in the isocentric plane. A spot beyond the nozzle's scanning "
        "range cannot be delivered. The shipped limit is a generic upper bound; set your nozzle's."
    )
    reference = "DICOM PS3.3 C.8.8.25 - Scan Spot Position Map (300A,0394); nozzle specification"
    requires = ("rtplan",)
    applies_to = CHARGED_PARTICLE
    default_params = {"max_abs_spot_position_mm": 300.0}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        limit = float(params["max_abs_spot_position_mm"])
        scanning = [b for b in _ion_beams(ctx) if _scanning(b)]
        if not scanning:
            raise NotApplicable("no beam uses a MODULATED scan mode")
        for beam in scanning:
            peak = 0.0
            for cp in control_points(beam):
                pos = _floats(cp, "ScanSpotPositionMap")
                if pos.size:
                    peak = max(peak, float(np.abs(pos).max()))
            yield Check(
                f"{beam_label(beam)} largest spot coordinate",
                peak <= limit,
                f"{fmt(peak)} mm",
                f"<= {fmt(limit)} mm",
            )


@register
class RangeShifterAndSnout(Rule):
    id = "ION-006"
    title = "Range shifter and snout settings are consistent"
    category = "ion"
    description = (
        "Range-shifter settings must reference a range shifter the beam declares, and a beam with a "
        "snout must give its position (mm from isocentre) within a configured travel. Dangling "
        "references mean the physical accessory in the nozzle is unknown to the plan."
    )
    reference = "DICOM PS3.3 C.8.8.25 - Range Shifter Sequence, Range Shifter Settings, Snout Position (300A,030D)"
    requires = ("rtplan",)
    applies_to = CHARGED_PARTICLE
    default_severity = Severity.ERROR
    default_params = {"max_snout_position_mm": 700.0}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        limit = float(params["max_snout_position_mm"])
        evaluated = False
        for beam in _ion_beams(ctx):
            declared = {
                int(r.RangeShifterNumber)
                for r in beam.get("RangeShifterSequence", [])
                if "RangeShifterNumber" in r
            }
            has_snout = "SnoutSequence" in beam
            problems: list[str] = []
            uses = False
            cps = control_points(beam)
            for i, cp in enumerate(cps):
                for setting in cp.get("RangeShifterSettingsSequence", []):
                    uses = True
                    ref = setting.get("ReferencedRangeShifterNumber")
                    if ref is None or int(ref) not in declared:
                        problems.append(f"CP {i}: range shifter {ref} is not declared by the beam")
                if "SnoutPosition" in cp:
                    uses = True
                    pos = cp.get("SnoutPosition")
                    if pos is None or not (0.0 <= float(pos) <= limit):
                        problems.append(f"CP {i}: snout position {pos} outside 0..{fmt(limit)} mm")
            if has_snout and cps and "SnoutPosition" not in cps[0]:
                uses = True
                problems.append(
                    "SnoutSequence declared but SnoutPosition missing at control point 0"
                )
            if declared or uses or has_snout:
                evaluated = True
                yield Check(
                    f"{beam_label(beam)} range shifter / snout",
                    not problems,
                    "consistent" if not problems else f"{len(problems)} problem(s)",
                    "consistent",
                    note=_note(problems),
                )
        if not evaluated:
            raise NotApplicable("no range shifter or snout used by any ion beam")
