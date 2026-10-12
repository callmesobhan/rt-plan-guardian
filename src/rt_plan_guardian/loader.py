"""Load DICOM-RT files into reviewable cases.

Files are classified by SOP Class UID (not by file name or the Modality tag, which are easy to get
wrong). Non-RT objects such as CT slices are listed as ignored so the report shows exactly what was
and was not reviewed.

Two loading modes, deliberately different:

* **Exactly one of each kind supplied** -> one case, taken at face value. Objects are NOT matched by
  UID, so a dose that belongs to another plan is *reported* (LINK-002), never silently repaired.
* **More than one of any kind supplied** (a folder holding several plans/doses/structure sets, as
  real exports often do) -> objects are assembled into cases by following the UID references the
  files themselves carry (plan -> structure set, dose -> plan). Anything that cannot be linked is
  surfaced as *unassigned* rather than guessed at.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError

from .context import KINDS, IgnoredFile, LoadedFile, PlanContext
from .errors import LoadError

SOP_CLASS_KINDS = {
    "1.2.840.10008.5.1.4.1.1.481.5": "rtplan",  # RT Plan (photon, electron, brachytherapy)
    "1.2.840.10008.5.1.4.1.1.481.8": "rtplan",  # RT Ion Plan (proton / ion)
    "1.2.840.10008.5.1.4.1.1.481.3": "rtstruct",
    "1.2.840.10008.5.1.4.1.1.481.2": "rtdose",
}

# Recognised but out of scope: reported as ignored with a reason instead of silently dropped.
UNSUPPORTED_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.481.4": "RT Beams Treatment Record",
    "1.2.840.10008.5.1.4.1.1.481.6": "RT Brachy Treatment Record",
    "1.2.840.10008.5.1.4.1.1.481.9": "RT Ion Beams Treatment Record",
    "1.2.840.10008.5.1.4.1.1.481.7": "RT Treatment Summary Record",
    "1.2.840.10008.5.1.4.1.1.481.1": "RT Image",
}

_NOT_DICOM = "not a readable DICOM file"
_READ_ERRORS = (InvalidDicomError, EOFError, ValueError, OSError)


@dataclass(frozen=True)
class CaseSet:
    """The outcome of loading: one or more cases plus whatever could not be assigned to one."""

    cases: tuple[PlanContext, ...]
    unassigned: tuple[IgnoredFile, ...] = ()
    ignored: tuple[IgnoredFile, ...] = ()


def _sop_class(ds: Dataset) -> str:
    uid = ds.get("SOPClassUID")
    if uid is None and getattr(ds, "file_meta", None) is not None:
        uid = ds.file_meta.get("MediaStorageSOPClassUID")
    return str(uid or "")


def _classify(ds: Dataset) -> tuple[str | None, str]:
    """Return (kind, reason-if-ignored)."""
    sop_class = _sop_class(ds)
    if sop_class in SOP_CLASS_KINDS:
        return SOP_CLASS_KINDS[sop_class], ""
    if sop_class in UNSUPPORTED_SOP_CLASSES:
        return None, f"unsupported object: {UNSUPPORTED_SOP_CLASSES[sop_class]}"
    modality = str(ds.get("Modality", "") or "unknown")
    return None, f"not an RT plan/structure set/dose object (modality {modality})"


def loaded_file_from_bytes(name: str, raw: bytes) -> tuple[LoadedFile | None, IgnoredFile | None]:
    """Parse one file's bytes. Returns (file, None) for RT objects, (None, ignored) otherwise."""
    try:
        ds = pydicom.dcmread(io.BytesIO(raw))
    except _READ_ERRORS:
        return None, IgnoredFile(name, _NOT_DICOM)
    kind, reason = _classify(ds)
    if kind is None:
        return None, IgnoredFile(name, reason)
    return LoadedFile(
        name=name, kind=kind, sha256=hashlib.sha256(raw).hexdigest(), dataset=ds
    ), None


def loaded_file_from_dataset(name: str, ds: Dataset) -> LoadedFile:
    """Serialise and re-read a dataset so tests/demos exercise exactly what is on disk."""
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    loaded, ignored = loaded_file_from_bytes(name, buf.getvalue())
    if loaded is None:
        raise LoadError(f"{name}: {ignored.reason if ignored else 'unrecognised object'}")
    return loaded


# --------------------------------------------------------------------------- assembly
def _referenced(ds: Dataset, sequence: str) -> set[str]:
    return {str(i.get("ReferencedSOPInstanceUID", "")) for i in ds.get(sequence, [])}


