"""Synthetic photon (linac) case and its defects.

Geometry (all mm, patient coordinates):
  PTV         cylinder r=20, z in [-15, 15], 5 mm slices           (ROI 1, 60 Gy in 30 fx)
  BODY        cylinder r=100, z in [-50, 50]                       (ROI 2)
  SpinalCord  cylinder r=6 at (0, 55), z in [-40, 40]              (ROI 3)
  Dose grid   3 mm, x/y in [-60, 60], z in [-30, 30]; ellipsoidal plateau of ~62 Gy
  Beams       AP (gantry 0) and PA (gantry 180), isocentre at origin, 6 MV, 10-pair MLC
"""

from __future__ import annotations

import numpy as np
from pydicom.dataset import Dataset

from .common import (
    RT_DOSE,
    RT_PLAN,
    RT_STRUCT,
    SyntheticCase,
    base_dataset,
    beam_by_number,
    circle,
    defect,
    make_uid,
    other_uid,
    rounded,
)

PRESCRIPTION_GY = 60.0
FRACTIONS = 30


# --------------------------------------------------------------------------- RTSTRUCT
def build_structure_set(seed: int = 0) -> Dataset:
    ds = base_dataset("RTSTRUCT", RT_STRUCT, seed, "struct")
    frame_uid = make_uid(seed, "frame")
    ds.StructureSetLabel = "SYNTH_SS"
    ds.StructureSetName = "Synthetic structure set"
    ds.StructureSetDate = "20260101"
    ds.StructureSetTime = "120000"
    ref_frame = Dataset()
    ref_frame.FrameOfReferenceUID = frame_uid
    ds.ReferencedFrameOfReferenceSequence = [ref_frame]

    # (number, name, interpreted type, centre x, centre y, radius, z levels)
    specs = [
        (1, "PTV", "PTV", 0.0, 0.0, 20.0, np.arange(-15, 15.1, 5.0)),
        (2, "BODY", "EXTERNAL", 0.0, 0.0, 100.0, np.arange(-50, 50.1, 5.0)),
        (3, "SpinalCord", "ORGAN", 0.0, 55.0, 6.0, np.arange(-40, 40.1, 5.0)),
    ]
    roi_seq, contour_seq, obs_seq = [], [], []
    for number, name, rtype, cx, cy, r, zs in specs:
        roi = Dataset()
        roi.ROINumber = number
        roi.ReferencedFrameOfReferenceUID = frame_uid
        roi.ROIName = name
        roi.ROIGenerationAlgorithm = "MANUAL"
        roi_seq.append(roi)

        rc = Dataset()
        rc.ReferencedROINumber = number
        rc.ROIDisplayColor = [255, 0, 0]
        contours = []
        for k, z in enumerate(zs, start=1):
            pts = circle(cx, cy, r)
            c = Dataset()
            c.ContourNumber = k
            c.ContourGeometricType = "CLOSED_PLANAR"
            c.NumberOfContourPoints = len(pts)
            c.ContourData = rounded(np.column_stack([pts, np.full(len(pts), z)]).ravel())
            contours.append(c)
        rc.ContourSequence = contours
        contour_seq.append(rc)

        obs = Dataset()
        obs.ObservationNumber = number
        obs.ReferencedROINumber = number
        obs.RTROIInterpretedType = rtype
        obs.ROIInterpreter = ""
        obs_seq.append(obs)
    ds.StructureSetROISequence = roi_seq
    ds.ROIContourSequence = contour_seq
    ds.RTROIObservationsSequence = obs_seq
    return ds


