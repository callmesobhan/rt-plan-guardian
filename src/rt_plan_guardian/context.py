"""PlanContext: one reviewable case (a plan + its structure set + its dose) given to every rule."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

from pydicom.dataset import Dataset

from .errors import CannotEvaluate
from .geometry import DoseGrid, RoiGeometry, decode_dose_grid, parse_structure_set
from .plan_model import plan_techniques, vendor_isodose_percent

KINDS = ("rtplan", "rtstruct", "rtdose")


@dataclass(frozen=True)
class LoadedFile:
    name: str  # base file name only (directories can embed patient identifiers)
    kind: str  # one of KINDS
    sha256: str
    dataset: Dataset

    @property
    def sop_instance_uid(self) -> str:
        return str(self.dataset.get("SOPInstanceUID", ""))

    @property
    def sop_class_uid(self) -> str:
        return str(self.dataset.get("SOPClassUID", ""))


@dataclass(frozen=True)
class IgnoredFile:
    name: str
    reason: str


@dataclass(frozen=True)
class Target:
    """A target the plan itself declares (DoseReferenceSequence), resolved to an ROI when possible."""

    roi_number: int | None
    prescription_gy: float | None
    label: str
    resolved_by: str = "roi-reference"  # roi-reference | description | pattern


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


@dataclass(eq=False)
class PlanContext:
    files: tuple[LoadedFile, ...]
    ignored: tuple[IgnoredFile, ...] = ()
    #: RT objects that were supplied but are not part of this case (multi-case assembly only).
    unassigned: tuple[IgnoredFile, ...] = ()

    # ---- access
    def get_all(self, kind: str) -> list[Dataset]:
        return [f.dataset for f in self.files if f.kind == kind]

    def get(self, kind: str) -> Dataset | None:
        """The primary object of this kind. For RTDOSE: the plan-summation dose if present."""
        items = [f for f in self.files if f.kind == kind]
        if not items:
            return None
        if kind == "rtdose":
            plan_level = [f for f in items if str(f.dataset.get("DoseSummationType", "")) == "PLAN"]
            items = sorted(plan_level or items, key=lambda f: f.sop_instance_uid)
        return items[0].dataset

    def has(self, kind: str) -> bool:
        return self.get(kind) is not None

    def require(self, kind: str) -> Dataset:
        """The object of this kind. Rules declare `requires`, so the engine never runs them
        without it; if a rule touches an absent object anyway it is SKIPPED, not crashed."""
        ds = self.get(kind)
        if ds is None:
            raise CannotEvaluate(f"{kind.upper()} not supplied")
        return ds

    @property
    def plan(self) -> Dataset:
        return self.require("rtplan")

    @property
    def structure_set(self) -> Dataset:
        return self.require("rtstruct")

    @property
    def dose(self) -> Dataset:
        return self.require("rtdose")

    @property
    def extra_doses(self) -> list[Dataset]:
        primary = self.get("rtdose")
        return [d for d in self.get_all("rtdose") if d is not primary]

    def labelled(self) -> list[tuple[str, Dataset]]:
        """[(RTPLAN, ds), (RTSTRUCT, ds), (RTDOSE, ds), ("RTDOSE[2]", ds)...] in a fixed order."""
        out: list[tuple[str, Dataset]] = []
        for kind in KINDS:
            group = [f for f in self.files if f.kind == kind]
            for i, f in enumerate(group, start=1):
                label = kind.upper() + (f"[{i}]" if len(group) > 1 else "")
                out.append((label, f.dataset))
        return out

    @cached_property
    def techniques(self) -> frozenset[str]:
        plan = self.get("rtplan")
        return plan_techniques(plan) if plan is not None else frozenset()

    @property
    def case_id(self) -> str:
        plan = self.get("rtplan")
        if plan is None:
            return "case"
        label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(plan.get("RTPlanLabel", "") or "plan")).strip(
            "_"
        )
        return f"{label or 'plan'}_{str(plan.get('SOPInstanceUID', ''))[-6:]}"

    # ---- derived, cached geometry
    @cached_property
    def rois(self) -> dict[int, RoiGeometry]:
        struct = self.get("rtstruct")
        return parse_structure_set(struct) if struct is not None else {}

    @cached_property
    def dose_grid(self) -> DoseGrid:
        return decode_dose_grid(self.dose)

    # ---- prescriptions & targets
    def declared_targets(self) -> list[Target]:
        """Target dose references of the plan, resolved to ROIs.

        VOLUME references point at an ROI number. SITE/POINT references (Gamma Knife, brachytherapy,
        many vendors) only carry a description, so those are matched to an ROI by name (exact,
        case-insensitive, also after a 'prefix:' as in GammaPlan's 'A:TV'); unresolved ones keep
        roi_number=None so rules can say so instead of guessing.
        """
        plan = self.get("rtplan")
        out: list[Target] = []
        if plan is None:
            return out
        names = {num: _norm(g.name) for num, g in self.rois.items()}
        for ref in plan.get("DoseReferenceSequence", []):
            if str(ref.get("DoseReferenceType", "TARGET")) != "TARGET":
                continue
            rx = ref.get("TargetPrescriptionDose")
            label = str(ref.get("DoseReferenceDescription", "") or "")
            roi: int | None = None
            how = "roi-reference"
            if "ReferencedROINumber" in ref:
                roi = int(ref.ReferencedROINumber)
            elif label:
                candidates = {_norm(label), _norm(label.split(":")[-1])}
                hits = [n for n, nm in names.items() if nm in candidates]
                if len(hits) == 1:
                    roi, how = hits[0], "description"
            out.append(Target(roi, float(rx) if rx is not None else None, label, how))
        return out

    def resolve_targets(self, fallback_patterns: list[str]) -> list[Target]:
        """Declared targets win; otherwise fall back to ROI-name regexes (case-insensitive)."""
        declared = [t for t in self.declared_targets() if t.roi_number is not None or t.label]
        if declared:
            return declared
        regexes = [re.compile(p, re.IGNORECASE) for p in fallback_patterns]
        return [
            Target(num, None, geom.name, "pattern")
            for num, geom in self.rois.items()
            if any(r.search(geom.name) for r in regexes)
        ]

    def target_geometries(self, fallback_patterns: list[str]) -> list[tuple[Target, RoiGeometry]]:
        """Targets resolved to contour geometry; raises CannotEvaluate (-> SKIPPED, never a pass)
        when a target cannot be tied to a non-empty ROI."""
        targets = self.resolve_targets(fallback_patterns)
        if not targets:
            raise CannotEvaluate("plan declares no target and no ROI matches target_patterns")
        out: list[tuple[Target, RoiGeometry]] = []
        for t in targets:
            if t.roi_number is None:
                raise CannotEvaluate(
                    f"target '{t.label}' is not linked to an ROI (no ROI reference and no ROI of that name)"
                )
            geom = self.rois.get(t.roi_number)
            if geom is None or not geom.slices:
                raise CannotEvaluate(f"target ROI {t.roi_number} is missing or has no contours")
            out.append((t, geom))
        return out

    def prescription_isodose(
        self, explicit_percent: float = 0.0, use_vendor: bool = True
    ) -> tuple[float, str]:
        """(percent of the reference dose the prescription is specified at, provenance text).

        Linac plans prescribe at 100 %; radiosurgery systems prescribe to an isodose line
        (e.g. 50 % of the maximum dose). Order: explicit ruleset value, then a vendor-specific
        element (Elekta GammaPlan, see plan_model), else 100 %."""
        if explicit_percent and explicit_percent > 0:
            return float(explicit_percent), "set by the ruleset"
        plan = self.get("rtplan")
        if use_vendor and plan is not None:
            found = vendor_isodose_percent(plan)
            if found is not None:
                return (
                    found,
                    "private element (0009,1027) of block ELEKTA GAMMAPLAN (empirical reading)",
                )
        return 100.0, "no isodose information: prescription taken at 100%"

    def max_prescription_gy(self) -> float | None:
        values = [t.prescription_gy for t in self.declared_targets() if t.prescription_gy]
        return max(values) if values else None
