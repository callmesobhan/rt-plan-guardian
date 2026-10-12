"""Synthetic proton pencil-beam-scanning case (RT Ion Plan) and its defects.

Two scanned proton fields (gantry 90 and 270, 3 energy layers each, 4 spots per layer), a range
shifter and a snout; dose is RBE-weighted (DoseType EFFECTIVE). The structure set and the dose
distribution are shared with the photon case so only the delivery model differs. The beam
structure follows DICOM PS3.3 C.8.8.25 / C.8.8.25.7: two control points per energy layer.
"""

from __future__ import annotations

from pydicom.dataset import Dataset

from .common import (
    RT_ION_PLAN,
    SyntheticCase,
    beam_by_number,
    control_points_of,
    defect,
)
from .photon import (
    FRACTIONS,
    build_dose,
    build_structure_set,
    fraction_group,
    plan_header,
)

# (energy MeV, [(x, y, weight), ...]) - each beam's weights sum to 100.0
_LAYERS_A = [
    (110.0, [(-10, -10, 8), (10, -10, 8), (10, 10, 8), (-10, 10, 8)]),
    (120.0, [(0, 0, 9), (-12, 0, 8), (12, 0, 8), (0, 12, 8)]),
    (130.0, [(-6, -6, 9), (6, -6, 9), (6, 6, 9), (-6, 6, 8)]),
]
_LAYERS_B = [(e + 5.0, spots) for e, spots in reversed(_LAYERS_A)]
_ISO = [0.0, 0.0, 0.0]


def _control_point(
    index: int,
    energy: float,
    spots: list[tuple[float, float, float]],
    cmw: float,
    first: bool,
    closing: bool,
) -> Dataset:
    cp = Dataset()
    cp.ControlPointIndex = index
    cp.NominalBeamEnergy = energy
    cp.ScanSpotTuneID = "1.0"
    cp.NumberOfScanSpotPositions = len(spots)
    cp.ScanSpotPositionMap = [float(v) for x, y, _ in spots for v in (x, y)]
    cp.ScanSpotMetersetWeights = [0.0 if closing else float(w) for _, _, w in spots]
    cp.NumberOfPaintings = 1
    cp.CumulativeMetersetWeight = cmw
    return cp


def build_ion_beam(number: int, name: str, gantry: float, layers) -> Dataset:
    beam = Dataset()
    beam.Manufacturer = "RT-Plan Guardian synthetic generator"
    beam.TreatmentMachineName = "PROTON1"
    beam.BeamNumber = number
    beam.BeamName = name
    beam.BeamType = "STATIC"
    beam.RadiationType = "PROTON"
    beam.ScanMode = "MODULATED"
    beam.TreatmentDeliveryType = "TREATMENT"
    beam.PrimaryDosimeterUnit = "MU"
    beam.VirtualSourceAxisDistances = [2200.0, 2200.0]
    beam.NumberOfRangeShifters = 1
    beam.NumberOfLateralSpreadingDevices = 0
    beam.NumberOfRangeModulators = 0
    shifter = Dataset()
    shifter.RangeShifterNumber = 1
    shifter.RangeShifterID = "RS40"
    shifter.RangeShifterType = "BINARY"
    beam.RangeShifterSequence = [shifter]
    snout = Dataset()
    snout.SnoutID = "SNOUT1"
    beam.SnoutSequence = [snout]

    total = float(sum(w for _, spots in layers for _, _, w in spots))
    beam.FinalCumulativeMetersetWeight = total
    beam.NumberOfControlPoints = 2 * len(layers)
    cps: list[Dataset] = []
    cmw = 0.0
    for k, (energy, spots) in enumerate(layers):
        layer_weight = float(sum(w for _, _, w in spots))
        opening = _control_point(2 * k, energy, spots, cmw, first=k == 0, closing=False)
        closing = _control_point(
            2 * k + 1, energy, spots, cmw + layer_weight, first=False, closing=True
        )
        if k == 0:  # geometry is given once, on the first control point
            opening.GantryAngle = gantry
            opening.GantryRotationDirection = "NONE"
            opening.PatientSupportAngle = 0.0
            opening.PatientSupportRotationDirection = "NONE"
            opening.IsocenterPosition = list(_ISO)
            opening.SnoutPosition = 300.0
            setting = Dataset()
            setting.RangeShifterSetting = "OUT"
            setting.ReferencedRangeShifterNumber = 1
            opening.RangeShifterSettingsSequence = [setting]
        cps += [opening, closing]
        cmw += layer_weight
    beam.IonControlPointSequence = cps
    return beam


def build_ion_plan(struct: Dataset, seed: int = 0) -> Dataset:
    ds = plan_header(struct, seed, sop_class=RT_ION_PLAN, label="SYNTH_PROTON")
    ds.RTPlanName = "Synthetic two-field PBS proton plan"
    ds.FractionGroupSequence = [fraction_group([(1, 100.0), (2, 100.0)], 1.0, FRACTIONS)]
    ds.IonBeamSequence = [
        build_ion_beam(1, "RLAT", 90.0, _LAYERS_A),
        build_ion_beam(2, "LLAT", 270.0, _LAYERS_B),
    ]
    return ds


