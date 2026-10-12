"""Prescription isodose handling (radiosurgery): explicit parameter or the labelled vendor reading."""

from rt_plan_guardian.engine import run_review
from rt_plan_guardian.plan_model import vendor_isodose_percent

from .conftest import failed_ids, ruleset_with, status_of


def gamma_plan(case, isodose, beam_dose, vr="DS", value=None):
    """Photon case re-labelled the way Elekta GammaPlan exports a prescription isodose."""
    ref = case.plan.DoseReferenceSequence[0]
    block = ref.private_block(0x0009, "ELEKTA GAMMAPLAN", create=True)
    block.add_new(0x27, vr, value if value is not None else str(isodose))
    for r in case.plan.FractionGroupSequence[0].ReferencedBeamSequence:
        r.BeamDose = beam_dose


def test_isodose_prescription_passes_and_is_documented(case):
    gamma_plan(case, 50, 2.0)  # sum 4 Gy x 30 fx x 50% = 60 Gy = Rx; Dmax 62 Gy = 52% of Rx/0.5
    report = run_review(case.to_context(), ruleset_with({}))
    assert failed_ids(report) == set()
    note = next(r for r in report.results if r.rule_id == "PLAN-004").checks[0].note
    assert "50% (private element (0009,1027)" in note and "empirical" in note


def test_a_wrong_isodose_reading_fails_loudly(case):
    gamma_plan(case, 46, 2.0)  # 120 x 0.46 = 55.2 != 60
    assert failed_ids(run_review(case.to_context(), ruleset_with({}))) >= {"PLAN-004"}


def test_vendor_reading_can_be_switched_off(case):
    gamma_plan(case, 50, 2.0)
    case.dose.DoseGridScaling = 0.0019  # peak 117.8 Gy: 98% of the 120 Gy 100%-level, 196% of Rx
    off = ruleset_with(
        {
            "PLAN-004": {"params": {"use_vendor_isodose": False}},
            "DOSE-004": {"params": {"use_vendor_isodose": False}},
        }
    )
    on = ruleset_with({})
    assert {"PLAN-004", "DOSE-004"} <= failed_ids(run_review(case.to_context(), off))
    assert not {"PLAN-004", "DOSE-004"} & failed_ids(run_review(case.to_context(), on))


def test_explicit_ruleset_isodose_needs_no_private_tag(case):
    for r in case.plan.FractionGroupSequence[0].ReferencedBeamSequence:
        r.BeamDose = 2.0
    explicit = ruleset_with(
        {
            "PLAN-004": {"params": {"prescription_isodose_percent": 50.0}},
            "DOSE-004": {"params": {"prescription_isodose_percent": 50.0}},
        }
    )
    assert failed_ids(run_review(case.to_context(), explicit)) == set()


def test_private_element_with_unknown_vr_bytes_is_read(case):
    gamma_plan(case, 46, 1.0, vr="UN", value=b"46")
    assert vendor_isodose_percent(case.plan) == 46.0


def test_implausible_or_foreign_values_are_ignored(case):
    gamma_plan(case, 0, 1.0)
    assert vendor_isodose_percent(case.plan) is None
    other = case.copy()
    ref = other.plan.DoseReferenceSequence[0]
    ref.private_block(0x0011, "SOMEONE ELSE", create=True).add_new(0x27, "DS", "50")
    assert vendor_isodose_percent(other.plan) is None


def test_hotspot_is_normalised_to_the_100_percent_level(case):
    gamma_plan(case, 50, 2.0)
    case.dose.DoseGridScaling = (
        0.0019  # peak 117.8 Gy = 98% of Rx/0.5 (120 Gy): fine; 196% of Rx raw
    )
    report = run_review(case.to_context(), ruleset_with({}))
    assert status_of(report, "DOSE-004") == "PASS"
