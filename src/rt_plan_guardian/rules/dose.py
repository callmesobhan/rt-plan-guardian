"""RT Dose rules: units, grid integrity, resolution, hotspot, coverage, spatial overlap with targets."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from ..context import PlanContext
from ..errors import CannotEvaluate
from ..geometry import dose_axes, percentile_dose
from ..models import Check, Severity
from ..plan_model import CHARGED_PARTICLE, EXTERNAL_BEAM
from .base import Rule, fmt, register


@register
class DoseUnitsAndType(Rule):
    id = "DOSE-001"
    title = "Dose units, type and summation are as expected"
    category = "dose"
    description = (
        "Absolute dose statistics are only meaningful for physical dose in Gy summed over the "
        "plan. Relative dose, per-fraction or per-beam grids must not be compared with the "
        "plan prescription. Proton/ion plans may legitimately carry EFFECTIVE (RBE-weighted) dose."
    )
    reference = "RT Dose Module - DoseUnits, DoseType, DoseSummationType"
    requires = ("rtdose",)
    default_params = {
        "allowed_units": ["GY"],
        "allowed_dose_types": ["PHYSICAL"],
        "allowed_dose_types_charged_particle": ["PHYSICAL", "EFFECTIVE"],
        "allowed_summation_types": ["PLAN"],
    }

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        ds = ctx.dose
        type_key = (
            "allowed_dose_types_charged_particle"
            if ctx.techniques & CHARGED_PARTICLE
            else "allowed_dose_types"
        )
        for attr, key in (
            ("DoseUnits", "allowed_units"),
            ("DoseType", type_key),
            ("DoseSummationType", "allowed_summation_types"),
        ):
            value = str(ds.get(attr, "") or "").upper()
            allowed = [str(a).upper() for a in params[key]]
            yield Check(f"{attr} is allowed", value in allowed, value or "(missing)", allowed)


@register
class DoseGridIntegrity(Rule):
    id = "DOSE-002"
    title = "Dose grid is internally consistent"
    category = "dose"
    description = (
        "Pixel data size must match Rows x Columns x NumberOfFrames, the frame offset vector must "
        "match the frame count and be monotonic, scaling must be positive, and the dose must not "
        "be all zero. A corrupt grid can still 'load' and produce plausible-looking nonsense."
    )
    reference = "RT Dose Module / Multi-frame Overlay - Rows, Columns, NumberOfFrames, GridFrameOffsetVector, DoseGridScaling"
    requires = ("rtdose",)

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        ds = ctx.dose
        try:
            rows, cols = int(ds.Rows), int(ds.Columns)
            frames = int(ds.get("NumberOfFrames", 1) or 1)
            bits = int(ds.BitsAllocated)
        except (AttributeError, TypeError, ValueError):
            yield Check(
                "Rows, Columns, NumberOfFrames, BitsAllocated are present integers",
                False,
                "missing or malformed",
                "integers",
            )
            return
        dims_ok = rows > 0 and cols > 0 and frames > 0 and bits in (8, 16, 32)
        yield Check(
            "Dimensions are positive and BitsAllocated is 8/16/32",
            dims_ok,
            f"{rows}x{cols}x{frames}, {bits}-bit",
            "positive; 8/16/32 bit",
        )
        if not dims_ok:
            return
        expected = rows * cols * frames * (bits // 8)
        raw = ds.get("PixelData")
        actual = len(raw) if raw is not None else 0
        yield Check(
            "PixelData length equals Rows x Columns x Frames x bytes",
            expected <= actual <= expected + 1,
            actual,
            expected,
            note="" if raw is not None else "no PixelData",
        )
        offsets = ds.get("GridFrameOffsetVector")
        if frames > 1 or offsets is not None:
            n = len(offsets) if offsets is not None else 0
            yield Check(
                "GridFrameOffsetVector length equals NumberOfFrames", n == frames, n, frames
            )
            if offsets is not None and n > 1:
                diffs = np.diff([float(v) for v in offsets])
                mono = bool(np.all(diffs > 0) or np.all(diffs < 0))
                yield Check(
                    "GridFrameOffsetVector is strictly monotonic",
                    mono,
                    "monotonic" if mono else "not monotonic",
                    "monotonic",
                )
        try:
            scaling = float(ds.DoseGridScaling)
        except (AttributeError, TypeError, ValueError):
            scaling = float("nan")
        yield Check(
            "DoseGridScaling is a positive number",
            bool(np.isfinite(scaling) and scaling > 0),
            "(missing)" if np.isnan(scaling) else scaling,
            "> 0",
        )
        spacing = ds.get("PixelSpacing")
        yield Check(
            "PixelSpacing has two positive values",
            spacing is not None and len(spacing) == 2 and all(float(v) > 0 for v in spacing),
            list(spacing) if spacing is not None else "(missing)",
            "two values > 0",
        )
        ipp = ds.get("ImagePositionPatient")
        yield Check(
            "ImagePositionPatient has three values",
            ipp is not None and len(ipp) == 3,
            list(ipp) if ipp is not None else "(missing)",
            "three values",
        )
        if expected <= actual and bits in (16, 32):
            dtype = np.dtype(
                f"<{'i' if int(ds.get('PixelRepresentation', 0)) == 1 else 'u'}{bits // 8}"
            )
            peak = int(np.frombuffer(raw, dtype=dtype, count=rows * cols * frames).max())
            yield Check(
                "Dose grid contains non-zero dose", peak > 0, f"max stored value {peak}", "> 0"
            )


@register
class DoseGridResolution(Rule):
    id = "DOSE-003"
    title = "Dose grid resolution is fine enough"
    category = "dose"
    description = (
        "A coarse grid under-resolves gradients and small targets, so DVH statistics and hotspot "
        "values become unreliable. `max_spacing_mm` applies to in-plane and slice spacing."
    )
    reference = "RT Dose Module - PixelSpacing, GridFrameOffsetVector"
    default_severity = Severity.WARNING
    requires = ("rtdose",)
    default_params = {"max_spacing_mm": 3.0}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        limit = float(params["max_spacing_mm"])
        ds = ctx.dose
        spacing = ds.get("PixelSpacing")
        if spacing is None or len(spacing) != 2:
            raise CannotEvaluate("PixelSpacing missing")
        yield Check(
            "In-plane spacing",
            max(float(v) for v in spacing) <= limit + 1e-6,
            [float(v) for v in spacing],
            f"<= {fmt(limit)} mm",
        )
        offsets = ds.get("GridFrameOffsetVector")
        if offsets is not None and len(offsets) > 1:
            step = float(np.max(np.abs(np.diff([float(v) for v in offsets]))))
            yield Check(
                "Slice spacing (largest step)",
                step <= limit + 1e-6,
                round(step, 3),
                f"<= {fmt(limit)} mm",
            )


@register
class DoseHotspot(Rule):
    id = "DOSE-004"
    title = "Maximum dose does not exceed the hotspot limit"
    category = "dose"
    description = (
        "Maximum dose anywhere in the grid, as a percentage of the highest prescription (normalised to the "
        "100% level when the plan is prescribed to an isodose line, e.g. Gamma Knife). A value "
        "far above the limit usually means a wrong prescription, wrong dose file or a "
        "normalisation error."
    )
    reference = "Local policy (typical 107-115% of prescription)"
    default_severity = Severity.WARNING
    applies_to = EXTERNAL_BEAM
    requires = ("rtplan", "rtdose")
    default_params = {
        "max_percent_of_rx": 110.0,
        "prescription_isodose_percent": 0.0,
        "use_vendor_isodose": True,
    }

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        rx = ctx.max_prescription_gy()
        if rx is None:
            raise CannotEvaluate("no TargetPrescriptionDose in the plan to normalise against")
        isodose, why = ctx.prescription_isodose(
            float(params["prescription_isodose_percent"]), bool(params["use_vendor_isodose"])
        )
        rx = rx / (isodose / 100.0)  # dose at the 100% (maximum-dose) normalisation
        grid = ctx.dose_grid
        peak = float(grid.dose_gy.max())
        kz, ky, kx = np.unravel_index(int(np.argmax(grid.dose_gy)), grid.dose_gy.shape)
        limit = float(params["max_percent_of_rx"])
        pct = 100.0 * peak / rx
        yield Check(
            "Dmax as % of prescription",
            pct <= limit,
            f"{peak:.2f} Gy ({pct:.1f}%)",
            f"<= {fmt(limit)}% of {fmt(rx)} Gy",
            note=f"at x={grid.xs[kx]:.1f}, y={grid.ys[ky]:.1f}, z={grid.zs[kz]:.1f} mm"
            + (f"; prescribed at {fmt(isodose)}% isodose ({why})" if isodose != 100.0 else ""),
        )


@register
class TargetInsideDoseGrid(Rule):
    id = "DOSE-005"
    title = "Target volumes lie inside the dose grid"
    category = "dose"
    description = (
        "If a target extends beyond the calculated grid, dose statistics for it are computed on "
        "part of the volume only - or none of it if the grid is shifted (wrong origin, wrong "
        "patient position)."
    )
    reference = "RT Dose Module - ImagePositionPatient; Structure Set contours"
    requires = ("rtstruct", "rtdose")
    default_params = {"target_patterns": ["^PTV"]}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        pairs = ctx.target_geometries(list(params["target_patterns"]))
        xs, ys, zs = dose_axes(ctx.dose)
        half = np.array([_step(xs), _step(ys), _step(zs)]) / 2.0
        glo = np.array([xs.min(), ys.min(), zs.min()]) - half
        ghi = np.array([xs.max(), ys.max(), zs.max()]) + half
        for t, geom in pairs:
            pts = np.concatenate([p for polys in geom.slices.values() for p in polys])
            z = geom.z_levels
            tlo = np.array([pts[:, 0].min(), pts[:, 1].min(), z.min()])
            thi = np.array([pts[:, 0].max(), pts[:, 1].max(), z.max()])
            inside = bool(np.all(tlo >= glo) and np.all(thi <= ghi))
            yield Check(
                f"Target ROI {t.roi_number} '{geom.name}' bounding box inside dose grid",
                inside,
                {
                    "target_min": [round(float(v), 1) for v in tlo],
                    "target_max": [round(float(v), 1) for v in thi],
                },
                {
                    "grid_min": [round(float(v), 1) for v in glo],
                    "grid_max": [round(float(v), 1) for v in ghi],
                },
            )


def _step(axis: np.ndarray) -> float:
    return float(np.max(np.abs(np.diff(axis)))) if len(axis) > 1 else 0.0


@register
class TargetCoverage(Rule):
    id = "DOSE-006"
    title = "Target coverage (DVH point) meets the prescription"
    category = "dose"
    description = (
        "Samples the dose grid at voxel centres inside each declared target and checks that Dx% "
        "(default D95) reaches a fraction of that target's own prescription. This ties plan, "
        "structure set and dose together; it is a screen, not a substitute for the planning "
        "system's DVH."
    )
    reference = "Local policy (typical: D95 >= 95% of prescription)"
    default_severity = Severity.WARNING
    applies_to = EXTERNAL_BEAM
    requires = ("rtplan", "rtstruct", "rtdose")
    default_params = {"volume_percent": 95.0, "min_percent_of_rx": 95.0, "target_patterns": []}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        vol = float(params["volume_percent"])
        need = float(params["min_percent_of_rx"])
        pairs = [
            (t, geom, float(t.prescription_gy))
            for t, geom in ctx.target_geometries(list(params["target_patterns"]))
            if t.prescription_gy
        ]
        if not pairs:
            raise CannotEvaluate("plan declares no target volume with a TargetPrescriptionDose")
        grid = ctx.dose_grid
        for t, geom, rx in pairs:
            values = grid.dose_gy[geom.mask_on_grid(grid.xs, grid.ys, grid.zs)]
            subject = f"Target ROI {t.roi_number} '{geom.name}' D{fmt(vol)} >= {fmt(need)}% of {fmt(rx)} Gy"
            if values.size == 0:
                yield Check(
                    subject,
                    False,
                    "no dose voxels inside the target",
                    f">= {fmt(need * rx / 100)} Gy",
                )
                continue
            dx = percentile_dose(values, vol)
            pct = 100.0 * dx / rx
            yield Check(
                subject,
                pct >= need,
                f"{dx:.2f} Gy ({pct:.1f}%)",
                f">= {need * rx / 100:.2f} Gy",
                note=f"{values.size} voxels sampled",
            )
