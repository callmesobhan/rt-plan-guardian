"""Tests for the vendor-neutral AAPM TG-275 adversarial mutation & commissioning harness."""

from __future__ import annotations

import csv
import json
import shlex
import sys
from pathlib import Path

import pydicom

from rt_plan_guardian.cli import main
from rt_plan_guardian.commissioning import (
    MUTATION_CATALOG,
    generate_commissioning_suite,
    load_external_results,
    render_commissioning_html,
    render_commissioning_markdown,
    run_command_adapter_on_suite,
    run_rtpg_on_suite,
    score_commissioning_suite,
    wilson_ci,
    write_commissioning_reports,
)
from rt_plan_guardian.synthetic import make_case


def test_wilson_ci_bounds_and_extremes():
    lo, hi = wilson_ci(10, 10)
    assert 0.7 < lo < 1.0 and hi == 1.0
    lo0, hi0 = wilson_ci(0, 10)
    assert lo0 == 0.0 and 0.0 < hi0 < 0.35
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_generate_from_external_clinical_dicom_export_remints_uids_and_anonymizes(tmp_path: Path):
    # Export a realistic clinical-style photon plan with non-synthetic patient identifiers
    src_dir = tmp_path / "clinic_export"
    sc = make_case(seed=42, technique="photon")
    assert sc.plan is not None and sc.struct is not None and sc.dose is not None
    orig_plan_uid = str(sc.plan.SOPInstanceUID)
    for ds in (sc.plan, sc.struct, sc.dose):
        ds.PatientID = "REAL-PATIENT-12345"
        ds.PatientName = "DOE^JANE"
        ds.InstitutionName = "Memorial Clinical Hospital"
    sc.write(src_dir)

    suite_dir = tmp_path / "suite"
    manifest = generate_commissioning_suite(
        suite_dir,
        source_paths=[src_dir],
        profile="all",
        anonymize=True,
        blind=True,
        seed=7,
    )

    assert manifest["source_mode"] == "clinical_dicom"
    assert manifest["blinded"] is True
    assert manifest["summary"]["negative_controls"] == 2
    assert manifest["summary"]["positive_defects"] >= 18
    # Modality-specific mutations (electron/ion/brachy) should be cleanly skipped as inapplicable
    assert manifest["summary"]["skipped_inapplicable"] >= 7

    # Verify every mutant has a distinct SOPInstanceUID, zero PHI leakage, and clean UIDs
    seen_plan_uids: set[str] = set()
    for entry in manifest["cases"]:
        assert entry["case_id"].startswith("CASE_")
        rp = pydicom.dcmread(suite_dir / entry["relative_dir"] / "RP.dcm")
        uid = str(rp.SOPInstanceUID)
        assert uid != orig_plan_uid
        assert uid not in seen_plan_uids
        seen_plan_uids.add(uid)
        assert str(rp.PatientID).startswith("TG275-")
        assert "DOE" not in str(rp.PatientName)
        assert "InstitutionName" not in rp


def test_generate_all_modalities_covers_entire_catalog(tmp_path: Path):
    suite_dir = tmp_path / "multi_modal_suite"
    manifest = generate_commissioning_suite(suite_dir, technique="all", profile="all", seed=1)
    applied_ids = {c["mutation_id"] for c in manifest["cases"]}
    assert applied_ids == set(MUTATION_CATALOG)


