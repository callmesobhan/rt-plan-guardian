"""RT Structure Set rules: ROI bookkeeping and contour well-formedness."""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from ..context import PlanContext
from ..errors import CannotEvaluate, NotApplicable
from ..models import Check, Severity
from .base import Rule, register
from .linkage import structure_set_frames


def _rois(struct: Any) -> list[tuple[int, str, Any]]:
    out = []
    for roi in struct.get("StructureSetROISequence", []):
        if "ROINumber" in roi:
            out.append((int(roi.ROINumber), str(roi.get("ROIName", "") or ""), roi))
    return out


def _contours_by_roi(struct: Any) -> dict[int, list[Any]]:
    out: dict[int, list[Any]] = {}
    for rc in struct.get("ROIContourSequence", []):
        if "ReferencedROINumber" in rc:
            out.setdefault(int(rc.ReferencedROINumber), []).extend(rc.get("ContourSequence", []))
    return out


def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(re.search(p, name, re.IGNORECASE) for p in patterns)


@register
class RoiBookkeeping(Rule):
    id = "STRUCT-001"
    title = "ROI numbers, names and references are well-formed"
    category = "structure"
    description = (
        "Everything downstream addresses ROIs by number. Duplicate numbers, unnamed ROIs or "
        "orphaned contour data make 'which structure is this?' ambiguous."
    )
    reference = "Structure Set Module - StructureSetROISequence; ROI Contour Module"
    requires = ("rtstruct",)

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        struct = ctx.structure_set
        rois = _rois(struct)
        yield Check("Structure set defines at least one ROI", bool(rois), len(rois), ">= 1")
        numbers = [n for n, _, _ in rois]
        dup_numbers = sorted(n for n, c in Counter(numbers).items() if c > 1)
        yield Check(
            "ROI numbers are unique", not dup_numbers, dup_numbers or "all unique", "unique"
        )
        unnamed = [n for n, name, _ in rois if not name.strip()]
        yield Check(
            "Every ROI has a name",
            not unnamed,
            unnamed or "all named",
            "all named",
            note="values listed are ROI numbers" if unnamed else "",
        )
        names = Counter(name.strip().lower() for _, name, _ in rois if name.strip())
        dup_names = sorted(n for n, c in names.items() if c > 1)
        yield Check(
            "ROI names are unique (case-insensitive)",
            not dup_names,
            dup_names or "all unique",
            "unique",
        )
        orphans = sorted(set(_contours_by_roi(struct)) - set(numbers))
        yield Check(
            "Every ROIContour references a defined ROI",
            not orphans,
            orphans or "none",
            "none",
            note="values listed are ReferencedROINumber" if orphans else "",
        )
        frames = structure_set_frames(struct)
        stray = sorted(
            {
                str(roi.get("ReferencedFrameOfReferenceUID", ""))
                for _, _, roi in rois
                if str(roi.get("ReferencedFrameOfReferenceUID", "")) not in frames
            }
        )
        yield Check(
            "Every ROI uses a frame of reference declared by the structure set",
            not stray,
            stray or "all declared",
            "declared",
        )


@register
class RequiredRoisPresent(Rule):
    id = "STRUCT-002"
    title = "Required ROIs are present"
    category = "structure"
    description = (
        "Site policy usually demands certain structures (a target, an external contour, key "
        "OARs). Each entry in `required_roi_patterns` is a case-insensitive regex searched in "
        "the ROI names."
    )
    reference = "Local policy; naming guidance: AAPM TG-263"
    requires = ("rtstruct",)
    default_params = {"required_roi_patterns": []}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        patterns = list(params["required_roi_patterns"])
        if not patterns:
            raise NotApplicable("no required_roi_patterns configured")
        names = [name for _, name, _ in _rois(ctx.structure_set)]
        for pattern in patterns:
            hits = [n for n in names if re.search(pattern, n, re.IGNORECASE)]
            yield Check(
                f"An ROI matches /{pattern}/", bool(hits), hits or "no match", "at least one match"
            )


