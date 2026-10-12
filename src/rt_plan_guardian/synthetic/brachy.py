"""Synthetic interstitial HDR Ir-192 case (two catheters, five stepwise dwells each) and defects.

The structure mirrors the two real vendor exports it was modelled on (Varian and Elekta TPS):
a SITE-type prescription that names its target only by description, open non-planar catheter
contours in the structure set, stepwise channels with two control points per dwell, and a total
reference air kerma that equals source strength x total time.
"""

from __future__ import annotations

import numpy as np
from pydicom.dataset import Dataset

from .common import RT_DOSE, SyntheticCase, base_dataset, defect, rounded
from .photon import build_structure_set, finish_dose, plan_header

BRACHY_RX_GY = 30.0
BRACHY_FRACTIONS = 5
SETUP_DOSE_GY = 6.0
AIR_KERMA_RATE = 40700.0  # uGy/h at 1 m (10 Ci-class Ir-192)
HALF_LIFE_DAYS = 73.83
FINAL_TIME_WEIGHT = 2.0

#: (catheter x mm, dwell times s) - dwell positions are z = -10, -5, 0, 5, 10 mm
_CHANNELS = [(-8.0, [4.0, 6.0, 8.0, 6.0, 4.0]), (8.0, [3.0, 5.0, 7.0, 5.0, 3.0])]
_Z = [-10.0, -5.0, 0.0, 5.0, 10.0]


def _catheter_rois(struct: Dataset) -> None:
    """Add two open non-planar catheter polylines (valid DICOM, but not volumes)."""
    frame_uid = struct.ReferencedFrameOfReferenceSequence[0].FrameOfReferenceUID
    for n, (x, _) in enumerate(_CHANNELS, start=4):
        roi = Dataset()
        roi.ROINumber = n
        roi.ReferencedFrameOfReferenceUID = frame_uid
        roi.ROIName = f"Catheter{n - 3}"
        roi.ROIGenerationAlgorithm = "MANUAL"
        struct.StructureSetROISequence.append(roi)
        rc = Dataset()
        rc.ReferencedROINumber = n
        rc.ROIDisplayColor = [0, 255, 0]
        c = Dataset()
        c.ContourNumber = 1
        c.ContourGeometricType = "OPEN_NONPLANAR"
        zs = np.arange(-20.0, 20.1, 5.0)
        c.NumberOfContourPoints = len(zs)
        c.ContourData = rounded(
            np.column_stack([np.full(len(zs), x), np.zeros(len(zs)), zs]).ravel()
        )
        rc.ContourSequence = [c]
        struct.ROIContourSequence.append(rc)
        obs = Dataset()
        obs.ObservationNumber = n
        obs.ReferencedROINumber = n
        obs.RTROIInterpretedType = "BRACHY_CHANNEL"
        obs.ROIInterpreter = ""
        struct.RTROIObservationsSequence.append(obs)


def _channel(number: int, x: float, times: list[float]) -> Dataset:
    total = float(sum(times))
    ch = Dataset()
    ch.ChannelNumber = number
    ch.ChannelLength = 1300.0
    ch.ChannelTotalTime = total
    ch.SourceMovementType = "STEPWISE"
    ch.TransferTubeNumber = number
    ch.TransferTubeLength = 1000.0
    ch.FinalCumulativeTimeWeight = FINAL_TIME_WEIGHT
    ch.ReferencedSourceNumber = 1
    cps: list[Dataset] = []
    cumulative = 0.0
    for k, (z, t) in enumerate(zip(_Z, times, strict=True)):
        position = 60.0 + 5.0 * k
        opening, closing = Dataset(), Dataset()
        for cp, index, weight in (
            (opening, 2 * k, cumulative),
            (closing, 2 * k + 1, cumulative + FINAL_TIME_WEIGHT * t / total),
        ):
            cp.ControlPointIndex = index
            cp.ControlPointRelativePosition = position
            cp.CumulativeTimeWeight = round(weight, 9)
            cp.ControlPoint3DPosition = [x, 0.0, z]
        cumulative += FINAL_TIME_WEIGHT * t / total
        cps += [opening, closing]
    ch.NumberOfControlPoints = len(cps)
    ch.BrachyControlPointSequence = cps
    return ch