# --------------------------------------------------------------------------- RTPLAN
def build_beam(number: int, name: str, gantry: float) -> Dataset:
    beam = Dataset()
    beam.Manufacturer = "RT-Plan Guardian synthetic generator"
    beam.TreatmentMachineName = "LINAC1"
    beam.BeamNumber = number
    beam.BeamName = name
    beam.BeamType = "STATIC"
    beam.RadiationType = "PHOTON"
    beam.TreatmentDeliveryType = "TREATMENT"
    beam.NumberOfWedges = 0
    beam.NumberOfCompensators = 0
    beam.NumberOfBoli = 0
    beam.NumberOfBlocks = 0
    beam.FinalCumulativeMetersetWeight = 1.0
    beam.NumberOfControlPoints = 2
    beam.PrimaryDosimeterUnit = "MU"
    beam.SourceAxisDistance = 1000.0

    devices = []
    for kind, pairs in (("ASYMX", 1), ("ASYMY", 1), ("MLCX", 10)):
        d = Dataset()
        d.RTBeamLimitingDeviceType = kind
        d.NumberOfLeafJawPairs = pairs
        if kind == "MLCX":
            d.LeafPositionBoundaries = [float(v) for v in range(-50, 51, 10)]
        devices.append(d)
    beam.BeamLimitingDeviceSequence = devices

    def positions(kind: str, values: list[float]) -> Dataset:
        p = Dataset()
        p.RTBeamLimitingDeviceType = kind
        p.LeafJawPositions = values
        return p

    cp0 = Dataset()
    cp0.ControlPointIndex = 0
    cp0.NominalBeamEnergy = 6.0
    cp0.DoseRateSet = 600.0
    cp0.BeamLimitingDevicePositionSequence = [
        positions("ASYMX", [-40.0, 40.0]),
        positions("ASYMY", [-40.0, 40.0]),
        positions("MLCX", [-30.0] * 10 + [30.0] * 10),
    ]
    cp0.GantryAngle = gantry
    cp0.GantryRotationDirection = "NONE"
    cp0.BeamLimitingDeviceAngle = 0.0
    cp0.BeamLimitingDeviceRotationDirection = "NONE"
    cp0.PatientSupportAngle = 0.0
    cp0.PatientSupportRotationDirection = "NONE"
    cp0.TableTopEccentricAngle = 0.0
    cp0.IsocenterPosition = [0.0, 0.0, 0.0]
    cp0.SourceToSurfaceDistance = 900.0
    cp0.CumulativeMetersetWeight = 0.0

    cp1 = Dataset()
    cp1.ControlPointIndex = 1
    cp1.CumulativeMetersetWeight = 1.0
    beam.ControlPointSequence = [cp0, cp1]
    return beam


def plan_header(
    struct: Dataset, seed: int, sop_class: str = RT_PLAN, label: str = "SYNTH_PLAN"
) -> Dataset:
    """Plan scaffolding shared by every external-beam technique: identity, approval, target dose
    reference, structure-set link and a one-group fraction scheme (beams added by the caller)."""
    ds = base_dataset("RTPLAN", sop_class, seed, "plan")
    ds.RTPlanLabel = label
    ds.RTPlanName = "Synthetic plan"
    ds.RTPlanDate = "20260101"
    ds.RTPlanTime = "120000"
    ds.RTPlanGeometry = "PATIENT"
    ds.ApprovalStatus = "APPROVED"
    ds.ReviewDate = "20260102"
    ds.ReviewTime = "090000"
    ds.ReviewerName = "SYNTHETIC^REVIEWER"

    ref_struct = Dataset()
    ref_struct.ReferencedSOPClassUID = RT_STRUCT
    ref_struct.ReferencedSOPInstanceUID = struct.SOPInstanceUID
    ds.ReferencedStructureSetSequence = [ref_struct]

    dose_ref = Dataset()
    dose_ref.DoseReferenceNumber = 1
    dose_ref.DoseReferenceUID = make_uid(seed, "doseref")
    dose_ref.DoseReferenceStructureType = "VOLUME"
    dose_ref.DoseReferenceDescription = "PTV"
    dose_ref.DoseReferenceType = "TARGET"
    dose_ref.ReferencedROINumber = 1
    dose_ref.TargetPrescriptionDose = PRESCRIPTION_GY
    ds.DoseReferenceSequence = [dose_ref]
    return ds


def fraction_group(
    beam_numbers_mu: list[tuple[int, float]], beam_dose: float, fractions: int
) -> Dataset:
    fg = Dataset()
    fg.FractionGroupNumber = 1
    fg.NumberOfFractionsPlanned = fractions
    fg.NumberOfBeams = len(beam_numbers_mu)
    fg.NumberOfBrachyApplicationSetups = 0
    refs = []
    for number, mu in beam_numbers_mu:
        r = Dataset()
        r.ReferencedBeamNumber = number
        r.BeamDose = beam_dose
        r.BeamMeterset = mu
        refs.append(r)
    fg.ReferencedBeamSequence = refs
    return fg


def build_plan(struct: Dataset, seed: int = 0) -> Dataset:
    ds = plan_header(struct, seed)
    ds.RTPlanName = "Synthetic AP/PA plan"
    ds.FractionGroupSequence = [fraction_group([(1, 180.0), (2, 190.0)], 1.0, FRACTIONS)]
    ds.BeamSequence = [build_beam(1, "AP", 0.0), build_beam(2, "PA", 180.0)]
    return ds


