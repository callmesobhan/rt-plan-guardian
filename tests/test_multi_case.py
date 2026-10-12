"""Several plans/doses/structure sets in one folder: assembly, unassigned objects, CLI output."""

import json

from rt_plan_guardian.cli import main
from rt_plan_guardian.engine import run_review
from rt_plan_guardian.loader import load_cases
from rt_plan_guardian.synthetic import make_case

from .conftest import failed_ids, ruleset_with, status_of


def test_unassigned_dose_is_reported_by_input_002(tmp_path):
    a = make_case().write(tmp_path / "a")
    b = make_case(seed=1).write(tmp_path / "b")
    case_set = load_cases([a[0], a[1], a[2], b[2]])  # plan B itself is not supplied
    report = run_review(
        case_set.cases[0].__class__(files=case_set.cases[0].files, unassigned=case_set.unassigned),
        ruleset_with({}),
    )
    assert failed_ids(report) == {"INPUT-002"}
    assert report.verdict.value == "PASS_WITH_WARNINGS"
    assert report.to_dict()["unassigned_files"][0]["filename"] == "RD.SYNTH.dcm"


def test_single_case_has_no_unassigned_objects(case, review):
    assert status_of(review(case), "INPUT-002") == "PASS"


def test_input_003_requires_one_plan_level_dose(tmp_path):
    case = make_case()
    case.write(tmp_path)
    extra = case.dose
    extra.SOPInstanceUID = "1.2.3.4.99"
    extra.DoseSummationType = "PLAN"  # a second plan-level dose: ambiguous
    extra.save_as(tmp_path / "RD.second.dcm", enforce_file_format=True)
    report = run_review(load_cases([tmp_path]).cases[0], ruleset_with({}))
    assert failed_ids(report) == {"INPUT-003"}


def test_input_003_not_applicable_for_one_dose(case, review):
    assert status_of(review(case), "INPUT-003") == "NOT_APPLICABLE"


def test_cli_reviews_each_case_and_writes_an_index(tmp_path, capsys):
    make_case().write(tmp_path / "in" / "a")
    bad = make_case(seed=1)
    bad.dose.PatientID = "WRONG"
    bad.write(tmp_path / "in" / "b")
    out = tmp_path / "out"
    assert main(["check", str(tmp_path / "in"), "--out", str(out), "--format", "json"]) == 1
    text = capsys.readouterr().out
    assert "Assembled 2 cases" in text and "Summary:" in text
    index = json.loads((out / "index.json").read_text())
    assert sorted(i["verdict"] for i in index) == ["FAIL", "PASS"]
    assert all((out / i["case_id"] / "rtpg_report.json").is_file() for i in index)


def test_cli_groups_ignored_files(tmp_path, capsys):
    from .test_loader import _other_object

    make_case().write(tmp_path)
    for i in range(5):
        _other_object(tmp_path / f"CT.{i}.dcm", "1.2.840.10008.5.1.4.1.1.2", "CT")
    assert main(["check", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "ignored   5 file(s)" in out and out.count("ignored") == 1


def test_cli_demo_and_check_every_technique(tmp_path, capsys):
    for technique in ("photon", "electron", "proton", "brachy"):
        target = tmp_path / technique
        assert main(["demo", str(target), "--technique", technique]) == 0
        assert main(["check", str(target)]) == 0
        assert f"technique: {technique}" in capsys.readouterr().out


def test_cli_rulesets_listing(capsys):
    assert main(["rulesets"]) == 0
    out = capsys.readouterr().out
    assert all(
        name in out for name in ("default", "aapm_mppg11a", "hytec_brain_srs", "tg263_naming")
    )


def test_cli_accepts_a_builtin_ruleset_name(tmp_path):
    make_case().write(tmp_path)
    assert main(["check", str(tmp_path), "--config", "aapm_mppg11a"]) == 0