def build_brachy_plan(struct: Dataset, seed: int = 0) -> Dataset:
    ds = plan_header(struct, seed, label="SYNTH_HDR")
    ds.RTPlanName = "Synthetic interstitial HDR plan"
    ref = ds.DoseReferenceSequence[0]
    del ref.ReferencedROINumber
    ref.DoseReferenceStructureType = "SITE"  # target named only by description, as vendors export
    ref.TargetPrescriptionDose = BRACHY_RX_GY
    ds.BrachyTreatmentTechnique = "INTERSTITIAL"
    ds.BrachyTreatmentType = "HDR"
    machine = Dataset()
    machine.TreatmentMachineName = "AFTERLOADER1"
    ds.TreatmentMachineSequence = [machine]

    source = Dataset()
    source.SourceNumber = 1
    source.SourceType = "POINT"
    source.SourceIsotopeName = "Ir-192"
    source.SourceIsotopeHalfLife = HALF_LIFE_DAYS
    source.ReferenceAirKermaRate = AIR_KERMA_RATE
    source.SourceStrengthReferenceDate = "20251220"
    source.SourceStrengthReferenceTime = "120000"
    ds.SourceSequence = [source]

    setup = Dataset()
    setup.ApplicationSetupType = "OTHER"
    setup.ApplicationSetupNumber = 1
    setup.ApplicationSetupName = "SYNTH_BRACHY"
    channels = [_channel(i, x, times) for i, (x, times) in enumerate(_CHANNELS, start=1)]
    setup.TotalReferenceAirKerma = round(
        sum(AIR_KERMA_RATE * float(c.ChannelTotalTime) / 3600.0 for c in channels), 6
    )
    setup.ChannelSequence = channels
    ds.ApplicationSetupSequence = [setup]

    fg = Dataset()
    fg.FractionGroupNumber = 1
    fg.NumberOfFractionsPlanned = BRACHY_FRACTIONS
    fg.NumberOfBeams = 0
    fg.NumberOfBrachyApplicationSetups = 1
    r = Dataset()
    r.ReferencedBrachyApplicationSetupNumber = 1
    r.BrachyApplicationSetupDose = SETUP_DOSE_GY
    fg.ReferencedBrachyApplicationSetupSequence = [r]
    ds.FractionGroupSequence = [fg]
    return ds


def build_brachy_dose(plan: Dataset, struct: Dataset, seed: int = 0) -> Dataset:
    """A point-source 1/r^2 dose summed over the dwell positions (steep near-source gradients)."""
    ds = base_dataset("RTDOSE", RT_DOSE, seed, "dose")
    ds.FrameOfReferenceUID = struct.ReferencedFrameOfReferenceSequence[0].FrameOfReferenceUID
    ds.PositionReferenceIndicator = ""
    spacing = 3.0
    xs = -60.0 + spacing * np.arange(41)
    zs = -30.0 + spacing * np.arange(21)
    Z, Y, X = np.meshgrid(zs, xs, xs, indexing="ij")
    raw = np.zeros_like(X)
    for cx, times in _CHANNELS:
        for z, t in zip(_Z, times, strict=True):
            raw += t / ((X - cx) ** 2 + Y**2 + (Z - z) ** 2 + 16.0)
    centre = raw[10, 20, 20]
    dose = np.clip(raw * (40.0 / centre), 0.0, 300.0)
    return finish_dose(ds, plan, dose, xs, zs, spacing, scaling=0.005)


def make(seed: int = 0) -> SyntheticCase:
    struct = build_structure_set(seed)
    _catheter_rois(struct)
    plan = build_brachy_plan(struct, seed)
    dose = build_brachy_dose(plan, struct, seed)
    return SyntheticCase(plan=plan, struct=struct, dose=dose, seed=seed, technique="brachy")


# --------------------------------------------------------------------------- defects
def _setup(c: SyntheticCase) -> Dataset:
    return c.plan.ApplicationSetupSequence[0]


def _channel_of(c: SyntheticCase, n: int = 0) -> Dataset:
    return _setup(c).ChannelSequence[n]


