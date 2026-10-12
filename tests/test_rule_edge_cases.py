"""Behaviour at the edges of individual rules: parameters, unsupported data, fallbacks."""

import copy

from rt_plan_guardian.config import parse_ruleset
from rt_plan_guardian.engine import run_review

from .conftest import failed_ids, status_of


def run(case, rules=None):
    return run_review(case.to_context(), parse_ruleset({"rules": rules or {}}))


# ---- parameters tighten rules
def test_machine_whitelist(case):
    assert (
        status_of(run(case, {"PLAN-005": {"params": {"allowed_machines": ["LINAC1"]}}}), "PLAN-005")
        == "PASS"
    )
    report = run(case, {"PLAN-005": {"params": {"allowed_machines": ["TRUEBEAM9"]}}})
    assert failed_ids(report) == {"PLAN-005"}


def test_energy_whitelist(case):
    assert (
        failed_ids(run(case, {"BEAM-004": {"params": {"allowed_energies": {"PHOTON": [6]}}}}))
        == set()
    )
    assert failed_ids(
        run(case, {"BEAM-004": {"params": {"allowed_energies": {"PHOTON": [10, 15]}}}})
    ) == {"BEAM-004"}


def test_mu_limits_are_configurable(case):
    assert failed_ids(run(case, {"BEAM-005": {"params": {"limits": {"MU": [1.0, 100.0]}}}})) == {
        "BEAM-005"
    }


def test_hotspot_threshold_is_configurable(case):
    assert failed_ids(run(case, {"DOSE-004": {"params": {"max_percent_of_rx": 100.0}}})) == {
        "DOSE-004"
    }


def test_required_roi_patterns_are_case_insensitive_regex(case):
    ok = run(case, {"STRUCT-002": {"params": {"required_roi_patterns": ["^ptv$", "spinal.*"]}}})
    assert status_of(ok, "STRUCT-002") == "PASS"
    bad = run(case, {"STRUCT-002": {"params": {"required_roi_patterns": ["^Lung_L$"]}}})
    assert failed_ids(bad) == {"STRUCT-002"}


def test_empty_roi_ignore_pattern_suppresses_warning(case):
    del case.struct.ROIContourSequence[2].ContourSequence
    assert failed_ids(run(case, {"STRUCT-003": {"params": {"ignore_patterns": ["cord"]}}})) == set()


# ---- unsupported data is SKIPPED, never silently passed or wrongly failed
def test_rotated_dose_grid_skips_spatial_rules(case):
    case.dose.ImageOrientationPatient = [0, 1, 0, 1, 0, 0]
    report = run(case)
    assert failed_ids(report) == set()
    for rid in ("DOSE-004", "DOSE-005", "DOSE-006"):
        assert status_of(report, rid) == "SKIPPED"


def test_multiple_fraction_groups_skip_prescription_arithmetic(case):
    fg2 = copy.deepcopy(case.plan.FractionGroupSequence[0])
    fg2.FractionGroupNumber = 2
    case.plan.FractionGroupSequence.append(fg2)
    assert status_of(run(case), "PLAN-004") == "SKIPPED"


def test_multiple_prescription_levels_skip_prescription_arithmetic(case):
    ref2 = copy.deepcopy(case.plan.DoseReferenceSequence[0])
    ref2.DoseReferenceNumber = 2
    ref2.TargetPrescriptionDose = 54.0
    case.plan.DoseReferenceSequence.append(ref2)
    report = run(case)
    assert status_of(report, "PLAN-004") == "SKIPPED"
    assert (
        status_of(report, "DOSE-006") == "PASS"
    )  # each target checked against its own prescription


def test_missing_beam_dose_skips_prescription_arithmetic(case):
    del case.plan.FractionGroupSequence[0].ReferencedBeamSequence[0].BeamDose
    assert status_of(run(case), "PLAN-004") == "SKIPPED"


# ---- target resolution falls back to ROI-name patterns when the plan declares none
def test_no_declared_target_uses_name_patterns(case):
    del case.plan.DoseReferenceSequence
    report = run(case)
    assert status_of(report, "LINK-004") == "NOT_APPLICABLE"
    assert status_of(report, "BEAM-006") == "PASS"
    assert status_of(report, "DOSE-005") == "PASS"
    for rid in ("PLAN-004", "DOSE-004", "DOSE-006"):  # these genuinely need a prescription
        assert status_of(report, rid) == "SKIPPED"


def test_no_declared_target_and_no_pattern_match_skips(case):
    del case.plan.DoseReferenceSequence
    report = run(case, {"BEAM-006": {"params": {"target_patterns": ["^GTV"]}}})
    assert status_of(report, "BEAM-006") == "SKIPPED"


# ---- control points beyond the static 2-CP case
def _arc_like(case, weights):
    beam = case.plan.BeamSequence[0]
    template = beam.ControlPointSequence[1]
    cps = [beam.ControlPointSequence[0]]
    for i, w in enumerate(weights[1:], start=1):
        cp = copy.deepcopy(template)
        cp.ControlPointIndex = i
        cp.CumulativeMetersetWeight = w
        cps.append(cp)
    beam.ControlPointSequence = cps
    beam.NumberOfControlPoints = len(cps)


def test_multi_control_point_beam_passes_when_monotonic(case):
    _arc_like(case, [0.0, 0.3, 0.7, 1.0])
    assert status_of(run(case), "BEAM-002") == "PASS"


def test_decreasing_meterset_weight_fails(case):
    _arc_like(case, [0.0, 0.7, 0.4, 1.0])
    assert failed_ids(run(case)) == {"BEAM-002"}


def test_declared_control_point_count_must_match(case):
    case.plan.BeamSequence[0].NumberOfControlPoints = 5
    assert failed_ids(run(case)) == {"BEAM-002"}


# ---- setup beams are not treated as treatment beams
def test_setup_beam_is_ignored_by_delivery_rules(case):
    setup = copy.deepcopy(case.plan.BeamSequence[0])
    setup.BeamNumber = 3
    setup.TreatmentDeliveryType = "SETUP"
    setup.ControlPointSequence[
        0
    ].GantryAngle = 999.0  # would fail BEAM-003 if it were a treatment beam
    case.plan.BeamSequence.append(setup)
    assert failed_ids(run(case)) == set()


# ---- isocentre must be inside the target in 3D, including z
def test_isocentre_just_beyond_last_slice_is_outside(case):
    for beam in case.plan.BeamSequence:
        beam.ControlPointSequence[0].IsocenterPosition = [
            0.0,
            0.0,
            17.6,
        ]  # last slice 15 + half thickness 2.5
    assert failed_ids(run(case)) == {"BEAM-006"}
    for beam in case.plan.BeamSequence:
        beam.ControlPointSequence[0].IsocenterPosition = [0.0, 0.0, 17.4]
    assert failed_ids(run(case)) == set()


def test_isocentre_outside_polygon_in_plane(case):
    for beam in case.plan.BeamSequence:
        beam.ControlPointSequence[0].IsocenterPosition = [25.0, 0.0, 0.0]  # PTV radius is 20
    assert failed_ids(run(case)) == {"BEAM-006"}