# --------------------------------------------------------------------------- RTDOSE
def build_dose(
    plan: Dataset,
    struct: Dataset,
    seed: int = 0,
    *,
    spacing_xy: float = 3.0,
    spacing_z: float = 3.0,
    peak_gy: float = 62.0,
    dose_type: str = "PHYSICAL",
) -> Dataset:
    ds = base_dataset("RTDOSE", RT_DOSE, seed, "dose")
    ds.FrameOfReferenceUID = struct.ReferencedFrameOfReferenceSequence[0].FrameOfReferenceUID
    ds.PositionReferenceIndicator = ""
    n_xy = round(120.0 / spacing_xy) + 1
    n_z = round(60.0 / spacing_z) + 1
    xs = -60.0 + spacing_xy * np.arange(n_xy)
    zs = -30.0 + spacing_z * np.arange(n_z)
    Z, Y, X = np.meshgrid(zs, xs, xs, indexing="ij")
    rho = np.sqrt((X**2 + Y**2) / 40.0**2 + Z**2 / 35.0**2)
    dose = peak_gy / (1.0 + np.exp(-(1.0 - rho) / 0.08))
    return finish_dose(ds, plan, dose, xs, zs, spacing_xy, dose_type)


def finish_dose(
    ds: Dataset,
    plan: Dataset,
    dose: np.ndarray,
    xs: np.ndarray,
    zs: np.ndarray,
    spacing_xy: float,
    dose_type: str = "PHYSICAL",
    scaling: float = 0.001,
) -> Dataset:
    """Encode a (nz, ny, nx) Gy array as RT Dose pixel data on the grid xs x xs x zs."""
    stored = np.round(dose / scaling).astype("<u2")
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.Rows = len(xs)
    ds.Columns = len(xs)
    ds.NumberOfFrames = len(zs)
    ds.PixelSpacing = [spacing_xy, spacing_xy]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.ImagePositionPatient = [float(xs[0]), float(xs[0]), float(zs[0])]
    ds.GridFrameOffsetVector = rounded(zs - zs[0])
    ds.DoseUnits = "GY"
    ds.DoseType = dose_type
    ds.DoseSummationType = "PLAN"
    ds.DoseGridScaling = scaling
    ref_plan = Dataset()
    ref_plan.ReferencedSOPClassUID = plan.SOPClassUID
    ref_plan.ReferencedSOPInstanceUID = plan.SOPInstanceUID
    ds.ReferencedRTPlanSequence = [ref_plan]
    ds.PixelData = stored.tobytes()
    return ds


def make(seed: int = 0) -> SyntheticCase:
    struct = build_structure_set(seed)
    plan = build_plan(struct, seed)
    dose = build_dose(plan, struct, seed)
    return SyntheticCase(plan=plan, struct=struct, dose=dose, seed=seed, technique="photon")


# --------------------------------------------------------------------------- defects
# ---- intake / identity
@defect("missing_dose_file", "RTDOSE was not exported with the plan", {"INPUT-001"})
def _(c: SyntheticCase) -> None:
    c.dose = None


@defect("missing_structure_set_file", "RTSTRUCT was not exported with the plan", {"INPUT-001"})
def _(c: SyntheticCase) -> None:
    c.struct = None


@defect("wrong_patient_id", "Dose file belongs to a different patient ID", {"IDENT-001"})
def _(c: SyntheticCase) -> None:
    c.dose.PatientID = "RTPG-9999"


@defect("patient_name_mismatch", "Structure set carries a different patient name", {"IDENT-002"})
def _(c: SyntheticCase) -> None:
    c.struct.PatientName = "OTHER^PERSON"


@defect("duplicate_sop_uid", "Dose and plan share one SOP Instance UID", {"IDENT-003"})
def _(c: SyntheticCase) -> None:
    c.dose.SOPInstanceUID = c.plan.SOPInstanceUID


# ---- linkage
@defect(
    "plan_references_other_structure_set",
    "Plan was made on a different structure set",
    {"LINK-001"},
)
def _(c: SyntheticCase) -> None:
    c.plan.ReferencedStructureSetSequence[0].ReferencedSOPInstanceUID = other_uid(c, "struct")


@defect(
    "dose_references_other_plan", "Dose was calculated for a different plan revision", {"LINK-002"}
)
def _(c: SyntheticCase) -> None:
    c.dose.ReferencedRTPlanSequence[0].ReferencedSOPInstanceUID = other_uid(c, "plan")


