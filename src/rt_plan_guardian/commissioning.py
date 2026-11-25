"""Vendor-neutral AAPM TG-275 / MPPG 11.a DICOM-RT adversarial mutation & commissioning harness.

Commercial automated plan checkers (ClearCheck, SunCHECK PlanCHECK, Mobius3D, ESAPI scripts,
etc.) must be commissioned before clinical use (AAPM TG-275, MPPG 11.a). Physicists otherwise
mutate exported plans by hand one by one to check whether their software catches silent hazards.

This module turns *any* anonymized clinical DICOM-RT case (Eclipse, RayStation, Monaco, GammaPlan,
Oncentra, ...) or the built-in multi-modality synthetic baselines into a deterministic, UID-clean
commissioning test suite of obvious defects, clinically subtle "silent killers", and negative
controls — and scores *any* external checker (via CSV/JSON results or a CLI runner command) into
a signed JSON/Markdown/HTML commissioning certificate with Wilson 95% confidence intervals and a
false-negative blind-spot register.
"""

from __future__ import annotations

import copy
import csv
import datetime as dt
import hashlib
import json
import math
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape
from pydicom.dataset import Dataset
from pydicom.uid import UID, generate_uid

from . import __version__
from .config import load_ruleset
from .engine import run_review
from .errors import GuardianError
from .loader import load_cases
from .models import Verdict
from .plan_model import brachy_setups, control_points, plan_beams, plan_techniques
from .synthetic import TECHNIQUES, make_case

# --------------------------------------------------------------------------- data structures
RISK_TIERS = ("high", "medium", "control")
SUBTLETIES = ("obvious", "subtle", "control")
PROFILES = ("all", "tg275-core", "subtle", "controls")

_PHI_TAGS_TO_CLEAR = (
    "PatientBirthDate",
    "PatientSex",
    "PatientComments",
    "OtherPatientIDs",
    "OtherPatientNames",
    "PatientAddress",
    "InstitutionName",
    "InstitutionAddress",
    "ReferringPhysicianName",
    "PhysiciansOfRecord",
    "OperatorsName",
    "ReviewerName",
)


@dataclass
class MutableCase:
    """Deep-copied DICOM-RT triad (RTPLAN, optional RTSTRUCT, optional RTDOSE) ready for mutation."""

    plan: Dataset
    struct: Dataset | None
    dose: Dataset | None
    source_label: str = "case"

    def copy(self) -> MutableCase:
        return copy.deepcopy(self)

    def datasets(self) -> list[tuple[str, Dataset]]:
        items: list[tuple[str, Dataset]] = [("RP.dcm", self.plan)]
        if self.struct is not None:
            items.append(("RS.dcm", self.struct))
        if self.dose is not None:
            items.append(("RD.dcm", self.dose))
        return items

    def write(self, directory: str | Path) -> dict[str, str]:
        """Write DICOM files and return {filename: sha256}."""
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        hashes: dict[str, str] = {}
        for name, ds in self.datasets():
            path = out / name
            ds.save_as(path, enforce_file_format=True)
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes


