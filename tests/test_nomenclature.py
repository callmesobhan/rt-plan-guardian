"""STRUCT-006: standard structure names (AAPM TG-263 list supplied by the user)."""

import pytest

from rt_plan_guardian.config import load_ruleset
from rt_plan_guardian.engine import run_review
from rt_plan_guardian.rules.structure import NOMENCLATURE_ENV, load_name_list

from .conftest import failed_ids, ruleset_with, status_of


def rs(path="", **params):
    return ruleset_with({"STRUCT-006": {"params": {"nomenclature_file": str(path), **params}}})


@pytest.fixture
def names_file(tmp_path):
    f = tmp_path / "names.txt"
    f.write_text("# test list\nPTV\nSpinalCord  # trailing comment\nBODY,ignored second column\n")
    return f


def test_name_list_parsing(names_file):
    assert load_name_list(names_file) == ["PTV", "SpinalCord", "BODY"]


def test_conforming_names_pass(case, names_file):
    assert status_of(run_review(case.to_context(), rs(names_file)), "STRUCT-006") == "PASS"


def test_non_listed_name_fails(case, names_file):
    case.struct.StructureSetROISequence[2].ROIName = "Cord"
    report = run_review(case.to_context(), rs(names_file))
    bad = [
        c for c in next(r for r in report.results if r.rule_id == "STRUCT-006").checks if not c.ok
    ]
    assert failed_ids(report) == {"STRUCT-006"} and "Cord" in bad[0].subject
    assert report.verdict.value == "PASS_WITH_WARNINGS"  # naming is a warning by default


def test_capitalisation_is_strict_by_default(case, names_file):
    case.struct.StructureSetROISequence[2].ROIName = "spinalcord"
    assert failed_ids(run_review(case.to_context(), rs(names_file))) == {"STRUCT-006"}
    assert failed_ids(run_review(case.to_context(), rs(names_file, case_sensitive=False))) == set()


def test_length_limit(case, names_file):
    case.struct.StructureSetROISequence[2].ROIName = "SpinalCord"
    assert failed_ids(run_review(case.to_context(), rs(names_file, max_name_length=5))) == {
        "STRUCT-006"
    }


def test_additional_patterns_allow_targets_and_planning_structures(case, names_file):
    case.struct.StructureSetROISequence[0].ROIName = "PTV_5400"
    assert failed_ids(run_review(case.to_context(), rs(names_file))) == {"STRUCT-006"}
    ok = rs(names_file, additional_allowed_patterns=["^PTV_[0-9]+$"])
    assert failed_ids(run_review(case.to_context(), ok)) == set()


def test_not_applicable_without_a_list(case, monkeypatch):
    monkeypatch.delenv(NOMENCLATURE_ENV, raising=False)
    assert status_of(run_review(case.to_context(), rs()), "STRUCT-006") == "NOT_APPLICABLE"


def test_environment_variable_supplies_the_list(case, names_file, monkeypatch):
    monkeypatch.setenv(NOMENCLATURE_ENV, str(names_file))
    assert status_of(run_review(case.to_context(), rs()), "STRUCT-006") == "PASS"


def test_missing_or_empty_list_is_skipped_not_passed(case, tmp_path):
    assert (
        status_of(run_review(case.to_context(), rs(tmp_path / "nope.txt")), "STRUCT-006")
        == "SKIPPED"
    )
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n")
    assert status_of(run_review(case.to_context(), rs(empty)), "STRUCT-006") == "SKIPPED"


def test_shipped_tg263_ruleset_loads_and_targets_the_rule():
    r = load_ruleset("tg263_naming")
    p = r.settings["STRUCT-006"].params
    assert p["max_name_length"] == 16 and p["additional_allowed_patterns"]
