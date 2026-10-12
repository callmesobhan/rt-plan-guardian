"""Regression tests on real, open-access DICOM-RT data (opt-in).

    python scripts/fetch_open_data.py gamma_knife brachy_breast tg263 --dest open_data --yes
    RTPG_REAL_DATA=$PWD/open_data pytest -m realdata

Skipped automatically when RTPG_REAL_DATA is unset. The expectations below record what the tool
found on these files when they were evaluated (see docs/real-data-evaluation.md); several are
properties of the benchmark datasets, not defects of this tool, and are labelled as such. If a
number changes, a rule changed: decide whether the rule or the expectation is right.

Data: Vestibular-Schwannoma-SEG (TCIA, CC BY 4.0; Shapey et al., Sci Data 2021) and the
'Interstitial HDR breast' TG-186 test case (Zenodo 10.5281/zenodo.18496579, CC BY 4.0; Peppa et
al., Med Phys 2023).
"""

import os
from pathlib import Path

import pytest

from rt_plan_guardian.config import load_ruleset
from rt_plan_guardian.engine import run_review
from rt_plan_guardian.loader import load_cases
from rt_plan_guardian.models import Verdict

from ..conftest import ruleset_with

ROOT = Path(os.environ.get("RTPG_REAL_DATA", ""))
pytestmark = [
    pytest.mark.realdata,
    pytest.mark.skipif(not os.environ.get("RTPG_REAL_DATA"), reason="RTPG_REAL_DATA not set"),
]

PATIENTS = ["VS-SEG-001", "VS-SEG-002", "VS-SEG-003", "VS-SEG-004"]
BRACHY = {
    "varian_tps": "Breast_Varian/Reference TPS",
    "elekta_tps": "Breast_Elekta/Reference TPS",
    "varian_mc": "Breast_Varian/Reference MC",
    "elekta_mc": "Breast_Elekta/Reference MC",
}


def statuses(report):
    return {r.rule_id: r.status.value for r in report.results}


@pytest.fixture(scope="module")
def gamma_knife_reports():
    rs = load_ruleset()
    out = {}
    for pid in PATIENTS:
        case_set = load_cases([ROOT / "gamma_knife" / pid])
        assert len(case_set.cases) == 2 and case_set.unassigned == ()
        out[pid] = [run_review(ctx, rs) for ctx in case_set.cases]
    return out


@pytest.fixture(scope="module")
def brachy_reports():
    rs = load_ruleset()
    return {
        name: run_review(load_cases([ROOT / "brachy_breast" / sub]).cases[0], rs)
        for name, sub in BRACHY.items()
    }


# ----------------------------------------------------------------------------- Gamma Knife
def test_every_object_is_paired_with_its_own_partners_by_uid(gamma_knife_reports):
    for reports in gamma_knife_reports.values():
        for report in reports:
            s = statuses(report)
            assert s["LINK-001"] == s["LINK-002"] == s["LINK-003"] == "PASS"
            assert report.context.techniques == frozenset({"photon"})


def test_no_gamma_knife_plan_fails_or_crashes(gamma_knife_reports):
    for pid, reports in gamma_knife_reports.items():
        for report in reports:
            assert report.verdict is Verdict.PASS_WITH_WARNINGS, (pid, report.verdict)
            assert "ERROR" not in statuses(report).values()


def test_prescription_isodose_identity_holds_on_all_eight_plans(gamma_knife_reports):
    """sum(BeamDose) x isodose% == Rx, with the isodose read from the GammaPlan private element."""
    for reports in gamma_knife_reports.values():
        for report in reports:
            assert statuses(report)["PLAN-004"] == "PASS"
            note = next(r for r in report.results if r.rule_id == "PLAN-004").checks[0].note
            assert "ELEKTA GAMMAPLAN" in note


def test_the_isodose_reading_matters_without_it_plan_004_fails():
    rs = ruleset_with({"PLAN-004": {"params": {"use_vendor_isodose": False}}})
    report = run_review(load_cases([ROOT / "gamma_knife" / "VS-SEG-003"]).cases[0], rs)
    assert statuses(report)["PLAN-004"] == "FAIL"  # 26 Gy summed vs 13 Gy prescribed


def test_hotspot_is_judged_against_the_100_percent_level(gamma_knife_reports):
    for reports in gamma_knife_reports.values():
        for report in reports:
            assert statuses(report)["DOSE-004"] == "PASS"