@dataclass(frozen=True)
class MutationSpec:
    """One vendor-neutral commissioning mutation or negative control."""

    id: str
    title: str
    category: str
    subtlety: str  # obvious | subtle | control
    risk_tier: str  # high | medium | control
    expected_detection: bool
    tg275_reference: str
    clinical_impact: str
    expected_rtpg_rules: tuple[str, ...]
    apply: Callable[[MutableCase, str], tuple[bool, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "subtlety": self.subtlety,
            "risk_tier": self.risk_tier,
            "expected_detection": self.expected_detection,
            "tg275_reference": self.tg275_reference,
            "clinical_impact": self.clinical_impact,
            "expected_rtpg_rules": list(self.expected_rtpg_rules),
        }


# --------------------------------------------------------------------------- UID & PHI helpers
def _fresh_uid(salt: str) -> str:
    return str(generate_uid(entropy_srcs=["rt-plan-guardian-commissioning", salt]))


def _set_sop_uid(ds: Dataset, uid: str) -> None:
    ds.SOPInstanceUID = uid
    if getattr(ds, "file_meta", None) is not None:
        ds.file_meta.MediaStorageSOPInstanceUID = UID(uid)


def sanitize_and_remint_uids(
    case: MutableCase,
    mutant_salt: str,
    *,
    anonymize: bool = False,
    patient_label: str | None = None,
) -> None:
    """Mint fresh internally consistent DICOM UIDs so mutants never collide on import.

    Cross-object references (plan -> structure set, dose -> plan, FrameOfReferenceUID) are
    rewired consistently *before* any mutation runs, so linkage-breaking mutations cleanly break
    the freshly minted references while healthy mutants remain 100% self-consistent.
    """
    study_uid = _fresh_uid(f"{mutant_salt}:study")
    frame_uid = _fresh_uid(f"{mutant_salt}:frame")
    plan_uid = _fresh_uid(f"{mutant_salt}:plan")
    struct_uid = _fresh_uid(f"{mutant_salt}:struct") if case.struct is not None else None
    dose_uid = _fresh_uid(f"{mutant_salt}:dose") if case.dose is not None else None

    old_frame_uids: set[str] = set()
    if case.struct is not None:
        for rf in case.struct.get("ReferencedFrameOfReferenceSequence", []):
            if "FrameOfReferenceUID" in rf:
                old_frame_uids.add(str(rf.FrameOfReferenceUID))
        for roi in case.struct.get("StructureSetROISequence", []):
            if "ReferencedFrameOfReferenceUID" in roi:
                old_frame_uids.add(str(roi.ReferencedFrameOfReferenceUID))
    if case.dose is not None and "FrameOfReferenceUID" in case.dose:
        old_frame_uids.add(str(case.dose.FrameOfReferenceUID))

    _set_sop_uid(case.plan, plan_uid)
    case.plan.StudyInstanceUID = study_uid
    case.plan.SeriesInstanceUID = _fresh_uid(f"{mutant_salt}:series-plan")
    if struct_uid is not None:
        for ref in case.plan.get("ReferencedStructureSetSequence", []):
            ref.ReferencedSOPInstanceUID = struct_uid

    if case.struct is not None and struct_uid is not None:
        _set_sop_uid(case.struct, struct_uid)
        case.struct.StudyInstanceUID = study_uid
        case.struct.SeriesInstanceUID = _fresh_uid(f"{mutant_salt}:series-struct")
        for rf in case.struct.get("ReferencedFrameOfReferenceSequence", []):
            if "FrameOfReferenceUID" in rf:
                rf.FrameOfReferenceUID = frame_uid
        for roi in case.struct.get("StructureSetROISequence", []):
            if "ReferencedFrameOfReferenceUID" in roi and (
                not old_frame_uids or str(roi.ReferencedFrameOfReferenceUID) in old_frame_uids
            ):
                roi.ReferencedFrameOfReferenceUID = frame_uid

    if case.dose is not None and dose_uid is not None:
        _set_sop_uid(case.dose, dose_uid)
        case.dose.StudyInstanceUID = study_uid
        case.dose.SeriesInstanceUID = _fresh_uid(f"{mutant_salt}:series-dose")
        if "FrameOfReferenceUID" in case.dose:
            case.dose.FrameOfReferenceUID = frame_uid
        for ref in case.dose.get("ReferencedRTPlanSequence", []):
            ref.ReferencedSOPInstanceUID = plan_uid

    if anonymize:
        pid = patient_label or f"TG275-{hashlib.sha256(mutant_salt.encode()).hexdigest()[:8].upper()}"
        for _, ds in case.datasets():
            ds.PatientID = pid
            ds.PatientName = f"COMMISSIONING^{pid}"
            for tag in _PHI_TAGS_TO_CLEAR:
                if tag in ds:
                    del ds[tag]
            ds.PatientComments = "TG-275 COMMISSIONING MUTANT - NOT FOR CLINICAL USE"


# --------------------------------------------------------------------------- mutation builders
def _first_treatment_beam(plan: Dataset) -> Dataset | None:
    beams = plan_beams(plan)
    for b in beams:
        if str(b.get("TreatmentDeliveryType", "TREATMENT")).upper() == "TREATMENT":
            return b
    return beams[0] if beams else None


def _all_treatment_beams(plan: Dataset) -> list[Dataset]:
    beams = plan_beams(plan)
    tx = [b for b in beams if str(b.get("TreatmentDeliveryType", "TREATMENT")).upper() == "TREATMENT"]
    return tx or beams


def _m_control_clean(case: MutableCase, salt: str) -> tuple[bool, str]:
    return True, "Unmodified UID-reminted clean baseline (negative control)"


def _m_control_benign_metadata(case: MutableCase, salt: str) -> tuple[bool, str]:
    case.plan.RTPlanDescription = f"Commissioning benign re-export ({salt[:12]})"
    return True, "Updated non-clinical RTPlanDescription only (negative control)"


def _m_wrong_patient_id(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.dose is None:
        return False, "case has no RTDOSE object"
    orig = str(case.dose.get("PatientID", ""))
    case.dose.PatientID = f"{orig}_MISMATCH" if orig else "MISMATCH-9999"
    return True, f"Changed RTDOSE PatientID from {orig!r} to {str(case.dose.PatientID)!r}"


def _m_patient_name_mismatch(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.struct is None:
        return False, "case has no RTSTRUCT object"
    case.struct.PatientName = "WRONG^PATIENT"
    return True, "Changed RTSTRUCT PatientName to 'WRONG^PATIENT'"


def _m_duplicate_sop_uid(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.dose is None:
        return False, "case has no RTDOSE object"
    _set_sop_uid(case.dose, str(case.plan.SOPInstanceUID))
    return True, "Set RTDOSE SOPInstanceUID equal to RTPLAN SOPInstanceUID"


def _m_stale_dose_plan_uid(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.dose is None:
        return False, "case has no RTDOSE object"
    refs = list(case.dose.get("ReferencedRTPlanSequence", []))
    if not refs:
        return False, "RTDOSE has no ReferencedRTPlanSequence"
    refs[0].ReferencedSOPInstanceUID = _fresh_uid(f"{salt}:stale-plan")
    return True, "Pointed RTDOSE ReferencedRTPlanSequence to a stale plan UID"


def _m_stale_plan_struct_uid(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.struct is None:
        return False, "case has no RTSTRUCT object"
    refs = list(case.plan.get("ReferencedStructureSetSequence", []))
    if not refs:
        return False, "RTPLAN has no ReferencedStructureSetSequence"
    refs[0].ReferencedSOPInstanceUID = _fresh_uid(f"{salt}:stale-struct")
    return True, "Pointed RTPLAN ReferencedStructureSetSequence to a stale structure-set UID"


def _m_frame_of_reference_mismatch(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.dose is None or case.struct is None:
        return False, "requires both RTSTRUCT and RTDOSE"
    if "FrameOfReferenceUID" not in case.dose:
        return False, "RTDOSE has no FrameOfReferenceUID"
    case.dose.FrameOfReferenceUID = _fresh_uid(f"{salt}:other-for")
    return True, "Changed RTDOSE FrameOfReferenceUID so it disagrees with RTSTRUCT"


def _m_unapproved_plan_export(case: MutableCase, salt: str) -> tuple[bool, str]:
    case.plan.ApprovalStatus = "UNAPPROVED"
    return True, "Set RTPLAN ApprovalStatus to UNAPPROVED"


def _m_prescription_dose_bump_10pct(case: MutableCase, salt: str) -> tuple[bool, str]:
    for ref in case.plan.get("DoseReferenceSequence", []):
        if str(ref.get("DoseReferenceType", "TARGET")) == "TARGET" and "TargetPrescriptionDose" in ref:
            old = float(ref.TargetPrescriptionDose)
            new = round(old * 1.10, 4)
            ref.TargetPrescriptionDose = new
            return True, f"Bumped TargetPrescriptionDose +10% ({old} -> {new} Gy) without updating beams/dose"
    return False, "RTPLAN has no target DoseReference with TargetPrescriptionDose"


def _m_fraction_count_typo_10x(case: MutableCase, salt: str) -> tuple[bool, str]:
    fgs = list(case.plan.get("FractionGroupSequence", []))
    if not fgs or "NumberOfFractionsPlanned" not in fgs[0]:
        return False, "RTPLAN has no FractionGroupSequence with NumberOfFractionsPlanned"
    old = int(fgs[0].NumberOfFractionsPlanned)
    new = max(old * 10, 200)
    fgs[0].NumberOfFractionsPlanned = new
    return True, f"Mutated NumberOfFractionsPlanned from {old} to {new} (10x decimal/typo error)"


def _m_beam_dropped_from_fg(case: MutableCase, salt: str) -> tuple[bool, str]:
    fgs = list(case.plan.get("FractionGroupSequence", []))
    if not fgs:
        return False, "RTPLAN has no FractionGroupSequence"
    refs = list(fgs[0].get("ReferencedBeamSequence", []))
    if len(refs) < 2:
        return False, "requires at least 2 referenced beams in FractionGroupSequence"
    dropped = int(refs[-1].ReferencedBeamNumber)
    fgs[0].ReferencedBeamSequence = refs[:-1]
    return True, f"Dropped beam {dropped} from FractionGroupSequence while leaving it in BeamSequence"


def _m_mixed_treatment_machines(case: MutableCase, salt: str) -> tuple[bool, str]:
    beams = _all_treatment_beams(case.plan)
    if len(beams) < 2:
        return False, "requires at least 2 treatment beams"
    orig = str(beams[-1].get("TreatmentMachineName", "LINAC1"))
    beams[-1].TreatmentMachineName = f"{orig}_ALT"
    return True, f"Reassigned last beam TreatmentMachineName from {orig!r} to {orig + '_ALT'!r}"


def _m_subtle_mlc_leaf_collision_1mm(case: MutableCase, salt: str) -> tuple[bool, str]:
    for beam in _all_treatment_beams(case.plan):
        for cp in control_points(beam):
            for dev in cp.get("BeamLimitingDevicePositionSequence", []):
                dtype = str(dev.get("RTBeamLimitingDeviceType", "")).upper()
                if dtype.startswith("MLC") and "LeafJawPositions" in dev:
                    pos = [float(v) for v in dev.LeafJawPositions]
                    n = len(pos) // 2
                    if n >= 1:
                        mid = n // 2
                        # Force opposing bank A leaf to cross bank B leaf by exactly 1.0 mm.
                        pos[mid] = round(pos[n + mid] + 1.0, 3)
                        dev.LeafJawPositions = pos
                        return (
                            True,
                            f"Overlapped MLC leaf pair {mid + 1} by 1.0 mm (A={pos[mid]} > B={pos[n + mid]})",
                        )
    return False, "plan contains no MLC leaf positions"


def _m_subtle_isocentre_shift_15mm(case: MutableCase, salt: str) -> tuple[bool, str]:
    beams = _all_treatment_beams(case.plan)
    if not beams:
        return False, "plan has no external beams"
    shifted = 0
    for beam in beams:
        cps = control_points(beam)
        if cps and "IsocenterPosition" in cps[0]:
            iso = [float(v) for v in cps[0].IsocenterPosition]
            # 100 mm shift guarantees clean target-boundary exit on arbitrary target sizes
            cps[0].IsocenterPosition = [iso[0], iso[1], round(iso[2] + 100.0, 3)]
            shifted += 1
    if not shifted:
        return False, "no beam control point carries IsocenterPosition"
    return True, f"Shifted IsocenterPosition by +100 mm in Z on {shifted} beam(s)"


def _m_gantry_angle_out_of_range(case: MutableCase, salt: str) -> tuple[bool, str]:
    beam = _first_treatment_beam(case.plan)
    if beam is None:
        return False, "plan has no external beams"
    cps = control_points(beam)
    if not cps or "GantryAngle" not in cps[0]:
        return False, "first control point has no GantryAngle"
    cps[0].GantryAngle = 365.0
    return True, "Set first beam GantryAngle to 365.0 deg (invalid >360 deg)"


def _m_excessive_mu_spike(case: MutableCase, salt: str) -> tuple[bool, str]:
    fgs = list(case.plan.get("FractionGroupSequence", []))
    if not fgs:
        return False, "plan has no FractionGroupSequence"
    refs = list(fgs[0].get("ReferencedBeamSequence", []))
    if not refs or "BeamMeterset" not in refs[0]:
        return False, "no ReferencedBeamSequence item with BeamMeterset"
    old = float(refs[0].BeamMeterset)
    refs[0].BeamMeterset = max(5000.0, old * 20.0)
    return True, f"Inflated beam 1 BeamMeterset from {old} to {float(refs[0].BeamMeterset)}"


def _m_subtle_dose_grid_half_voxel_shift(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.dose is None or "ImagePositionPatient" not in case.dose:
        return False, "requires RTDOSE with ImagePositionPatient"
    spacing = [float(v) for v in case.dose.get("PixelSpacing", [3.0, 3.0])]
    ipp = [float(v) for v in case.dose.ImagePositionPatient]
    dz = 1.5
    gfov = [float(v) for v in case.dose.get("GridFrameOffsetVector", [])]
    if len(gfov) >= 2:
        dz = abs(gfov[1] - gfov[0]) / 2.0
    case.dose.ImagePositionPatient = [
        round(ipp[0] + spacing[0] / 2.0, 4),
        round(ipp[1] + spacing[1] / 2.0, 4),
        round(ipp[2] + dz, 4),
    ]
    return (
        True,
        f"Shifted RTDOSE ImagePositionPatient by half a voxel (+{spacing[0]/2:.2f}, +{spacing[1]/2:.2f}, +{dz:.2f} mm)",
    )


def _m_dose_grid_origin_shift_50mm(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.dose is None or "ImagePositionPatient" not in case.dose:
        return False, "requires RTDOSE with ImagePositionPatient"
    ipp = [float(v) for v in case.dose.ImagePositionPatient]
    case.dose.ImagePositionPatient = [round(ipp[0] + 100.0, 3), ipp[1], ipp[2]]
    return True, "Shifted RTDOSE ImagePositionPatient +100 mm in X (target outside dose grid)"


def _m_dose_scaling_underdose_15pct(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.dose is None or "DoseGridScaling" not in case.dose:
        return False, "requires RTDOSE with DoseGridScaling"
    old = float(case.dose.DoseGridScaling)
    case.dose.DoseGridScaling = round(old * 0.80, 8)
    return True, f"Reduced DoseGridScaling by 20% ({old} -> {float(case.dose.DoseGridScaling)})"


def _m_dose_scaling_hotspot_20pct(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.dose is None or "DoseGridScaling" not in case.dose:
        return False, "requires RTDOSE with DoseGridScaling"
    old = float(case.dose.DoseGridScaling)
    case.dose.DoseGridScaling = round(old * 1.25, 8)
    return True, f"Inflated DoseGridScaling by 25% ({old} -> {float(case.dose.DoseGridScaling)})"


def _m_dose_units_relative(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.dose is None:
        return False, "case has no RTDOSE object"
    case.dose.DoseUnits = "RELATIVE"
    return True, "Changed RTDOSE DoseUnits to RELATIVE instead of GY"


def _m_nonplanar_contour_spike_2mm(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.struct is None:
        return False, "case has no RTSTRUCT object"
    for rc in case.struct.get("ROIContourSequence", []):
        for contour in rc.get("ContourSequence", []):
            if str(contour.get("ContourGeometricType", "")) == "CLOSED_PLANAR" and "ContourData" in contour:
                data = [float(v) for v in contour.ContourData]
                if len(data) >= 9:
                    data[2] = round(data[2] + 2.0, 3)
                    contour.ContourData = data
                    return True, f"Displaced first vertex Z by +2.0 mm on ROI {rc.ReferencedROINumber}"
    return False, "RTSTRUCT has no CLOSED_PLANAR contours"


def _m_empty_oar_contour(case: MutableCase, salt: str) -> tuple[bool, str]:
    if case.struct is None:
        return False, "case has no RTSTRUCT object"
    contours = list(case.struct.get("ROIContourSequence", []))
    if not contours:
        return False, "RTSTRUCT has no ROIContourSequence items"
    target = contours[-1]
    if "ContourSequence" not in target:
        return False, "candidate ROI already has no ContourSequence"
    del target.ContourSequence
    return True, f"Stripped ContourSequence from ROI {target.ReferencedROINumber}"


def _m_electron_missing_applicator(case: MutableCase, salt: str) -> tuple[bool, str]:
    for beam in _all_treatment_beams(case.plan):
        if str(beam.get("RadiationType", "")).upper() == "ELECTRON" and "ApplicatorSequence" in beam:
            del beam.ApplicatorSequence
            return True, f"Deleted ApplicatorSequence from electron beam {beam.BeamNumber}"
    return False, "plan has no ELECTRON beam with ApplicatorSequence"


def _m_electron_ssd_extreme(case: MutableCase, salt: str) -> tuple[bool, str]:
    for beam in _all_treatment_beams(case.plan):
        if str(beam.get("RadiationType", "")).upper() == "ELECTRON":
            cps = control_points(beam)
            if cps:
                cps[0].SourceToSurfaceDistance = 1500.0
                return True, f"Set electron beam {beam.BeamNumber} SSD to 1500.0 mm"
    return False, "plan has no ELECTRON beam"


def _m_ion_spot_weight_sum_drift(case: MutableCase, salt: str) -> tuple[bool, str]:
    for beam in case.plan.get("IonBeamSequence", []):
        cps = list(beam.get("IonControlPointSequence", []))
        if cps and "ScanSpotMetersetWeights" in cps[0]:
            w = [float(v) for v in cps[0].ScanSpotMetersetWeights]
            if w:
                w[0] = round(w[0] + max(1.0, 0.05 * sum(w)), 4)
                cps[0].ScanSpotMetersetWeights = w
                return True, f"Inflated ion beam {beam.BeamNumber} spot[0] weight without updating cumulative meterset"
    return False, "plan has no IonBeamSequence with ScanSpotMetersetWeights"


def _m_ion_range_shifter_undeclared(case: MutableCase, salt: str) -> tuple[bool, str]:
    for beam in case.plan.get("IonBeamSequence", []):
        cps = list(beam.get("IonControlPointSequence", []))
        if cps and cps[0].get("RangeShifterSettingsSequence"):
            cps[0].RangeShifterSettingsSequence[0].ReferencedRangeShifterNumber = 99
            return True, f"Pointed ion beam {beam.BeamNumber} range-shifter setting to undeclared shifter 99"
    return False, "plan has no IonBeamSequence with RangeShifterSettingsSequence"


def _m_ion_snout_position_missing(case: MutableCase, salt: str) -> tuple[bool, str]:
    for beam in case.plan.get("IonBeamSequence", []):
        cps = list(beam.get("IonControlPointSequence", []))
        if cps and "SnoutPosition" in cps[0]:
            del cps[0].SnoutPosition
            return True, f"Removed SnoutPosition from ion beam {beam.BeamNumber} control point 0"
    return False, "plan has no IonBeamSequence with SnoutPosition"


def _m_brachy_trak_drift_10pct(case: MutableCase, salt: str) -> tuple[bool, str]:
    setups = brachy_setups(case.plan)
    if not setups or "TotalReferenceAirKerma" not in setups[0]:
        return False, "plan has no brachy ApplicationSetupSequence with TotalReferenceAirKerma"
    old = float(setups[0].TotalReferenceAirKerma)
    setups[0].TotalReferenceAirKerma = round(old * 1.10, 6)
    return True, f"Inflated TotalReferenceAirKerma +10% ({old} -> {float(setups[0].TotalReferenceAirKerma)})"


def _m_brachy_source_decay_stale(case: MutableCase, salt: str) -> tuple[bool, str]:
    sources = list(case.plan.get("SourceSequence", []))
    if not sources or "SourceStrengthReferenceDate" not in sources[0]:
        return False, "plan has no brachy SourceSequence with SourceStrengthReferenceDate"
    sources[0].SourceStrengthReferenceDate = "20240101"
    return True, "Backdated SourceStrengthReferenceDate to 20240101 (stale decay reference)"


def _m_brachy_dwell_position_mismatch(case: MutableCase, salt: str) -> tuple[bool, str]:
    for setup in brachy_setups(case.plan):
        for ch in setup.get("ChannelSequence", []):
            cps = list(ch.get("BrachyControlPointSequence", []))
            if len(cps) >= 4 and "ControlPointRelativePosition" in cps[3]:
                old = float(cps[3].ControlPointRelativePosition)
                cps[3].ControlPointRelativePosition = round(old + 1.5, 3)
                return True, f"Shifted brachy closing dwell CP3 position by +1.5 mm ({old} -> {old + 1.5})"
    return False, "plan has no brachy stepwise ChannelSequence"


MUTATION_CATALOG: dict[str, MutationSpec] = {
    spec.id: spec
    for spec in (
        # ---- negative controls
        MutationSpec(
            id="control_clean_clone",
            title="Clean UID-reminted baseline clone",
            category="control",
            subtlety="control",
            risk_tier="control",
            expected_detection=False,
            tg275_reference="AAPM TG-275 §4 (Commissioning specificity baseline)",
            clinical_impact="None — clean reference plan must pass without false alarms.",
            expected_rtpg_rules=(),
            apply=_m_control_clean,
        ),
        MutationSpec(
            id="control_benign_metadata_edit",
            title="Benign non-clinical description edit",
            category="control",
            subtlety="control",
            risk_tier="control",
            expected_detection=False,
            tg275_reference="AAPM TG-275 §4 (Specificity against benign TPS re-export)",
            clinical_impact="None — descriptive comment edit must not trigger false positives.",
            expected_rtpg_rules=(),
            apply=_m_control_benign_metadata,
        ),
        # ---- identity & linkage
        MutationSpec(
            id="wrong_patient_id",
            title="RTDOSE PatientID mismatch",
            category="identity",
            subtlety="obvious",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III / MPPG 11.a Table 7 (Patient identity)",
            clinical_impact="Wrong-patient dose evaluation or chart mix-up.",
            expected_rtpg_rules=("IDENT-001",),
            apply=_m_wrong_patient_id,
        ),
        MutationSpec(
            id="patient_name_mismatch",
            title="RTSTRUCT PatientName mismatch",
            category="identity",
            subtlety="obvious",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III / MPPG 11.a Table 7 (Patient identity)",
            clinical_impact="Structure set exported from another patient.",
            expected_rtpg_rules=("IDENT-002",),
            apply=_m_patient_name_mismatch,
        ),
        MutationSpec(
            id="duplicate_sop_uid",
            title="Duplicate SOPInstanceUID across RTPLAN and RTDOSE",
            category="identity",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="DICOM PS3.3 / AAPM TG-275 (Object integrity)",
            clinical_impact="Corrupted PACS/R&V import or silent object overwrite.",
            expected_rtpg_rules=("IDENT-003",),
            apply=_m_duplicate_sop_uid,
        ),
        MutationSpec(
            id="stale_dose_plan_uid",
            title="RTDOSE linked to stale RTPLAN revision UID",
            category="linkage",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III / MPPG 11.a Table 7 (Plan-dose linkage)",
            clinical_impact="Reviewing yesterday's dose grid against today's edited beams.",
            expected_rtpg_rules=("LINK-002",),
            apply=_m_stale_dose_plan_uid,
        ),
        MutationSpec(
            id="stale_plan_struct_uid",
            title="RTPLAN linked to stale RTSTRUCT revision UID",
            category="linkage",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III / MPPG 11.a Table 7 (Plan-contour linkage)",
            clinical_impact="Plan approved against outdated target or OAR contours.",
            expected_rtpg_rules=("LINK-001",),
            apply=_m_stale_plan_struct_uid,
        ),
        MutationSpec(
            id="frame_of_reference_mismatch",
            title="RTDOSE FrameOfReferenceUID disagrees with RTSTRUCT",
            category="linkage",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 / MPPG 11.a Table 7 (Coordinate registration)",
            clinical_impact="Dose grid evaluated in a mismatched patient coordinate frame.",
            expected_rtpg_rules=("LINK-003",),
            apply=_m_frame_of_reference_mismatch,
        ),
        # ---- plan & prescription
        MutationSpec(
            id="unapproved_plan_export",
            title="Plan exported in UNAPPROVED state",
            category="plan",
            subtlety="obvious",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III / MPPG 11.a Table 7 (Plan approval)",
            clinical_impact="Draft or unreviewed plan transferred toward treatment delivery.",
            expected_rtpg_rules=("PLAN-001",),
            apply=_m_unapproved_plan_export,
        ),
        MutationSpec(
            id="prescription_dose_bump_10pct",
            title="Prescription bumped +10% without updating beam doses",
            category="prescription",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III (Rx vs beam dose consistency)",
            clinical_impact="Systematic 10% under-delivery relative to physician prescription.",
            expected_rtpg_rules=("PLAN-004",),
            apply=_m_prescription_dose_bump_10pct,
        ),
        MutationSpec(
            id="fraction_count_typo_10x",
            title="Planned fraction count typo (10x)",
            category="prescription",
            subtlety="obvious",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III / MPPG 11.a Table 7 (Fractionation)",
            clinical_impact="Catastrophic total-dose mismatch in R&V scheduling.",
            expected_rtpg_rules=("PLAN-003", "PLAN-004"),
            apply=_m_fraction_count_typo_10x,
        ),
        MutationSpec(
            id="beam_dropped_from_fraction_group",
            title="Deliverable beam omitted from FractionGroupSequence",
            category="prescription",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III (Beam-to-fraction-group completeness)",
            clinical_impact="Beam silently omitted during R&V delivery.",
            expected_rtpg_rules=("BEAM-001", "PLAN-004"),
            apply=_m_beam_dropped_from_fg,
        ),
        MutationSpec(
            id="mixed_treatment_machines",
            title="Beams assigned to mismatched treatment machines",
            category="plan",
            subtlety="subtle",
            risk_tier="medium",
            expected_detection=True,
            tg275_reference="AAPM TG-275 / MPPG 11.a Table 7 (Machine assignment)",
            clinical_impact="Beam exported for wrong linac/beam model.",
            expected_rtpg_rules=("PLAN-005",),
            apply=_m_mixed_treatment_machines,
        ),
        # ---- collimation, geometry & dose
        MutationSpec(
            id="subtle_mlc_leaf_collision_1mm",
            title="Subtle 1.0 mm MLC opposing leaf overlap",
            category="beam",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 / MPPG 11.a Table 8 (MLC mechanical integrity)",
            clinical_impact="Physical MLC leaf collision / interlock failure at machine.",
            expected_rtpg_rules=("BEAM-007",),
            apply=_m_subtle_mlc_leaf_collision_1mm,
        ),
        MutationSpec(
            id="subtle_isocentre_shift_15mm",
            title="Isocentre displaced outside target ROI",
            category="beam",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III / MPPG 11.a Table 8 (Isocentre placement)",
            clinical_impact="Geometric miss or unintended off-axis delivery.",
            expected_rtpg_rules=("BEAM-006",),
            apply=_m_subtle_isocentre_shift_15mm,
        ),
        MutationSpec(
            id="gantry_angle_out_of_range",
            title="Invalid gantry angle (365 deg)",
            category="beam",
            subtlety="obvious",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="DICOM PS3.3 / AAPM TG-275 (Beam geometry)",
            clinical_impact="Illegal gantry angle fails R&V import or delivery.",
            expected_rtpg_rules=("BEAM-003",),
            apply=_m_gantry_angle_out_of_range,
        ),
        MutationSpec(
            id="excessive_mu_spike",
            title="Extreme beam meterset spike (>=5000 MU)",
            category="beam",
            subtlety="obvious",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III (Monitor unit sanity)",
            clinical_impact="Gross overdose risk if delivered unverified.",
            expected_rtpg_rules=("BEAM-005",),
            apply=_m_excessive_mu_spike,
        ),
        MutationSpec(
            id="subtle_dose_grid_half_voxel_shift",
            title="Subtle half-voxel RTDOSE origin convention offset",
            category="dose",
            subtlety="subtle",
            risk_tier="medium",
            expected_detection=True,
            tg275_reference="AAPM TG-275 / DICOM C.7.6.2 (Voxel-centre vs corner grid alignment)",
            clinical_impact="Silent sub-voxel dose-contour misregistration (~1-2 mm shift in steep SRS/SBRT gradients).",
            expected_rtpg_rules=(),
            apply=_m_subtle_dose_grid_half_voxel_shift,
        ),
        MutationSpec(
            id="dose_grid_origin_shift_50mm",
            title="RTDOSE grid displaced off target (+100 mm X)",
            category="dose",
            subtlety="obvious",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 / MPPG 11.a Table 7 (Dose grid extent)",
            clinical_impact="Target lies partially or completely outside calculated dose box.",
            expected_rtpg_rules=("DOSE-005", "DOSE-006"),
            apply=_m_dose_grid_origin_shift_50mm,
        ),
        MutationSpec(
            id="dose_scaling_underdose_15pct",
            title="Dose grid scaled down 20% (target under-coverage)",
            category="dose",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III (Target coverage / scaling integrity)",
            clinical_impact="Clinical target D95 falls far below prescribed coverage.",
            expected_rtpg_rules=("DOSE-006",),
            apply=_m_dose_scaling_underdose_15pct,
        ),
        MutationSpec(
            id="dose_scaling_hotspot_20pct",
            title="Dose grid scaled up 25% (excessive hotspot)",
            category="dose",
            subtlety="subtle",
            risk_tier="medium",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III (Plan maximum dose / hotspot)",
            clinical_impact="Unintended >120% dose hotspot inside patient.",
            expected_rtpg_rules=("DOSE-004",),
            apply=_m_dose_scaling_hotspot_20pct,
        ),
        MutationSpec(
            id="dose_units_relative",
            title="RTDOSE exported in RELATIVE units instead of GY",
            category="dose",
            subtlety="obvious",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="DICOM PS3.3 / AAPM TG-275 (Absolute dose verification)",
            clinical_impact="Absolute DVH constraints cannot be safely verified.",
            expected_rtpg_rules=("DOSE-001",),
            apply=_m_dose_units_relative,
        ),
        MutationSpec(
            id="nonplanar_contour_spike_2mm",
            title="Non-planar contour vertex spike (+2 mm Z)",
            category="structure",
            subtlety="subtle",
            risk_tier="medium",
            expected_detection=True,
            tg275_reference="AAPM TG-275 / MPPG 11.a Table 7 (Contour geometric integrity)",
            clinical_impact="Corrupted ROI polygon can break DVH/volume calculation.",
            expected_rtpg_rules=("STRUCT-004",),
            apply=_m_nonplanar_contour_spike_2mm,
        ),
        MutationSpec(
            id="empty_oar_contour",
            title="Named ROI stripped of all contours",
            category="structure",
            subtlety="obvious",
            risk_tier="medium",
            expected_detection=True,
            tg275_reference="AAPM TG-275 Table III (Empty OAR structure check)",
            clinical_impact="OAR appears in structure list but silently receives 0 Gy in DVH checks.",
            expected_rtpg_rules=("STRUCT-003",),
            apply=_m_empty_oar_contour,
        ),
        # ---- modality-specific (electron, ion, brachy)
        MutationSpec(
            id="electron_missing_applicator",
            title="Electron beam missing applicator/cone",
            category="electron",
            subtlety="obvious",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM MPPG 11.a Table 9 (Electron applicator)",
            clinical_impact="Incorrect electron output factor and field collimation.",
            expected_rtpg_rules=("ELEC-001",),
            apply=_m_electron_missing_applicator,
        ),
        MutationSpec(
            id="electron_ssd_extreme",
            title="Electron SSD set to extreme value (1500 mm)",
            category="electron",
            subtlety="obvious",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM MPPG 11.a Table 9 (Electron SSD sanity)",
            clinical_impact="Severe inverse-square output error on electron delivery.",
            expected_rtpg_rules=("ELEC-003",),
            apply=_m_electron_ssd_extreme,
        ),
        MutationSpec(
            id="ion_spot_weight_sum_drift_5pct",
            title="Proton/ion spot weights disagree with cumulative meterset",
            category="ion",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="DICOM PS3.3 C.8.8.25 / AAPM TG-275 (PBS spot-map integrity)",
            clinical_impact="Delivered proton layer MU disagrees with planned spot weights.",
            expected_rtpg_rules=("ION-003",),
            apply=_m_ion_spot_weight_sum_drift,
        ),
        MutationSpec(
            id="ion_range_shifter_undeclared",
            title="Proton control point references undeclared range shifter",
            category="ion",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-275 (Proton range-shifter consistency)",
            clinical_impact="Missing or wrong range shifter shifts Bragg peak depth by cm.",
            expected_rtpg_rules=("ION-006",),
            apply=_m_ion_range_shifter_undeclared,
        ),
        MutationSpec(
            id="ion_snout_position_missing",
            title="Proton snout position missing at control point 0",
            category="ion",
            subtlety="subtle",
            risk_tier="medium",
            expected_detection=True,
            tg275_reference="AAPM TG-275 (Proton nozzle clearance & air gap)",
            clinical_impact="Unverified nozzle-to-patient air gap / collision risk.",
            expected_rtpg_rules=("ION-006",),
            apply=_m_ion_snout_position_missing,
        ),
        MutationSpec(
            id="brachy_trak_drift_10pct",
            title="Brachytherapy TRAK inflated +10% vs source strength x time",
            category="brachy",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-59 / TG-275 (Brachytherapy TRAK identity)",
            clinical_impact="10% systematic brachytherapy dose/time discrepancy.",
            expected_rtpg_rules=("BRACHY-002",),
            apply=_m_brachy_trak_drift_10pct,
        ),
        MutationSpec(
            id="brachy_source_decay_stale_1yr",
            title="Brachytherapy source reference date stale (>1 year)",
            category="brachy",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="AAPM TG-59 / TG-275 (Source decay calibration date)",
            clinical_impact="Wrong source strength / dwell time calculation.",
            expected_rtpg_rules=("BRACHY-004",),
            apply=_m_brachy_source_decay_stale,
        ),
        MutationSpec(
            id="brachy_dwell_position_mismatch",
            title="Brachytherapy dwell opening/closing control points disagree (+1.5 mm)",
            category="brachy",
            subtlety="subtle",
            risk_tier="high",
            expected_detection=True,
            tg275_reference="DICOM PS3.3 C.8.8.15 / AAPM TG-59 (Stepwise dwell integrity)",
            clinical_impact="Afterloader dwell position error near critical OARs.",
            expected_rtpg_rules=("BRACHY-003",),
            apply=_m_brachy_dwell_position_mismatch,
        ),
    )
}


# --------------------------------------------------------------------------- suite generation
def _select_mutations(
    profile: str = "all",
    mutation_ids: Sequence[str] | None = None,
) -> list[MutationSpec]:
    if mutation_ids:
        unknown = [m for m in mutation_ids if m not in MUTATION_CATALOG]
        if unknown:
            raise GuardianError(
                f"Unknown mutation id(s): {', '.join(unknown)}. Run `rtpg commission list`."
            )
        return [MUTATION_CATALOG[m] for m in mutation_ids]
    if profile not in PROFILES:
        raise GuardianError(f"Unknown profile {profile!r}; choose from {', '.join(PROFILES)}")
    specs = list(MUTATION_CATALOG.values())
    if profile == "tg275-core":
        return [s for s in specs if s.risk_tier in ("high", "control")]
    if profile == "subtle":
        return [s for s in specs if s.subtlety in ("subtle", "control")]
    if profile == "controls":
        return [s for s in specs if s.subtlety == "control"]
    return specs


def _load_baseline_cases(
    source_paths: Sequence[str | Path] | None,
    technique: str,
    seed: int,
) -> list[MutableCase]:
    if source_paths:
        case_set = load_cases(source_paths)
        if not case_set.cases:
            raise GuardianError("No reviewable DICOM-RT cases found in the supplied paths.")
        out: list[MutableCase] = []
        for ctx in case_set.cases:
            plan = ctx.get("rtplan")
            if plan is None:
                continue
            out.append(
                MutableCase(
                    plan=copy.deepcopy(plan),
                    struct=copy.deepcopy(ctx.get("rtstruct")),
                    dose=copy.deepcopy(ctx.get("rtdose")),
                    source_label=ctx.case_id,
                )
            )
        if not out:
            raise GuardianError("Supplied paths contained no RTPLAN object to mutate.")
        return out

    techs = list(TECHNIQUES) if technique == "all" else [technique]
    out = []
    for tech in techs:
        sc = make_case(seed=seed, technique=tech)
        assert sc.plan is not None
        out.append(
            MutableCase(
                plan=sc.plan,
                struct=sc.struct,
                dose=sc.dose,
                source_label=f"synthetic_{tech}",
            )
        )
    return out


def generate_commissioning_suite(
    out_dir: str | Path,
    *,
    source_paths: Sequence[str | Path] | None = None,
    technique: str = "photon",
    profile: str = "all",
    mutation_ids: Sequence[str] | None = None,
    anonymize: bool = True,
    blind: bool = False,
    seed: int = 0,
) -> dict[str, Any]:
    """Generate a vendor-neutral TG-275 DICOM-RT commissioning suite on disk.

    Writes:
      - `<out_dir>/cases/<case_id>/*.dcm`
      - `<out_dir>/manifest.json`
      - `<out_dir>/results_template.csv`
    """
    out = Path(out_dir)
    cases_dir = out / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    baselines = _load_baseline_cases(source_paths, technique=technique, seed=seed)
    specs = _select_mutations(profile=profile, mutation_ids=mutation_ids)

    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seq_idx = 0

    for base in baselines:
        base_techniques = sorted(plan_techniques(base.plan))
        for spec in specs:
            mutant = base.copy()
            salt = f"{seed}:{base.source_label}:{spec.id}"
            seq_idx += 1
            case_id = (
                f"CASE_{seq_idx:03d}"
                if blind
                else f"{base.source_label}__{spec.id}"
            )
            sanitize_and_remint_uids(
                mutant,
                salt,
                anonymize=anonymize,
                patient_label=f"TG275-{seq_idx:03d}",
            )
            applied, detail = spec.apply(mutant, salt)
            if not applied:
                skipped.append(
                    {
                        "source_label": base.source_label,
                        "mutation_id": spec.id,
                        "reason": detail,
                    }
                )
                continue
            file_hashes = mutant.write(cases_dir / case_id)
            generated.append(
                {
                    "case_id": case_id,
                    "source_label": base.source_label,
                    "techniques": base_techniques,
                    "mutation_id": spec.id,
                    "title": spec.title,
                    "category": spec.category,
                    "subtlety": spec.subtlety,
                    "risk_tier": spec.risk_tier,
                    "expected_detection": spec.expected_detection,
                    "tg275_reference": spec.tg275_reference,
                    "clinical_impact": spec.clinical_impact,
                    "expected_rtpg_rules": list(spec.expected_rtpg_rules),
                    "mutation_detail": detail,
                    "relative_dir": f"cases/{case_id}",
                    "files": file_hashes,
                }
            )

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "tool": {"name": "rt-plan-guardian", "version": __version__},
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source_mode": "clinical_dicom" if source_paths else f"synthetic:{technique}",
        "profile": profile,
        "anonymized": anonymize,
        "blinded": blind,
        "seed": seed,
        "summary": {
            "total_cases": len(generated),
            "positive_defects": sum(1 for g in generated if g["expected_detection"]),
            "negative_controls": sum(1 for g in generated if not g["expected_detection"]),
            "high_risk_defects": sum(1 for g in generated if g["risk_tier"] == "high"),
            "subtle_defects": sum(1 for g in generated if g["subtlety"] == "subtle"),
            "skipped_inapplicable": len(skipped),
        },
        "cases": generated,
        "skipped": skipped,
    }
    manifest["suite_sha256"] = _stable_hash(
        {k: v for k, v in manifest.items() if k != "generated_at"}
    )

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Write a clean CSV template for external checkers (ClearCheck, SunCHECK, Mobius3D, ESAPI...)
    csv_path = out / "results_template.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["case_id", "detected", "severity", "checker_message"])
        for entry in generated:
            writer.writerow([entry["case_id"], "", "", ""])

    return manifest


# --------------------------------------------------------------------------- scoring & statistics
def _stable_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def wilson_ci(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Exact Wilson score 95% confidence interval for a binomial proportion."""
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p + z2 / (2.0 * total)) / denom
    margin = (z / denom) * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return (round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4))


def _rate_block(successes: int, total: int) -> dict[str, Any]:
    rate = round(successes / total, 4) if total > 0 else None
    lo, hi = wilson_ci(successes, total)
    return {
        "count": successes,
        "total": total,
        "rate": rate,
        "ci95": [lo, hi] if total > 0 else None,
    }


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "y", "fail", "warn", "warning", "detected", "flagged"):
        return True
    if s in ("0", "false", "no", "n", "pass", "ok", "clean", "missed", "not_detected"):
        return False
    raise GuardianError(f"Cannot interpret detected flag {raw!r} as boolean")


def load_external_results(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load external checker outcomes from CSV or JSON."""
    p = Path(path)
    if not p.exists():
        raise GuardianError(f"Results file does not exist: {p}")
    out: dict[str, dict[str, Any]] = {}
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data["results"] if isinstance(data, dict) and "results" in data else data
        for row in rows:
            cid = str(row["case_id"])
            out[cid] = {
                "detected": _parse_bool(row["detected"]),
                "severity": str(row.get("severity", "") or ""),
                "checker_message": str(row.get("checker_message", row.get("message", "")) or ""),
            }
        return out

    with p.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            cid = str(row.get("case_id", "")).strip()
            if not cid:
                continue
            raw_det = str(row.get("detected", "")).strip()
            if raw_det == "":
                continue
            out[cid] = {
                "detected": _parse_bool(raw_det),
                "severity": str(row.get("severity", "") or "").strip(),
                "checker_message": str(row.get("checker_message", "") or "").strip(),
            }
    return out


def run_rtpg_on_suite(
    suite_dir: str | Path,
    *,
    config: str | Path | None = None,
    count_warnings_as_detected: bool = True,
) -> dict[str, dict[str, Any]]:
    """Run RT-Plan Guardian itself against every case in a generated commissioning suite."""
    suite = Path(suite_dir)
    manifest = json.loads((suite / "manifest.json").read_text(encoding="utf-8"))
    ruleset = load_ruleset(config)
    outcomes: dict[str, dict[str, Any]] = {}
    for entry in manifest["cases"]:
        case_path = suite / entry["relative_dir"]
        case_set = load_cases([case_path])
        report = run_review(case_set.cases[0], ruleset)
        verdict = report.verdict
        detected = verdict in (Verdict.FAIL, Verdict.INCOMPLETE) or (
            count_warnings_as_detected and verdict == Verdict.PASS_WITH_WARNINGS
        )
        failed = [r.rule_id for r in report.results if r.status.value in ("FAIL", "ERROR")]
        outcomes[entry["case_id"]] = {
            "detected": detected,
            "severity": verdict.value,
            "checker_message": ", ".join(failed) if failed else "PASS",
        }
    return outcomes


def run_command_adapter_on_suite(
    suite_dir: str | Path,
    command_template: str,
) -> dict[str, dict[str, Any]]:
    """Run any external CLI checker command on every mutant directory.

    Convention: `{case_dir}` is substituted with the absolute mutant directory path.
    Exit code 0 = PASS (not detected); non-zero exit code = FAIL/WARN (detected).
    """
    suite = Path(suite_dir)
    manifest = json.loads((suite / "manifest.json").read_text(encoding="utf-8"))
    outcomes: dict[str, dict[str, Any]] = {}
    for entry in manifest["cases"]:
        case_path = str((suite / entry["relative_dir"]).resolve())
        tokens = [
            tok.replace("{case_dir}", case_path)
            for tok in shlex.split(command_template)
        ]
        if not any(case_path in t for t in tokens):
            tokens.append(case_path)
        proc = subprocess.run(tokens, capture_output=True, text=True, check=False)
        detected = proc.returncode != 0
        msg = (proc.stdout.strip() or proc.stderr.strip()).splitlines()
        outcomes[entry["case_id"]] = {
            "detected": detected,
            "severity": f"exit={proc.returncode}",
            "checker_message": msg[-1][:160] if msg else "",
        }
    return outcomes


def score_commissioning_suite(
    suite_dir: str | Path,
    observations: dict[str, dict[str, Any]],
    *,
    checker_name: str,
    checker_version: str = "",
    min_sensitivity: float = 0.85,
) -> dict[str, Any]:
    """Score any automated plan checker against a commissioning suite manifest."""
    suite = Path(suite_dir)
    manifest_path = suite / "manifest.json"
    if not manifest_path.exists():
        raise GuardianError(f"No manifest.json found in {suite}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    evaluated: list[dict[str, Any]] = []
    missing_ids: list[str] = []
    blind_spots: list[dict[str, Any]] = []
    false_alarms: list[dict[str, Any]] = []

    tp = fp = tn = fn = 0
    high_tp = high_total = 0
    subtle_tp = subtle_total = 0
    obvious_tp = obvious_total = 0
    by_category: dict[str, dict[str, int]] = {}

    for entry in manifest["cases"]:
        cid = entry["case_id"]
        if cid not in observations:
            missing_ids.append(cid)
            continue
        obs = observations[cid]
        detected = bool(obs["detected"])
        expected = bool(entry["expected_detection"])

        if expected and detected:
            outcome = "TP"
            tp += 1
        elif expected and not detected:
            outcome = "FN"
            fn += 1
            blind_spots.append(
                {
                    "case_id": cid,
                    "mutation_id": entry["mutation_id"],
                    "title": entry["title"],
                    "category": entry["category"],
                    "risk_tier": entry["risk_tier"],
                    "subtlety": entry["subtlety"],
                    "tg275_reference": entry["tg275_reference"],
                    "clinical_impact": entry["clinical_impact"],
                    "checker_message": obs.get("checker_message", ""),
                }
            )
        elif not expected and detected:
            outcome = "FP"
            fp += 1
            false_alarms.append(
                {
                    "case_id": cid,
                    "mutation_id": entry["mutation_id"],
                    "title": entry["title"],
                    "checker_message": obs.get("checker_message", ""),
                }
            )
        else:
            outcome = "TN"
            tn += 1

        if entry["risk_tier"] == "high":
            high_total += 1
            if detected:
                high_tp += 1
        if entry["subtlety"] == "subtle":
            subtle_total += 1
            if detected:
                subtle_tp += 1
        elif entry["subtlety"] == "obvious":
            obvious_total += 1
            if detected:
                obvious_tp += 1

        cat = entry["category"]
        bucket = by_category.setdefault(cat, {"tp": 0, "fn": 0, "tn": 0, "fp": 0, "total": 0})
        bucket[outcome.lower()] += 1
        bucket["total"] += 1

        evaluated.append(
            {
                **entry,
                "observed_detected": detected,
                "observed_severity": obs.get("severity", ""),
                "checker_message": obs.get("checker_message", ""),
                "outcome": outcome,
            }
        )

    pos_total = tp + fn
    neg_total = tn + fp
    sens = tp / pos_total if pos_total else 1.0
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    f1 = (2 * prec * sens / (prec + sens)) if (prec + sens) > 0 else 0.0

    if missing_ids:
        verdict = "INCOMPLETE"
        verdict_reason = f"{len(missing_ids)} suite case(s) have no recorded checker outcome."
    elif high_tp < high_total or fp > 0:
        verdict = "FAIL"
        reasons = []
        if high_tp < high_total:
            reasons.append(f"missed {high_total - high_tp} high-risk TG-275 defect(s)")
        if fp > 0:
            reasons.append(f"raised {fp} false alarm(s) on clean negative control(s)")
        verdict_reason = "; ".join(reasons).capitalize() + "."
    elif sens < min_sensitivity or fn > 0:
        verdict = "PASS_WITH_WARNINGS"
        verdict_reason = (
            f"All high-risk defects and negative controls passed, but {fn} subtle/medium defect(s) were missed."
        )
    else:
        verdict = "PASS"
        verdict_reason = "All evaluated defects detected with zero false alarms on negative controls."

    certificate: dict[str, Any] = {
        "schema_version": "1.0",
        "tool": {"name": "rt-plan-guardian", "version": __version__},
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "checker": {"name": checker_name, "version": checker_version},
        "suite": {
            "source_mode": manifest.get("source_mode", ""),
            "profile": manifest.get("profile", ""),
            "suite_sha256": manifest.get("suite_sha256", ""),
            "total_cases": len(manifest["cases"]),
            "evaluated_cases": len(evaluated),
            "missing_cases": missing_ids,
        },
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "confusion_matrix": {"tp": tp, "fn": fn, "tn": tn, "fp": fp},
        "metrics": {
            "sensitivity": _rate_block(tp, pos_total),
            "specificity": _rate_block(tn, neg_total),
            "precision": _rate_block(tp, tp + fp),
            "f1": round(f1, 4),
            "high_risk_recall": _rate_block(high_tp, high_total),
            "obvious_recall": _rate_block(obvious_tp, obvious_total),
            "subtle_recall": _rate_block(subtle_tp, subtle_total),
        },
        "by_category": by_category,
        "blind_spots": blind_spots,
        "false_alarms": false_alarms,
        "cases": evaluated,
    }
    certificate["content_sha256"] = _stable_hash(
        {k: v for k, v in certificate.items() if k != "generated_at"}
    )
    return certificate


# --------------------------------------------------------------------------- reporting
def _fmt_pct(block: dict[str, Any]) -> str:
    if block["rate"] is None:
        return "n/a"
    lo, hi = block["ci95"]
    return f"{block['rate'] * 100:.1f}% ({block['count']}/{block['total']}, 95% CI {lo * 100:.1f}-{hi * 100:.1f}%)"


def render_commissioning_markdown(cert: dict[str, Any]) -> str:
    m = cert["metrics"]
    checker = cert["checker"]["name"] + (
        f" {cert['checker']['version']}" if cert["checker"]["version"] else ""
    )
    lines = [
        f"# AAPM TG-275 Plan-Checker Commissioning Certificate — {cert['verdict']}",
        "",
        f"- **System under test:** {checker}",
        f"- **Suite source / profile:** `{cert['suite']['source_mode']}` / `{cert['suite']['profile']}`",
        f"- **Verdict:** **{cert['verdict']}** — {cert['verdict_reason']}",
        f"- **Suite SHA-256:** `{cert['suite']['suite_sha256']}`",
        f"- **Certificate SHA-256:** `{cert['content_sha256']}`",
        "",
        "## Diagnostic Performance (Wilson 95% CI)",
        "",
        "| Metric | Score |",
        "|---|---|",
        f"| Sensitivity (Overall Defect Recall) | {_fmt_pct(m['sensitivity'])} |",
        f"| Specificity (Clean Control Pass Rate) | {_fmt_pct(m['specificity'])} |",
        f"| High-Risk TG-275 Recall | {_fmt_pct(m['high_risk_recall'])} |",
        f"| Obvious-Defect Recall | {_fmt_pct(m['obvious_recall'])} |",
        f"| Subtle-Defect Recall | {_fmt_pct(m['subtle_recall'])} |",
        f"| F1 Score | {m['f1']:.4f} |",
        "",
        "## False-Negative Blind-Spot Register",
        "",
    ]
    if not cert["blind_spots"]:
        lines.append("No missed defects.")
    else:
        lines += [
            "| Case | Mutation | Risk | Subtlety | Clinical Impact | Reference |",
            "|---|---|---|---|---|---|",
        ]
        for b in cert["blind_spots"]:
            lines.append(
                f"| `{b['case_id']}` | `{b['mutation_id']}` | {b['risk_tier'].upper()} | "
                f"{b['subtlety']} | {b['clinical_impact']} | {b['tg275_reference']} |"
            )
    lines += ["", "## False-Positive Register (Negative Controls)", ""]
    if not cert["false_alarms"]:
        lines.append("No false alarms on negative controls.")
    else:
        for fa in cert["false_alarms"]:
            lines.append(
                f"- `{fa['case_id']}` (`{fa['mutation_id']}`): {fa['title']} — {fa['checker_message']}"
            )
    lines += [
        "",
        "---",
        f"*Generated by RT-Plan Guardian {cert['tool']['version']} ({cert['generated_at']}). "
        "Decision-support commissioning utility; clinical validation remains the responsibility of a QMP.*",
        "",
    ]
    return "\n".join(lines)


_ENV = Environment(
    loader=PackageLoader("rt_plan_guardian.report", "templates"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_commissioning_html(cert: dict[str, Any]) -> str:
    return _ENV.get_template("commissioning.html.j2").render(c=cert, fmt_pct=_fmt_pct)


def write_commissioning_reports(
    cert: dict[str, Any],
    out_dir: str | Path,
    formats: tuple[str, ...] = ("json", "md", "html"),
    stem: str = "tg275_commissioning_certificate",
) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    renderers = {
        "json": lambda: json.dumps(cert, indent=2),
        "md": lambda: render_commissioning_markdown(cert),
        "html": lambda: render_commissioning_html(cert),
    }
    written: list[Path] = []
    for fmt in formats:
        if fmt not in renderers:
            raise GuardianError(f"Unknown commissioning report format {fmt!r}")
        p = out / f"{stem}.{fmt}"
        p.write_text(renderers[fmt](), encoding="utf-8")
        written.append(p)
    return written