@defect(
    "dose_wrong_frame_of_reference",
    "Dose uses another coordinate system than the contours",
    {"LINK-003"},
)
def _(c: SyntheticCase) -> None:
    c.dose.FrameOfReferenceUID = other_uid(c, "frame")


@defect(
    "target_roi_missing_from_structure_set",
    "Prescription points at an ROI number that does not exist",
    {"LINK-004"},
    skips={"BEAM-006", "DOSE-005", "DOSE-006"},
)
def _(c: SyntheticCase) -> None:
    c.plan.DoseReferenceSequence[0].ReferencedROINumber = 99


# ---- structures
@defect(
    "duplicate_roi_number",
    "Two ROIs share ROI number 2 (and a contour is orphaned)",
    {"STRUCT-001"},
)
def _(c: SyntheticCase) -> None:
    c.struct.StructureSetROISequence[2].ROINumber = 2


@defect("empty_roi_name", "An ROI has no name", {"STRUCT-001"})
def _(c: SyntheticCase) -> None:
    c.struct.StructureSetROISequence[2].ROIName = ""


@defect(
    "target_not_named_ptv",
    "Target ROI does not match the site's required naming pattern",
    {"STRUCT-002"},
    rules={"STRUCT-002": {"params": {"required_roi_patterns": ["^PTV", "^(BODY|EXTERNAL)$"]}}},
)
def _(c: SyntheticCase) -> None:
    c.struct.StructureSetROISequence[0].ROIName = "TargetVol"


@defect("empty_cord_contours", "Spinal cord ROI has a name but no contours", {"STRUCT-003"})
def _(c: SyntheticCase) -> None:
    del c.struct.ROIContourSequence[2].ContourSequence


@defect(
    "malformed_contour_point_count",
    "A PTV contour declares the wrong number of points",
    {"STRUCT-004"},
)
def _(c: SyntheticCase) -> None:
    c.struct.ROIContourSequence[0].ContourSequence[0].NumberOfContourPoints = 99


@defect("nonplanar_contour", "One cord contour point is 2 mm off its slice", {"STRUCT-004"})
def _(c: SyntheticCase) -> None:
    contour = c.struct.ROIContourSequence[2].ContourSequence[0]
    data = [float(v) for v in contour.ContourData]
    data[2] += 2.0
    contour.ContourData = data


@defect(
    "missing_roi_interpreted_type", "An ROI observation has no interpreted type", {"STRUCT-005"}
)
def _(c: SyntheticCase) -> None:
    c.struct.RTROIObservationsSequence[2].RTROIInterpretedType = ""


# ---- plan
@defect("unapproved_plan", "Plan exported while still UNAPPROVED", {"PLAN-001"})
def _(c: SyntheticCase) -> None:
    c.plan.ApprovalStatus = "UNAPPROVED"


@defect(
    "unsupported_radiation_type", "Beam 2 is a neutron beam, which no rule covers", {"PLAN-002"}
)
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 2).RadiationType = "NEUTRON"


@defect("empty_plan", "Plan contains no beams at all", {"PLAN-002"}, skips={"PLAN-004"})
def _(c: SyntheticCase) -> None:
    c.plan.BeamSequence = []
    c.plan.FractionGroupSequence[0].ReferencedBeamSequence = []
    c.plan.FractionGroupSequence[0].NumberOfBeams = 0


@defect(
    "excessive_fractions", "300 fractions planned instead of 30 (typo)", {"PLAN-003", "PLAN-004"}
)
def _(c: SyntheticCase) -> None:
    c.plan.FractionGroupSequence[0].NumberOfFractionsPlanned = 300


@defect(
    "prescription_mismatch",
    "Prescription edited to 66 Gy but beam doses and dose grid still describe 60 Gy",
    {"PLAN-004", "DOSE-006"},
)
def _(c: SyntheticCase) -> None:
    c.plan.DoseReferenceSequence[0].TargetPrescriptionDose = 66.0


@defect("mixed_machines", "Beam 2 assigned to a different treatment machine", {"PLAN-005"})
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 2).TreatmentMachineName = "LINAC2"


# ---- beams
@defect(
    "beam_missing_from_fraction_group",
    "Beam 2 dropped from the fraction group",
    {"BEAM-001", "PLAN-004"},
)
def _(c: SyntheticCase) -> None:
    fg = c.plan.FractionGroupSequence[0]
    fg.ReferencedBeamSequence = [fg.ReferencedBeamSequence[0]]


