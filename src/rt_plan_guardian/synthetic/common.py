"""Shared builders and the fault-injection registry for the synthetic DICOM-RT cases.

Why synthetic cases exist: the repo must be testable, demoable and shareable without a single real
patient file, and every rule must be *proven* to catch its failure mode. Each technique module
builds a small, internally consistent clean case; `DEFECTS` is a catalogue of realistic mistakes,
each of which mutates a clean case and declares exactly which rules must then fail.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import PYDICOM_IMPLEMENTATION_UID, UID, ExplicitVRLittleEndian, generate_uid

from ..context import LoadedFile, PlanContext
from ..loader import loaded_file_from_dataset

RT_PLAN = "1.2.840.10008.5.1.4.1.1.481.5"
RT_ION_PLAN = "1.2.840.10008.5.1.4.1.1.481.8"
RT_STRUCT = "1.2.840.10008.5.1.4.1.1.481.3"
RT_DOSE = "1.2.840.10008.5.1.4.1.1.481.2"

PATIENT_ID = "RTPG-0001"


def make_uid(seed: int, name: str) -> str:
    return str(generate_uid(entropy_srcs=["rt-plan-guardian", str(seed), name]))


def base_dataset(modality: str, sop_class: str, seed: int, label: str) -> Dataset:
    ds = Dataset()
    sop_uid = make_uid(seed, f"sop-{label}")
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = UID(sop_class)
    ds.file_meta.MediaStorageSOPInstanceUID = UID(sop_uid)
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_uid
    ds.Modality = modality
    ds.PatientName = "SYNTHETIC^TEST"
    ds.PatientID = PATIENT_ID
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "O"
    ds.PatientComments = "SYNTHETIC TEST DATA - NOT A REAL PATIENT"
    ds.StudyInstanceUID = make_uid(seed, "study")
    ds.StudyDate = "20260101"
    ds.StudyTime = "120000"
    ds.SeriesInstanceUID = make_uid(seed, f"series-{label}")
    ds.SeriesNumber = "1"
    ds.InstanceNumber = "1"
    ds.Manufacturer = "RT-Plan Guardian synthetic generator"
    return ds


def rounded(values) -> list[float]:
    return [round(float(v), 3) for v in values]


def circle(cx: float, cy: float, r: float, n: int = 24) -> np.ndarray:
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return np.column_stack([cx + r * np.cos(t), cy + r * np.sin(t)])


@dataclass
class SyntheticCase:
    plan: Dataset | None
    struct: Dataset | None
    dose: Dataset | None
    seed: int = 0
    technique: str = "photon"

    def copy(self) -> SyntheticCase:
        return copy.deepcopy(self)

    def _named(self) -> list[tuple[str, Dataset]]:
        pairs = (
            ("RP.SYNTH.dcm", self.plan),
            ("RS.SYNTH.dcm", self.struct),
            ("RD.SYNTH.dcm", self.dose),
        )
        return [(name, ds) for name, ds in pairs if ds is not None]

    def loaded_files(self) -> list[LoadedFile]:
        return [loaded_file_from_dataset(name, ds) for name, ds in self._named()]

    def to_context(self) -> PlanContext:
        return PlanContext(files=tuple(self.loaded_files()))

    def write(self, directory: str | Path) -> list[Path]:
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for name, ds in self._named():
            path = out / name
            ds.save_as(path, enforce_file_format=True)
            paths.append(path)
        return paths


# --------------------------------------------------------------------------- fault injection
@dataclass(frozen=True)
class Defect:
    name: str
    description: str
    apply: Callable[[SyntheticCase], None]
    #: Rules that must end FAIL (any severity) after the defect is applied, and no others.
    expected_failures: frozenset[str]
    #: Rules that may legitimately end SKIPPED as a consequence (cannot evaluate broken data).
    expected_skips: frozenset[str] = field(default_factory=frozenset)
    #: Which synthetic technique the defect is applied to.
    technique: str = "photon"
    #: Ruleset overrides ({rule_id: {"params": {...}, "severity": ...}}) layered on the default
    #: ruleset for rules that only act when configured (naming policy, dose-volume constraints).
    rules: dict = field(default_factory=dict)


DEFECTS: dict[str, Defect] = {}


def defect(
    name: str,
    description: str,
    fails: set[str],
    skips: set[str] | None = None,
    technique: str = "photon",
    rules: dict | None = None,
):
    def deco(fn: Callable[[SyntheticCase], None]) -> Callable[[SyntheticCase], None]:
        if name in DEFECTS:
            raise ValueError(f"duplicate defect {name!r}")
        DEFECTS[name] = Defect(
            name,
            description,
            fn,
            frozenset(fails),
            frozenset(skips or ()),
            technique,
            dict(rules or {}),
        )
        return fn

    return deco


def beam_by_number(case: SyntheticCase, number: int) -> Dataset:
    seq = case.plan.get("IonBeamSequence") or case.plan.BeamSequence
    return next(b for b in seq if int(b.BeamNumber) == number)


def control_points_of(beam: Dataset):
    return beam.get("IonControlPointSequence") or beam.ControlPointSequence


def other_uid(case: SyntheticCase, tag: str) -> str:
    return make_uid(case.seed, f"other-{tag}")
