"""Synthetic single-field electron case (12 MeV, 10x10 applicator, cerrobend cutout) and defects."""

from __future__ import annotations

from pydicom.dataset import Dataset

from .common import SyntheticCase, beam_by_number, circle, defect
from .photon import build_dose, build_structure_set, fraction_group, plan_header

ELECTRON_RX_GY = 50.0
ELECTRON_FRACTIONS = 25


def build_electron_beam(number: int, name: str) -> Dataset:
    beam = Dataset()
    beam.Manufacturer = "RT-Plan Guardian synthetic generator"
    beam.TreatmentMachineName = "LINAC1"
    beam.BeamNumber = number
    beam.BeamName = name
    beam.BeamType = "STATIC"
    beam.RadiationType = "ELECTRON"
    beam.TreatmentDeliveryType = "TREATMENT"
    beam.NumberOfWedges = 0
    beam.NumberOfCompensators = 0
    beam.NumberOfBoli = 0
    beam.FinalCumulativeMetersetWeight = 1.0
    beam.NumberOfControlPoints = 2
    beam.PrimaryDosimeterUnit = "MU"
    beam.SourceAxisDistance = 1000.0

    devices = []
    for kind in ("ASYMX", "ASYMY"):
        d = Dataset()
        d.RTBeamLimitingDeviceType = kind
        d.NumberOfLeafJawPairs = 1
        devices.append(d)
    beam.BeamLimitingDeviceSequence = devices

    app = Dataset()
    app.ApplicatorID = "A10"
    app.ApplicatorType = "ELECTRON_SQUARE"
    app.ApplicatorDescription = "10x10 cm"
    beam.ApplicatorSequence = [app]
    beam.NumberOfBlocks = 1
    block = Dataset()
    block.BlockNumber = 1
    block.BlockType = "APERTURE"
    block.BlockDivergence = "PRESENT"
    block.BlockMountingPosition = "SOURCE_SIDE"
    block.BlockTrayID = "T1"
    block.SourceToBlockTrayDistance = 500.0
    block.MaterialID = "Cerrobend"
    block.BlockThickness = 20.0
    block.BlockTransmission = 0.0
    outline = circle(0.0, 0.0, 40.0, n=12)
    block.BlockNumberOfPoints = len(outline)
    block.BlockData = [round(float(v), 2) for v in outline.ravel()]
    beam.BlockSequence = [block]

    def positions(kind: str) -> Dataset:
        p = Dataset()
        p.RTBeamLimitingDeviceType = kind
        p.LeafJawPositions = [-50.0, 50.0]
        return p

    cp0 = Dataset()
    cp0.ControlPointIndex = 0
    cp0.NominalBeamEnergy = 12.0
    cp0.DoseRateSet = 400.0
    cp0.BeamLimitingDevicePositionSequence = [positions("ASYMX"), positions("ASYMY")]
    cp0.GantryAngle = 0.0
    cp0.GantryRotationDirection = "NONE"
    cp0.BeamLimitingDeviceAngle = 0.0
    cp0.BeamLimitingDeviceRotationDirection = "NONE"
    cp0.PatientSupportAngle = 0.0
    cp0.PatientSupportRotationDirection = "NONE"
    cp0.TableTopEccentricAngle = 0.0
    cp0.IsocenterPosition = [0.0, 0.0, 0.0]
    cp0.SourceToSurfaceDistance = 1000.0
    cp0.CumulativeMetersetWeight = 0.0
    cp1 = Dataset()
    cp1.ControlPointIndex = 1
    cp1.CumulativeMetersetWeight = 1.0
    beam.ControlPointSequence = [cp0, cp1]
    return beam


def build_electron_plan(struct: Dataset, seed: int = 0) -> Dataset:
    ds = plan_header(struct, seed, label="SYNTH_ELECTRON")
    ds.RTPlanName = "Synthetic single-field electron plan"
    ds.DoseReferenceSequence[0].TargetPrescriptionDose = ELECTRON_RX_GY
    ds.FractionGroupSequence = [
        fraction_group([(1, 220.0)], ELECTRON_RX_GY / ELECTRON_FRACTIONS, ELECTRON_FRACTIONS)
    ]
    ds.BeamSequence = [build_electron_beam(1, "E12")]
    return ds


def make(seed: int = 0) -> SyntheticCase:
    struct = build_structure_set(seed)
    plan = build_electron_plan(struct, seed)
    dose = build_dose(plan, struct, seed, peak_gy=51.5)
    return SyntheticCase(plan=plan, struct=struct, dose=dose, seed=seed, technique="electron")


# --------------------------------------------------------------------------- defects
@defect(
    "electron_no_applicator",
    "Electron beam exported without an applicator",
    {"ELEC-001"},
    technique="electron",
)
def _(c: SyntheticCase) -> None:
    del beam_by_number(c, 1).ApplicatorSequence


@defect(
    "electron_applicator_type_undefined",
    "Applicator type is not a DICOM electron applicator",
    {"ELEC-001"},
    technique="electron",
)
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 1).ApplicatorSequence[0].ApplicatorType = "ELECTRON_HEX"


@defect(
    "electron_cone_not_in_inventory",
    "Applicator ID is not one of the cones the clinic owns",
    {"ELEC-001"},
    technique="electron",
    rules={"ELEC-001": {"params": {"allowed_applicator_ids": ["A6", "A15", "A20"]}}},
)
def _(c: SyntheticCase) -> None:
    pass  # clean case uses A10, which the override above does not list


@defect(
    "electron_cutout_missing",
    "No cutout (aperture block) defined for the electron field",
    {"ELEC-002"},
    technique="electron",
)
def _(c: SyntheticCase) -> None:
    beam = beam_by_number(c, 1)
    del beam.BlockSequence
    beam.NumberOfBlocks = 0


@defect(
    "electron_cutout_point_count_mismatch",
    "Cutout declares 99 points but holds 12",
    {"ELEC-002"},
    technique="electron",
)
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 1).BlockSequence[0].BlockNumberOfPoints = 99


@defect("electron_ssd_out_of_range", "SSD of 1500 mm", {"ELEC-003"}, technique="electron")
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 1).ControlPointSequence[0].SourceToSurfaceDistance = 1500.0


@defect(
    "electron_ssd_missing",
    "SSD missing on the first control point",
    {"ELEC-003"},
    technique="electron",
)
def _(c: SyntheticCase) -> None:
    del beam_by_number(c, 1).ControlPointSequence[0].SourceToSurfaceDistance


@defect(
    "electron_energy_not_commissioned",
    "14 MeV beam on a machine commissioned for 6, 9, 12, 16, 20 MeV",
    {"BEAM-004"},
    technique="electron",
    rules={"BEAM-004": {"params": {"allowed_energies": {"ELECTRON": [6, 9, 12, 16, 20]}}}},
)
def _(c: SyntheticCase) -> None:
    beam_by_number(c, 1).ControlPointSequence[0].NominalBeamEnergy = 14.0