def make(seed: int = 0) -> SyntheticCase:
    struct = build_structure_set(seed)
    plan = build_ion_plan(struct, seed)
    dose = build_dose(plan, struct, seed, dose_type="EFFECTIVE")
    return SyntheticCase(plan=plan, struct=struct, dose=dose, seed=seed, technique="proton")


# --------------------------------------------------------------------------- defects
def _cps(c: SyntheticCase, beam: int = 1):
    return control_points_of(beam_by_number(c, beam))


@defect(
    "ion_scan_parameter_missing",
    "Spot-tune ID missing on a modulated control point",
    {"ION-001"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    del _cps(c)[0].ScanSpotTuneID


@defect(
    "ion_species_not_identified",
    "Radiation type ION without mass number / atomic number / charge state",
    {"ION-001"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 1).RadiationType = "ION"


@defect(
    "ion_spot_count_mismatch",
    "Declared spot count is 5 but the map and weights hold 4 spots",
    {"ION-002"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    cps = _cps(c)
    cps[0].NumberOfScanSpotPositions = 5
    cps[1].NumberOfScanSpotPositions = 5


@defect(
    "ion_negative_spot_weight",
    "One spot weight is negative (sum unchanged)",
    {"ION-002"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    w = [float(v) for v in _cps(c)[0].ScanSpotMetersetWeights]
    w[0] -= 10.0
    w[1] += 10.0
    _cps(c)[0].ScanSpotMetersetWeights = w


@defect(
    "ion_layer_weight_sum_mismatch",
    "Spot weights of a layer no longer sum to the meterset step (a spot was edited)",
    {"ION-003"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    w = [float(v) for v in _cps(c)[0].ScanSpotMetersetWeights]
    w[0] += 5.0
    _cps(c)[0].ScanSpotMetersetWeights = w


@defect(
    "ion_segment_positions_differ",
    "The two control points of a segment disagree on a spot position",
    {"ION-003"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    pos = [float(v) for v in _cps(c)[1].ScanSpotPositionMap]
    pos[0] += 2.0
    _cps(c)[1].ScanSpotPositionMap = pos


@defect(
    "ion_closing_weights_nonzero",
    "Closing control point of a segment still carries weights",
    {"ION-003"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    w = [float(v) for v in _cps(c)[1].ScanSpotMetersetWeights]
    w[0] = 1.0
    _cps(c)[1].ScanSpotMetersetWeights = w


@defect(
    "ion_energy_above_machine_range",
    "Layer energy of 250 MeV on a machine limited to 70-230 MeV",
    {"ION-004"},
    technique="proton",
    rules={"ION-004": {"params": {"min_energy_mev": 70.0, "max_energy_mev": 230.0}}},
)
def _(c: SyntheticCase) -> None:
    cps = _cps(c)
    cps[4].NominalBeamEnergy = 250.0
    cps[5].NominalBeamEnergy = 250.0


@defect(
    "ion_spot_outside_scan_field",
    "A spot at 160 mm on a nozzle limited to 150 mm",
    {"ION-005"},
    technique="proton",
    rules={"ION-005": {"params": {"max_abs_spot_position_mm": 150.0}}},
)
def _(c: SyntheticCase) -> None:
    for cp in _cps(c)[0:2]:
        pos = [float(v) for v in cp.ScanSpotPositionMap]
        pos[0] = 160.0
        cp.ScanSpotPositionMap = pos


@defect(
    "ion_range_shifter_not_declared",
    "Range-shifter setting references shifter 9 which the beam does not declare",
    {"ION-006"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    _cps(c)[0].RangeShifterSettingsSequence[0].ReferencedRangeShifterNumber = 9


@defect(
    "ion_snout_position_missing",
    "Snout declared but its position is missing at control point 0",
    {"ION-006"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    del _cps(c)[0].SnoutPosition


@defect(
    "ion_gantry_angle_out_of_range",
    "Gantry angle of 400 degrees on an ion beam",
    {"BEAM-003"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    _cps(c)[0].GantryAngle = 400.0


@defect(
    "ion_final_weight_mismatch",
    "FinalCumulativeMetersetWeight disagrees with the last control point",
    {"BEAM-002"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 1).FinalCumulativeMetersetWeight = 90.0


@defect(
    "ion_isocentre_outside_target",
    "Proton isocentres moved 100 mm out of the target",
    {"BEAM-006"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    for beam in c.plan.IonBeamSequence:
        beam.IonControlPointSequence[0].IsocenterPosition = [0.0, 0.0, 100.0]


@defect(
    "ion_dose_type_not_allowed",
    "Proton dose exported with an undefined DoseType",
    {"DOSE-001"},
    technique="proton",
)
def _(c: SyntheticCase) -> None:
    c.dose.DoseType = "RBE"