@defect("duplicate_beam_number", "Two beams carry beam number 1", {"BEAM-001"})
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 2).BeamNumber = 1


@defect("broken_control_points", "Final cumulative meterset weight is 0.5, not 1.0", {"BEAM-002"})
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 1).ControlPointSequence[-1].CumulativeMetersetWeight = 0.5


@defect("gantry_angle_out_of_range", "Gantry angle of 365 degrees", {"BEAM-003"})
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 1).ControlPointSequence[0].GantryAngle = 365.0


@defect("zero_beam_energy", "Beam energy exported as 0 MV", {"BEAM-004"})
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 1).ControlPointSequence[0].NominalBeamEnergy = 0.0


@defect("excessive_mu", "Beam 1 carries 5000 MU", {"BEAM-005"})
def _(c: SyntheticCase) -> None:
    c.plan.FractionGroupSequence[0].ReferencedBeamSequence[0].BeamMeterset = 5000.0


@defect(
    "isocentre_outside_target",
    "Both isocentres were moved 100 mm superior of the target",
    {"BEAM-006"},
)
def _(c: SyntheticCase) -> None:
    for beam in c.plan.BeamSequence:
        beam.ControlPointSequence[0].IsocenterPosition = [0.0, 0.0, 100.0]


@defect("crossed_mlc_leaves", "MLC leaf pair 3 crosses its opposing leaf", {"BEAM-007"})
def _(c: SyntheticCase) -> None:
    mlc = next(
        d
        for d in beam_by_number(c, 1).ControlPointSequence[0].BeamLimitingDevicePositionSequence
        if d.RTBeamLimitingDeviceType == "MLCX"
    )
    pos = [float(v) for v in mlc.LeafJawPositions]
    pos[3] = 40.0  # bank A leaf beyond bank B leaf at +30
    mlc.LeafJawPositions = pos


@defect(
    "undeclared_jaw_pairs",
    "Jaw declared with 0 pairs but positioned (vendor-export quirk)",
    {"BEAM-008"},
)
def _(c: SyntheticCase) -> None:
    for d in beam_by_number(c, 1).BeamLimitingDeviceSequence:
        if d.RTBeamLimitingDeviceType == "ASYMX":
            d.NumberOfLeafJawPairs = 0


@defect(
    "cobalt_timer_unit_in_minutes", "Beam meterset exported in MINUTE (valid, within limits)", set()
)
def _(c: SyntheticCase) -> None:
    for beam in c.plan.BeamSequence:
        beam.PrimaryDosimeterUnit = "MINUTE"
    for r in c.plan.FractionGroupSequence[0].ReferencedBeamSequence:
        r.BeamMeterset = 0.9  # < 1 would FAIL if the limit were applied as MU


# ---- dose
@defect(
    "dose_units_relative",
    "Dose grid is relative, not absolute Gy",
    {"DOSE-001"},
    skips={"DOSE-004", "DOSE-006"},
)
def _(c: SyntheticCase) -> None:
    c.dose.DoseUnits = "RELATIVE"


@defect(
    "truncated_pixel_data",
    "Dose pixel data is cut in half",
    {"DOSE-002"},
    skips={"DOSE-004", "DOSE-006"},
)
def _(c: SyntheticCase) -> None:
    c.dose.PixelData = c.dose.PixelData[: len(c.dose.PixelData) // 2]


@defect("coarse_dose_grid", "Dose calculated on a 5 mm grid", {"DOSE-003"})
def _(c: SyntheticCase) -> None:
    c.dose = build_dose(c.plan, c.struct, c.seed, spacing_xy=5.0)


@defect("hotspot", "Dose scaled up 20% (peak 124% of prescription)", {"DOSE-004"})
def _(c: SyntheticCase) -> None:
    c.dose.DoseGridScaling = 0.0012


@defect(
    "target_outside_dose_grid", "Dose grid origin shifted 100 mm in x", {"DOSE-005", "DOSE-006"}
)
def _(c: SyntheticCase) -> None:
    ipp = [float(v) for v in c.dose.ImagePositionPatient]
    c.dose.ImagePositionPatient = [ipp[0] + 100.0, ipp[1], ipp[2]]


@defect("underdosed_target", "Dose scaled down 20% (target under-covered)", {"DOSE-006"})
def _(c: SyntheticCase) -> None:
    c.dose.DoseGridScaling = 0.0008