def test_known_dataset_anomaly_vs_seg_001_second_dose_misses_its_target(gamma_knife_reports):
    """VS-SEG-001, plan ...470263: its RTDOSE peaks ~10.7 Gy, 40 mm from the shots, 0.01 Gy to the TV.
    This is a property of the published files (found by DOSE-006), not a tool defect."""
    bad = next(
        r
        for r in gamma_knife_reports["VS-SEG-001"]
        if r.context.plan.SOPInstanceUID.endswith("470263")
    )
    result = next(r for r in bad.results if r.rule_id == "DOSE-006")
    assert result.status.value == "FAIL" and "0.01 Gy" in result.checks[0].observed


def test_gamma_knife_vendor_quirks_are_warnings_not_blockers(gamma_knife_reports):
    for reports in gamma_knife_reports.values():
        for report in reports:
            s = statuses(report)
            assert s["BEAM-008"] == "FAIL"  # jaws exported with 0 leaf/jaw pairs (conformance)
            assert (
                report.results[[r.rule_id for r in report.results].index("BEAM-008")].severity.value
                == "warning"
            )
            assert s["BEAM-007"] == "PASS" and s["BEAM-005"] == "PASS"  # MINUTE-aware limits


# ----------------------------------------------------------------------------- brachytherapy
def test_brachy_plans_are_recognised_and_nothing_crashes(brachy_reports):
    for name, report in brachy_reports.items():
        assert report.context.techniques == frozenset({"brachy"}), name
        assert "ERROR" not in statuses(report).values(), name


def test_structural_brachy_rules_pass_on_both_vendors(brachy_reports):
    """TRAK identity, channel/dwell structure, step size and fraction scheme on real exports."""
    for name, report in brachy_reports.items():
        s = statuses(report)
        for rid in ("BRACHY-001", "BRACHY-002", "BRACHY-003", "BRACHY-005", "BRACHY-006"):
            assert s[rid] == "PASS", (name, rid)


def test_trak_agrees_to_better_than_a_tenth_of_a_percent(brachy_reports):
    for name, report in brachy_reports.items():
        for check in next(r for r in report.results if r.rule_id == "BRACHY-002").checks:
            deviation = float(check.note.split()[1].rstrip("%"))
            assert abs(deviation) < 0.1, (name, check.note)


def test_open_catheter_contours_are_not_flagged(brachy_reports):
    for name, report in brachy_reports.items():
        assert statuses(report)["STRUCT-004"] == "PASS", name


def test_source_reference_dates_are_flagged_as_stale(brachy_reports):
    """Benchmark datasets reuse a fixed source strength: 322 d (Varian) / ~1200 d (Elekta) old."""
    for name, report in brachy_reports.items():
        assert statuses(report)["BRACHY-004"] == "FAIL", name


def test_dataset_properties_found_by_cross_checks(brachy_reports):
    assert (
        statuses(brachy_reports["elekta_tps"])["PLAN-004"] == "SKIPPED"
    )  # plan carries no prescription
    mc = statuses(brachy_reports["varian_mc"])
    assert (
        mc["LINK-002"] == "FAIL"
    )  # MC dose references the original TPS plan UID, not the supplied MC plan
    assert mc["PLAN-004"] == "FAIL"  # MC plan states Rx 1 Gy but 4.3 Gy per setup
    assert brachy_reports["varian_tps"].verdict is Verdict.PASS_WITH_WARNINGS


def test_tg263_names_on_a_real_structure_set(tmp_path):
    xlrd = pytest.importorskip("xlrd")  # noqa: F841
    xls = ROOT / "tg263" / "TG263_Nomenclature_Worksheet_20170815.xls"
    if not xls.is_file():
        pytest.skip("TG-263 worksheet not fetched")
    import subprocess
    import sys

    names = tmp_path / "tg263_names.txt"
    script = Path(__file__).resolve().parents[2] / "scripts" / "tg263_xls_to_txt.py"
    subprocess.run(
        [sys.executable, str(script), str(xls), str(names)], check=True, capture_output=True
    )
    rs = ruleset_with({"STRUCT-006": {"params": {"nomenclature_file": str(names)}}})
    report = run_review(load_cases([ROOT / "gamma_knife" / "VS-SEG-001"]).cases[0], rs)
    result = next(r for r in report.results if r.rule_id == "STRUCT-006")
    bad = {c.subject.split("'")[1] for c in result.checks if not c.ok}
    assert {
        "TV",
        "Vol2016",
        "*Skull",
    } <= bad  # not TG-263 names; Cochlea alone lacks its _L/_R suffix