@register
class NoEmptyRois(Rule):
    id = "STRUCT-003"
    title = "Every ROI contains contour data"
    category = "structure"
    description = (
        "An ROI with a name but no contours is invisible to any dose statistic that uses it - "
        "a quiet way for an OAR constraint to be 'met'. ROIs matching `ignore_patterns` are exempt."
    )
    reference = "ROI Contour Module - ContourSequence (3006,0040)"
    default_severity = Severity.WARNING
    requires = ("rtstruct",)
    default_params = {"ignore_patterns": []}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        contours = _contours_by_roi(ctx.structure_set)
        ignore = list(params["ignore_patterns"])
        rois = _rois(ctx.structure_set)
        if not rois:
            raise NotApplicable("structure set defines no ROIs")
        for number, name, _ in rois:
            if _matches_any(name, ignore):
                continue
            count = len(contours.get(number, []))
            yield Check(
                f"ROI {number} '{name}' has contours", count > 0, f"{count} contours", ">= 1"
            )


@register
class ContoursWellFormed(Rule):
    id = "STRUCT-004"
    title = "Contours are well-formed for their geometric type"
    category = "structure"
    description = (
        "Declared point counts must match the data, closed polygons need at least three points, and "
        "all points of a planar contour must share one z. Open non-planar contours (brachytherapy "
        "catheters and applicators) and single POINTs are valid DICOM and are checked against their "
        "own type. Malformed contours corrupt volume and dose statistics without any visible error "
        "in most viewers."
    )
    reference = "ROI Contour Module - ContourGeometricType, NumberOfContourPoints, ContourData"
    requires = ("rtstruct",)
    default_params = {
        "planar_tolerance_mm": 0.01,
        "min_points": 3,
        "allowed_geometric_types": ["CLOSED_PLANAR", "OPEN_PLANAR", "OPEN_NONPLANAR", "POINT"],
    }

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        tol = float(params["planar_tolerance_mm"])
        min_points = int(params["min_points"])
        allowed = {str(t).upper() for t in params["allowed_geometric_types"]}
        contours = _contours_by_roi(ctx.structure_set)
        evaluated = False
        for number, name, _ in _rois(ctx.structure_set):
            items = contours.get(number, [])
            if not items:
                continue  # STRUCT-003's job
            evaluated = True
            problems: list[str] = []
            for i, c in enumerate(items):
                gtype = str(c.get("ContourGeometricType", ""))
                if gtype not in allowed:
                    problems.append(f"contour {i}: geometric type {gtype or 'missing'} not allowed")
                    continue
                data = c.get("ContourData")
                if data is None or len(data) % 3:
                    problems.append(f"contour {i}: ContourData length not a multiple of 3")
                    continue
                pts = np.asarray(data, dtype=float).reshape(-1, 3)
                declared = c.get("NumberOfContourPoints")
                if declared is None or int(declared) != len(pts):
                    problems.append(f"contour {i}: declares {declared} points, has {len(pts)}")
                needed = {"POINT": 1, "OPEN_PLANAR": 2, "OPEN_NONPLANAR": 2}.get(gtype, min_points)
                if len(pts) < needed:
                    problems.append(
                        f"contour {i}: only {len(pts)} points (need {needed} for {gtype})"
                    )
                if (
                    gtype in ("CLOSED_PLANAR", "OPEN_PLANAR")
                    and len(pts)
                    and float(np.ptp(pts[:, 2])) > tol
                ):
                    problems.append(
                        f"contour {i}: not planar (z spans {float(np.ptp(pts[:, 2])):.3f} mm)"
                    )
                if not np.isfinite(pts).all():
                    problems.append(f"contour {i}: non-finite coordinates")
            yield Check(
                f"ROI {number} '{name}': {len(items)} contours well-formed",
                not problems,
                f"{len(problems)} problem(s)" if problems else "all well-formed",
                "0 problems",
                note="; ".join(problems[:3]) + (" ..." if len(problems) > 3 else ""),
            )
        if not evaluated:
            raise NotApplicable("no ROI has contour data")