def test_self_test_rtpg_catches_high_risk_and_surfaces_subtle_half_voxel_blind_spot(tmp_path: Path):
    suite_dir = tmp_path / "suite_photon"
    generate_commissioning_suite(suite_dir, technique="photon", profile="all", seed=3)

    observations = run_rtpg_on_suite(suite_dir)
    cert = score_commissioning_suite(
        suite_dir,
        observations,
        checker_name="RT-Plan Guardian",
        checker_version="0.2.0",
    )

    # RT-Plan Guardian must catch 100% of high-risk TG-275 defects and raise 0 false alarms
    assert cert["metrics"]["high_risk_recall"]["rate"] == 1.0
    assert cert["metrics"]["specificity"]["rate"] == 1.0
    assert cert["false_alarms"] == []
    # And honestly expose the deliberate sub-voxel blind spot (half-voxel RTDOSE origin shift)
    blind_ids = {b["mutation_id"] for b in cert["blind_spots"]}
    assert blind_ids == {"subtle_dose_grid_half_voxel_shift"}
    assert cert["verdict"] == "PASS_WITH_WARNINGS"

    written = write_commissioning_reports(cert, tmp_path / "cert_out")
    assert len(written) == 3
    md = render_commissioning_markdown(cert)
    html = render_commissioning_html(cert)
    assert "subtle_dose_grid_half_voxel_shift" in md
    assert "AAPM TG-275 Plan-Checker Commissioning Certificate" in html
    assert cert["content_sha256"] in html


def test_external_csv_scoring_catches_missed_high_risk_and_false_positive(tmp_path: Path):
    suite_dir = tmp_path / "suite_ext"
    manifest = generate_commissioning_suite(
        suite_dir,
        technique="photon",
        mutation_ids=[
            "control_clean_clone",
            "wrong_patient_id",
            "stale_dose_plan_uid",
            "subtle_mlc_leaf_collision_1mm",
        ],
    )
    csv_path = suite_dir / "results_filled.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "detected", "severity", "checker_message"])
        for c in manifest["cases"]:
            mid = c["mutation_id"]
            if mid == "control_clean_clone":
                w.writerow([c["case_id"], "true", "WARN", "spurious false positive"])
            elif mid == "subtle_mlc_leaf_collision_1mm":
                w.writerow([c["case_id"], "false", "PASS", "missed 1mm overlap"])
            else:
                w.writerow([c["case_id"], "true", "FAIL", "caught"])

    obs = load_external_results(csv_path)
    cert = score_commissioning_suite(
        suite_dir, obs, checker_name="VendorChecker Pro", checker_version="3.1"
    )
    assert cert["verdict"] == "FAIL"
    assert cert["confusion_matrix"] == {"tp": 2, "fn": 1, "tn": 0, "fp": 1}
    assert cert["blind_spots"][0]["mutation_id"] == "subtle_mlc_leaf_collision_1mm"
    assert cert["false_alarms"][0]["mutation_id"] == "control_clean_clone"


def test_runner_cmd_adapter_and_cli_roundtrip(tmp_path: Path, capsys):
    assert main(["commission", "list"]) == 0
    assert "subtle_mlc_leaf_collision_1mm" in capsys.readouterr().out

    suite_dir = tmp_path / "cli_suite"
    rc_gen = main(
        [
            "commission",
            "generate",
            "--out",
            str(suite_dir),
            "--technique",
            "brachy",
            "--mutations",
            "control_clean_clone,brachy_trak_drift_10pct,brachy_source_decay_stale_1yr",
        ]
    )
    assert rc_gen == 0

    # Score using --self-test via CLI
    cert_dir = tmp_path / "cli_certs"
    rc_score = main(
        [
            "commission",
            "score",
            str(suite_dir),
            "--self-test",
            "--out",
            str(cert_dir),
        ]
    )
    assert rc_score == 0
    cert = json.loads((cert_dir / "tg275_commissioning_certificate.json").read_text())
    assert cert["verdict"] == "PASS"
    assert cert["metrics"]["sensitivity"]["rate"] == 1.0
    assert cert["metrics"]["specificity"]["rate"] == 1.0

    # Score via external CLI runner command adapter
    cmd = f"{shlex.quote(sys.executable)} -m rt_plan_guardian.cli check {{case_dir}} --fail-on warning"
    obs = run_command_adapter_on_suite(suite_dir, cmd)
    cert_cmd = score_commissioning_suite(suite_dir, obs, checker_name="CLI Adapter Test")
    assert cert_cmd["verdict"] == "PASS"