def assemble(files: list[LoadedFile], ignored: list[IgnoredFile]) -> CaseSet:
    """Group RT objects into cases (see module docstring for the two modes)."""
    by_kind = {k: [f for f in files if f.kind == k] for k in KINDS}
    if all(len(v) <= 1 for v in by_kind.values()):
        return CaseSet(
            (PlanContext(files=tuple(files), ignored=tuple(ignored)),), (), tuple(ignored)
        )

    plans = sorted(by_kind["rtplan"], key=lambda f: f.sop_instance_uid)
    if not plans:
        counts = ", ".join(f"{len(v)} {k.upper()}" for k, v in by_kind.items() if v)
        raise LoadError(
            f"Cannot assemble cases from {counts}: no RTPLAN was supplied to link the other objects to."
        )
    structs = {f.sop_instance_uid: f for f in by_kind["rtstruct"]}
    used: set[str] = set()
    cases: list[tuple[PlanContext, list[LoadedFile]]] = []
    for plan in plans:
        members = [plan]
        for uid in sorted(_referenced(plan.dataset, "ReferencedStructureSetSequence")):
            if uid in structs:
                members.append(structs[uid])
        members += [
            d
            for d in sorted(by_kind["rtdose"], key=lambda f: f.sop_instance_uid)
            if plan.sop_instance_uid in _referenced(d.dataset, "ReferencedRTPlanSequence")
        ]
        used.update(m.sop_instance_uid + m.kind for m in members)
        cases.append((PlanContext(files=tuple(members), ignored=tuple(ignored)), members))

    unassigned: list[IgnoredFile] = []
    for f in files:
        if f.sop_instance_uid + f.kind in used:
            continue
        if f.kind == "rtdose":
            refs = ", ".join(
                sorted(u[-8:] for u in _referenced(f.dataset, "ReferencedRTPlanSequence"))
            )
            why = f"RTDOSE references RTPLAN(s) ...{refs or '(none)'} that were not supplied"
        elif f.kind == "rtstruct":
            why = "RTSTRUCT is not referenced by any supplied RTPLAN"
        else:  # pragma: no cover - every plan forms a case
            why = "not linked to any case"
        unassigned.append(IgnoredFile(f.name, why))
    final = tuple(
        PlanContext(files=ctx.files, ignored=ctx.ignored, unassigned=tuple(unassigned))
        for ctx, _ in cases
    )
    return CaseSet(final, tuple(unassigned), tuple(ignored))


# --------------------------------------------------------------------------- entry points
def load_streams_cases(items: Iterable[tuple[str, bytes]]) -> CaseSet:
    """Load from in-memory bytes (used by the Streamlit app so nothing touches disk)."""
    files: list[LoadedFile] = []
    ignored: list[IgnoredFile] = []
    for name, raw in items:
        loaded, skip = loaded_file_from_bytes(name, raw)
        if loaded:
            files.append(loaded)
        elif skip:
            ignored.append(skip)
    return assemble(files, ignored)


def _peek(path: Path) -> tuple[str | None, str]:
    """Header-only classification so large CT series are never fully read."""
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True)
    except _READ_ERRORS:
        return None, _NOT_DICOM
    return _classify(ds)


def load_cases(paths: Iterable[str | Path]) -> CaseSet:
    """Load files and/or directories (scanned recursively) into one or more cases."""
    files: list[LoadedFile] = []
    ignored: list[IgnoredFile] = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            raise LoadError(f"Path does not exist: {path}")
        if path.is_dir():
            for candidate in sorted(c for c in path.rglob("*") if c.is_file()):
                kind, reason = _peek(candidate)
                if kind is None:
                    ignored.append(IgnoredFile(candidate.name, reason))
                    continue
                loaded, _ = loaded_file_from_bytes(candidate.name, candidate.read_bytes())
                if loaded:
                    files.append(loaded)
        else:
            loaded, skip = loaded_file_from_bytes(path.name, path.read_bytes())
            if loaded:
                files.append(loaded)
            elif skip and skip.reason == _NOT_DICOM:
                raise LoadError(f"{path}: {_NOT_DICOM}")
            elif skip:
                ignored.append(skip)
    return assemble(files, ignored)


def _single(case_set: CaseSet) -> PlanContext:
    if len(case_set.cases) != 1:
        raise LoadError(f"Expected one case, found {len(case_set.cases)}; use load_cases()")
    return case_set.cases[0]


def load_paths(paths: Iterable[str | Path]) -> PlanContext:
    """Strict single-case loader (raises if the input holds several cases)."""
    return _single(load_cases(paths))


def load_streams(items: Iterable[tuple[str, bytes]]) -> PlanContext:
    return _single(load_streams_cases(items))


def group_ignored(ignored: Iterable[IgnoredFile]) -> list[tuple[str, list[str]]]:
    """[(reason, [file names])] so 80 CT slices print as one line, not 80."""
    groups: dict[str, list[str]] = {}
    for item in ignored:
        groups.setdefault(item.reason, []).append(item.name)
    return sorted(groups.items(), key=lambda kv: -len(kv[1]))
