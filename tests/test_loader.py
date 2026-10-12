import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from rt_plan_guardian.errors import LoadError
from rt_plan_guardian.loader import load_cases, load_paths, load_streams
from rt_plan_guardian.synthetic import make_case


def _other_object(path, sop_class, modality):
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = sop_class
    ds.file_meta.MediaStorageSOPInstanceUID = "1.2.3.4"
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID, ds.SOPInstanceUID, ds.Modality = sop_class, "1.2.3.4", modality
    ds.save_as(path, enforce_file_format=True)


def test_directory_scan_loads_rt_objects_and_reports_ignored(tmp_path):
    make_case().write(tmp_path)
    (tmp_path / "notes.txt").write_text("not dicom")
    _other_object(tmp_path / "CT.001.dcm", "1.2.840.10008.5.1.4.1.1.2", "CT")
    _other_object(tmp_path / "RTR.dcm", "1.2.840.10008.5.1.4.1.1.481.4", "RTRECORD")
    ctx = load_paths([tmp_path])
    assert sorted(f.kind for f in ctx.files) == ["rtdose", "rtplan", "rtstruct"]
    reasons = {i.name: i.reason for i in ctx.ignored}
    assert "not a readable DICOM file" in reasons["notes.txt"]
    assert "CT" in reasons["CT.001.dcm"]
    assert "Treatment Record" in reasons["RTR.dcm"]


def test_a_second_plan_in_the_folder_becomes_its_own_case(tmp_path):
    make_case().write(tmp_path)
    _other_object(tmp_path / "RP.ion.dcm", "1.2.840.10008.5.1.4.1.1.481.8", "RTPLAN")
    case_set = load_cases([tmp_path])
    assert len(case_set.cases) == 2  # RT Ion Plan is a plan, not an ignored object
    kinds = sorted(tuple(sorted(f.kind for f in c.files)) for c in case_set.cases)
    assert kinds == [("rtdose", "rtplan", "rtstruct"), ("rtplan",)]


def test_files_are_hashed(tmp_path):
    paths = make_case().write(tmp_path)
    ctx = load_paths(paths)
    assert all(len(f.sha256) == 64 for f in ctx.files)
    assert ctx.files[0].sha256 != ctx.files[1].sha256


def test_only_base_names_are_recorded(tmp_path):
    nested = tmp_path / "PATIENT^NAME" / "plan"
    make_case().write(nested)
    ctx = load_paths([tmp_path])
    assert all("PATIENT" not in f.name for f in ctx.files)


def test_two_cases_in_one_directory_are_assembled_by_uid(tmp_path):
    make_case().write(tmp_path / "a")
    make_case(seed=1).write(tmp_path / "b")
    case_set = load_cases([tmp_path])
    assert len(case_set.cases) == 2
    for ctx in case_set.cases:  # every case got its OWN structure set and dose
        plan, struct, dose = ctx.plan, ctx.structure_set, ctx.dose
        assert (
            plan.ReferencedStructureSetSequence[0].ReferencedSOPInstanceUID == struct.SOPInstanceUID
        )
        assert dose.ReferencedRTPlanSequence[0].ReferencedSOPInstanceUID == plan.SOPInstanceUID
    assert case_set.unassigned == ()


def test_strict_single_case_loader_refuses_several(tmp_path):
    make_case().write(tmp_path / "a")
    make_case(seed=1).write(tmp_path / "b")
    with pytest.raises(LoadError, match="Expected one case"):
        load_paths([tmp_path])


def test_unlinked_objects_are_reported_not_guessed(tmp_path):
    a = make_case().write(tmp_path / "a")
    b = make_case(seed=1).write(tmp_path / "b")
    # plan A + its struct, plan B's dose: the dose belongs to a plan that was not supplied
    case_set = load_cases([a[0], a[1], b[2], b[0], a[2]])
    assert len(case_set.cases) == 2
    case_set = load_cases([a[0], a[1], a[2], b[2]])  # extra dose for plan B, plan B absent
    assert [i.name for i in case_set.unassigned] == ["RD.SYNTH.dcm"]
    assert "not supplied" in case_set.unassigned[0].reason
    assert case_set.cases[0].unassigned == case_set.unassigned


def test_multiple_doses_for_one_plan_choose_the_plan_level_one(tmp_path):
    case = make_case()
    paths = case.write(tmp_path)
    beam_dose = case.dose
    beam_dose.SOPInstanceUID = "1.2.3.4.5"
    beam_dose.DoseSummationType = "BEAM"
    beam_dose.save_as(tmp_path / "RD.beam.dcm", enforce_file_format=True)
    ctx = load_cases([tmp_path]).cases[0]
    assert len(ctx.get_all("rtdose")) == 2
    assert ctx.dose.DoseSummationType == "PLAN" and len(ctx.extra_doses) == 1
    assert paths


def test_ion_plan_is_loaded_as_a_plan(tmp_path):
    from rt_plan_guardian.synthetic import make_case as mk

    mk(technique="proton").write(tmp_path)
    ctx = load_paths([tmp_path])
    assert ctx.has("rtplan") and ctx.techniques == frozenset({"proton"})


def test_grouping_collapses_ignored_files():
    from rt_plan_guardian.context import IgnoredFile
    from rt_plan_guardian.loader import group_ignored

    grouped = group_ignored(
        [IgnoredFile(f"CT{i}.dcm", "CT") for i in range(80)] + [IgnoredFile("x.txt", "other")]
    )
    assert grouped[0][0] == "CT" and len(grouped[0][1]) == 80 and len(grouped) == 2


def test_explicit_non_dicom_file_is_an_error(tmp_path):
    f = tmp_path / "x.dcm"
    f.write_text("hello")
    with pytest.raises(LoadError, match="not a readable DICOM"):
        load_paths([f])


def test_missing_path_is_an_error(tmp_path):
    with pytest.raises(LoadError, match="does not exist"):
        load_paths([tmp_path / "nope"])


def test_load_streams_needs_no_disk(tmp_path):
    paths = make_case().write(tmp_path)
    ctx = load_streams((p.name, p.read_bytes()) for p in paths)
    assert ctx.has("rtplan") and ctx.has("rtstruct") and ctx.has("rtdose")
    assert (
        load_streams([("junk.bin", b"\x00\x01")]).ignored[0].reason == "not a readable DICOM file"
    )


def test_classification_uses_sop_class_not_filename(tmp_path):
    paths = make_case().write(tmp_path)
    renamed = {"x1.dcm": paths[0], "x2.dcm": paths[1], "x3.dcm": paths[2]}
    ctx = load_streams((n, p.read_bytes()) for n, p in renamed.items())
    assert {f.kind: f.name for f in ctx.files} == {
        "rtplan": "x1.dcm",
        "rtstruct": "x2.dcm",
        "rtdose": "x3.dcm",
    }