@defect(
    "brachy_half_life_wrong",
    "Half-life of 100 d entered for Ir-192",
    {"BRACHY-001"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    c.plan.SourceSequence[0].SourceIsotopeHalfLife = 100.0


@defect(
    "brachy_source_strength_zero",
    "Reference air kerma rate is zero (while the plan still states a non-zero TRAK)",
    {"BRACHY-001", "BRACHY-002"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    c.plan.SourceSequence[0].ReferenceAirKermaRate = 0.0


@defect(
    "brachy_air_kerma_rate_units_declared",
    "Source declares SourceStrengthUnits=AIR_KERMA_RATE with the value in ReferenceAirKermaRate "
    "(valid; as exported by Elekta Oncentra) - must NOT be flagged",
    set(),
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    c.plan.SourceSequence[0].SourceStrengthUnits = "AIR_KERMA_RATE"


@defect(
    "brachy_water_dose_rate_without_strength",
    "A DOSE_RATE_WATER (beta) source without SourceStrength",
    {"BRACHY-001"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    c.plan.SourceSequence[0].SourceStrengthUnits = "DOSE_RATE_WATER"


@defect(
    "brachy_trak_inconsistent",
    "TotalReferenceAirKerma is 10% higher than rate x time",
    {"BRACHY-002"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    _setup(c).TotalReferenceAirKerma = float(_setup(c).TotalReferenceAirKerma) * 1.10


@defect(
    "brachy_channel_source_missing",
    "Channel references source 9 which is not defined",
    {"BRACHY-003"},
    skips={"BRACHY-002"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    _channel_of(c).ReferencedSourceNumber = 9


@defect(
    "brachy_control_point_count_wrong",
    "Channel declares 12 control points but has 10",
    {"BRACHY-003"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    _channel_of(c).NumberOfControlPoints = 12


@defect(
    "brachy_dwell_pair_positions_differ",
    "The two control points of one dwell disagree on position",
    {"BRACHY-003"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    _channel_of(c).BrachyControlPointSequence[3].ControlPointRelativePosition = 66.2


@defect(
    "brachy_position_beyond_channel",
    "Last dwell lies at 2000 mm in a 1300 mm channel",
    {"BRACHY-003"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    cps = _channel_of(c).BrachyControlPointSequence
    cps[8].ControlPointRelativePosition = 2000.0
    cps[9].ControlPointRelativePosition = 2000.0


@defect(
    "brachy_time_weights_decrease",
    "A cumulative time weight goes backwards",
    {"BRACHY-003", "BRACHY-006"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    _channel_of(c).BrachyControlPointSequence[5].CumulativeTimeWeight = 0.1


@defect(
    "brachy_source_reference_in_future",
    "Source strength reference date is after the plan date",
    {"BRACHY-004"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    c.plan.SourceSequence[0].SourceStrengthReferenceDate = "20260301"


@defect(
    "brachy_source_strength_stale",
    "Source strength reference date is a year old (no decay correction)",
    {"BRACHY-004"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    c.plan.SourceSequence[0].SourceStrengthReferenceDate = "20250101"


@defect(
    "brachy_setup_not_delivered",
    "Fraction group references application setup 7, not 1",
    {"BRACHY-005"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    c.plan.FractionGroupSequence[0].ReferencedBrachyApplicationSetupSequence[
        0
    ].ReferencedBrachyApplicationSetupNumber = 7


@defect(
    "brachy_setup_count_mismatch",
    "Fraction group declares 2 setups but references 1",
    {"BRACHY-005"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    c.plan.FractionGroupSequence[0].NumberOfBrachyApplicationSetups = 2


@defect(
    "brachy_dwell_time_too_long",
    "A dwell of 8 s on a protocol that caps dwell time at 5 s",
    {"BRACHY-006"},
    technique="brachy",
    rules={"BRACHY-006": {"params": {"max_dwell_s": 5.0}}},
)
def _(c: SyntheticCase) -> None:
    pass


@defect(
    "brachy_step_size_not_allowed",
    "A dwell shifted 1.3 mm so spacing is not a multiple of the allowed 2.5/5/10 mm steps",
    {"BRACHY-006"},
    technique="brachy",
    rules={"BRACHY-006": {"params": {"allowed_step_sizes_mm": [2.5, 5.0, 10.0]}}},
)
def _(c: SyntheticCase) -> None:
    cps = _channel_of(c).BrachyControlPointSequence
    cps[4].ControlPointRelativePosition = 71.3
    cps[5].ControlPointRelativePosition = 71.3


@defect(
    "brachy_prescription_mismatch",
    "Setup dose edited to 7 Gy/fraction; prescription still 30 Gy",
    {"PLAN-004"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    c.plan.FractionGroupSequence[0].ReferencedBrachyApplicationSetupSequence[
        0
    ].BrachyApplicationSetupDose = 7.0


@defect(
    "brachy_catheter_contour_malformed",
    "Catheter contour declares the wrong point count",
    {"STRUCT-004"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    c.struct.ROIContourSequence[3].ContourSequence[0].NumberOfContourPoints = 3


@defect(
    "brachy_target_not_in_dose_grid",
    "Dose grid shifted 100 mm so the PTV is outside it",
    {"DOSE-005"},
    technique="brachy",
)
def _(c: SyntheticCase) -> None:
    ipp = [float(v) for v in c.dose.ImagePositionPatient]
    c.dose.ImagePositionPatient = [ipp[0] + 100.0, ipp[1], ipp[2]]