@register
class RoiInterpretedTypes(Rule):
    id = "STRUCT-005"
    title = "Every ROI has an interpreted type"
    category = "structure"
    description = (
        "RTROIInterpretedType (PTV, ORGAN, EXTERNAL...) lets downstream systems treat "
        "targets and organs differently. Missing types push that guesswork onto name matching."
    )
    reference = "RT ROI Observations Module - RTROIInterpretedType (3006,00A4)"
    default_severity = Severity.WARNING
    requires = ("rtstruct",)

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        rois = _rois(ctx.structure_set)
        if not rois:
            raise NotApplicable("structure set defines no ROIs")
        types = {
            int(o.ReferencedROINumber): str(o.get("RTROIInterpretedType", "") or "")
            for o in ctx.structure_set.get("RTROIObservationsSequence", [])
            if "ReferencedROINumber" in o
        }
        for number, name, _ in rois:
            t = types.get(number, "")
            yield Check(
                f"ROI {number} '{name}' has an interpreted type",
                bool(t),
                t or "(none)",
                "non-empty",
            )


NOMENCLATURE_ENV = "RTPG_NOMENCLATURE_FILE"


def load_name_list(path: str | Path) -> list[str]:
    """Names from a text/CSV file: one per line (first CSV column), '#' comments allowed."""
    names: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].split(",", 1)[0].strip()
        if entry:
            names.append(entry)
    return names


@register
class StandardNomenclature(Rule):
    id = "STRUCT-006"
    title = "ROI names follow the configured nomenclature (e.g. AAPM TG-263)"
    category = "structure"
    description = (
        "Standard structure names make plans comparable and let downstream tools recognise "
        "organs. Names must be in the supplied nomenclature list (one name per line; for AAPM "
        "TG-263 convert the official worksheet with scripts/tg263_xls_to_txt.py), fit "
        "`max_name_length` (TG-263: 16 characters) and may alternatively match one of "
        "`additional_allowed_patterns` (e.g. dose-suffixed target names). The list file is read "
        "from `nomenclature_file` or the RTPG_NOMENCLATURE_FILE environment variable; with neither "
        "the rule is not applicable. The list is never bundled: AAPM states no reuse licence."
    )
    reference = "AAPM TG-263 (Mayo et al., IJROBP 2018) nomenclature worksheet"
    default_severity = Severity.WARNING
    requires = ("rtstruct",)
    default_params = {
        "nomenclature_file": "",
        "max_name_length": 16,
        "case_sensitive": True,
        "additional_allowed_patterns": [],
        "ignore_patterns": [],
    }

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        path = str(params["nomenclature_file"]) or os.environ.get(NOMENCLATURE_ENV, "")
        if not path:
            raise NotApplicable(
                f"no nomenclature_file configured and {NOMENCLATURE_ENV} is not set"
            )
        if not Path(path).is_file():
            raise CannotEvaluate(f"nomenclature file not found: {path}")
        sensitive = bool(params["case_sensitive"])
        fold = (lambda n: n) if sensitive else (lambda n: n.casefold())
        allowed = {fold(n) for n in load_name_list(path)}
        if not allowed:
            raise CannotEvaluate(f"nomenclature file {path} contains no names")
        extra = [re.compile(p) for p in params["additional_allowed_patterns"]]
        limit = int(params["max_name_length"])
        for number, name, _ in _rois(ctx.structure_set):
            if _matches_any(name, list(params["ignore_patterns"])):
                continue
            problems = []
            if len(name) > limit:
                problems.append(f"{len(name)} characters (limit {limit})")
            if fold(name) not in allowed and not any(p.search(name) for p in extra):
                problems.append("not in the nomenclature list")
            yield Check(
                f"ROI {number} '{name}' follows the nomenclature",
                not problems,
                "conforms" if not problems else "; ".join(problems),
                f"a listed name of at most {limit} characters",
            )
