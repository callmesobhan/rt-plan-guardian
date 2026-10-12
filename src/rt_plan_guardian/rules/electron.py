"""Electron beam rules.

AAPM MPPG 11.a (Table 9) lists the electron-specific items therapists and physicists verify at
initial review: applicator, cone and block (cutout) code. DICOM carries these in the RT Beams
Module (ApplicatorSequence, BlockSequence) and the control point's source-to-surface distance.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from ..context import PlanContext
from ..errors import NotApplicable
from ..models import Check, Severity
from ..plan_model import ELECTRON, beam_technique, control_points
from .base import Rule, beam_label, first_control_point, fmt, register, treatment_beams

_ELECTRON_APPLICATORS = [
    "ELECTRON_SQUARE",
    "ELECTRON_RECT",
    "ELECTRON_CIRC",
    "ELECTRON_SHORT",
    "ELECTRON_OPEN",
    "INTRAOPERATIVE",
    "STEREOTACTIC",
]


def _electron_beams(ctx: PlanContext) -> list[Any]:
    beams = [b for b in treatment_beams(ctx.plan) if beam_technique(b) == ELECTRON]
    if not beams:
        raise NotApplicable("no electron beams in this plan")
    return beams


def _note(problems: list[str], limit: int = 3) -> str:
    return "; ".join(problems[:limit]) + (
        f" (+{len(problems) - limit} more)" if len(problems) > limit else ""
    )


@register
class ElectronApplicator(Rule):
    id = "ELEC-001"
    title = "Electron beams specify an applicator of an allowed type"
    category = "electron"
    description = (
        "The applicator (cone) defines the electron field's collimation and the air gap; a plan "
        "without one, or with an undefined type, cannot be matched to the physical set-up. "
        "AAPM MPPG 11.a Table 9 lists applicator, cone and block code among the electron checks. "
        "`allowed_applicator_ids` can whitelist the cones your clinic owns."
    )
    reference = "DICOM RT Beams Module - ApplicatorSequence (300A,0107); AAPM MPPG 11.a Table 9"
    requires = ("rtplan",)
    applies_to = frozenset({ELECTRON})
    default_params = {
        "allowed_applicator_types": _ELECTRON_APPLICATORS,
        "allowed_applicator_ids": [],
    }

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        types = {str(t).upper() for t in params["allowed_applicator_types"]}
        ids = [str(i) for i in params["allowed_applicator_ids"]]
        for beam in _electron_beams(ctx):
            seq = list(beam.get("ApplicatorSequence", []))
            problems: list[str] = []
            if not seq:
                problems.append("no ApplicatorSequence")
            for app in seq:
                app_id = str(app.get("ApplicatorID", "") or "")
                app_type = str(app.get("ApplicatorType", "") or "").upper()
                if not app_id:
                    problems.append("ApplicatorID missing")
                if app_type not in types:
                    problems.append(
                        f"ApplicatorType {app_type or 'missing'} not in the allowed list"
                    )
                if ids and app_id not in ids:
                    problems.append(f"ApplicatorID {app_id!r} not in the allowed list {ids}")
            yield Check(
                f"{beam_label(beam)} applicator",
                not problems,
                ", ".join(f"{a.get('ApplicatorID')}/{a.get('ApplicatorType')}" for a in seq)
                or "(none)",
                "an allowed applicator",
                note=_note(problems),
            )


@register
class ElectronCutout(Rule):
    id = "ELEC-002"
    title = "Electron cutouts (aperture blocks) are defined and well-formed"
    category = "electron"
    description = (
        "Electron fields are shaped by a cutout in the applicator insert. The plan must declare it "
        "(unless the applicator is an open type), the declared block count and point count must "
        "match the data, the outline needs at least three points, and coordinates must be finite "
        "and within a plausible extent. A missing or corrupt block means the dose was calculated "
        "for a field that cannot be reproduced."
    )
    reference = "DICOM RT Beams Module - BlockSequence (300A,00F4), BlockData (300A,0100); AAPM MPPG 11.a Table 9"
    requires = ("rtplan",)
    applies_to = frozenset({ELECTRON})
    default_params = {"require_cutout": True, "max_abs_block_coordinate_mm": 300.0}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        require = bool(params["require_cutout"])
        limit = float(params["max_abs_block_coordinate_mm"])
        for beam in _electron_beams(ctx):
            blocks = list(beam.get("BlockSequence", []))
            open_applicator = any(
                str(a.get("ApplicatorType", "")).upper() == "ELECTRON_OPEN"
                for a in beam.get("ApplicatorSequence", [])
            )
            problems: list[str] = []
            declared = beam.get("NumberOfBlocks")
            if declared is not None and int(declared) != len(blocks):
                problems.append(f"NumberOfBlocks {declared} but {len(blocks)} block(s) present")
            if (
                require
                and not open_applicator
                and not any(str(b.get("BlockType", "")).upper() == "APERTURE" for b in blocks)
            ):
                problems.append("no APERTURE block (cutout) defined")
            for b in blocks:
                label = f"block {b.get('BlockNumber', '?')}"
                data = [float(v) for v in b.get("BlockData", [])]
                if len(data) % 2:
                    problems.append(f"{label}: BlockData has an odd number of values")
                    continue
                pts = np.asarray(data).reshape(-1, 2)
                n = b.get("BlockNumberOfPoints")
                if n is None or int(n) != len(pts):
                    problems.append(f"{label}: declares {n} points, has {len(pts)}")
                if len(pts) < 3:
                    problems.append(f"{label}: only {len(pts)} points")
                if not np.isfinite(pts).all() or (len(pts) and np.abs(pts).max() > limit):
                    problems.append(f"{label}: coordinates not finite or beyond +/-{fmt(limit)} mm")
            yield Check(
                f"{beam_label(beam)} cutout",
                not problems,
                f"{len(blocks)} block(s)" if not problems else f"{len(problems)} problem(s)",
                "an APERTURE cutout, well-formed"
                if require and not open_applicator
                else "well-formed blocks",
                note=_note(problems),
            )


@register
class ElectronSsd(Rule):
    id = "ELEC-003"
    title = "Source-to-surface distance is given and plausible"
    category = "electron"
    description = (
        "Electron output depends strongly on SSD (virtual source, air gap). It must be present on "
        "the first control point and lie in the configured range; extended-SSD set-ups are "
        "legitimate but deserve a human look, hence WARNING severity."
    )
    reference = "DICOM RT Beams Module - SourceToSurfaceDistance (300A,0130)"
    default_severity = Severity.WARNING
    requires = ("rtplan",)
    applies_to = frozenset({ELECTRON})
    default_params = {"min_ssd_mm": 800.0, "max_ssd_mm": 1200.0}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        lo, hi = float(params["min_ssd_mm"]), float(params["max_ssd_mm"])
        for beam in _electron_beams(ctx):
            cp0 = first_control_point(beam)
            raw = cp0.get("SourceToSurfaceDistance") if cp0 is not None else None
            ssd = float(raw) if raw is not None else None
            varies = {
                float(cp.SourceToSurfaceDistance)
                for cp in control_points(beam)
                if "SourceToSurfaceDistance" in cp
            }
            yield Check(
                f"{beam_label(beam)} SSD",
                ssd is not None and lo <= ssd <= hi,
                f"{fmt(ssd)} mm" if ssd is not None else "(missing)",
                f"{fmt(lo)}..{fmt(hi)} mm",
                note="SSD differs between control points" if len(varies) > 1 else "",
            )
