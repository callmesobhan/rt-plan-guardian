"""The core correctness proof.

For every injected defect: exactly the declared rules FAIL, nothing crashes, and anything else
that did not pass is only an expected, explained SKIP. For every technique's clean case: every
applicable rule passes - so a rule that cries wolf is as much a test failure as one that misses a
defect.
"""

import pytest

from rt_plan_guardian.engine import run_review
from rt_plan_guardian.rules import REGISTRY
from rt_plan_guardian.synthetic import DEFECTS, TECHNIQUES, make_case

from .conftest import failed_ids, ruleset_with


def test_clean_case_passes_every_applicable_rule(any_case, review):
    report = review(any_case)
    not_passing = {
        r.rule_id: (r.status.value, r.summary)
        for r in report.results
        if r.status.value not in ("PASS", "NOT_APPLICABLE")
    }
    assert not_passing == {}
    assert report.verdict.value == "PASS"


@pytest.mark.parametrize("technique", TECHNIQUES)
def test_clean_case_exercises_the_technique_rules(technique, review):
    """Each technique's rules must actually run (PASS), not hide behind NOT_APPLICABLE."""
    report = review(make_case(technique=technique))
    passed = {r.rule_id for r in report.results if r.status.value == "PASS"}
    expected = {
        "photon": {"BEAM-007", "BEAM-008", "DOSE-004", "DOSE-006"},
        "electron": {"ELEC-001", "ELEC-002", "ELEC-003"},
        "proton": {f"ION-00{i}" for i in range(1, 7)},
        "brachy": {f"BRACHY-00{i}" for i in range(1, 7)},
    }[technique]
    assert expected <= passed, expected - passed


@pytest.mark.parametrize("name", sorted(DEFECTS))
def test_defect_fails_exactly_the_declared_rules(name):
    defect = DEFECTS[name]
    case = make_case(technique=defect.technique)
    defect.apply(case)
    rs = ruleset_with(defect.rules) if defect.rules else ruleset_with({})
    report = run_review(case.to_context(), rs)

    crashed = [r for r in report.results if r.status.value == "ERROR"]
    assert crashed == [], f"rules crashed: {[(r.rule_id, r.summary) for r in crashed]}"
    assert failed_ids(report) == set(defect.expected_failures)

    skipped = {r.rule_id for r in report.results if r.status.value == "SKIPPED"}
    unexpected_skips = skipped - defect.expected_skips - _skips_from_missing_objects(case)
    assert unexpected_skips == set(), "rules were skipped that the defect does not explain"


def _skips_from_missing_objects(case) -> set[str]:
    """Rules that legitimately SKIP because an object was not supplied."""
    missing = {
        k
        for k, ds in (("rtplan", case.plan), ("rtstruct", case.struct), ("rtdose", case.dose))
        if ds is None
    }
    return {rid for rid, cls in REGISTRY.items() if missing & set(cls.requires)}


def test_every_rule_is_covered_by_at_least_one_defect():
    """If you add a rule, add a defect that makes it fail - otherwise it is unproven."""
    covered = set().union(*(d.expected_failures for d in DEFECTS.values()))
    uncovered = set(REGISTRY) - covered
    # INPUT-002/003, STRUCT-006 and DVH-001 are proven by dedicated tests (they need several
    # objects, an external file, or constraint data); everything else must have a defect.
    assert uncovered <= {"INPUT-002", "INPUT-003", "STRUCT-006", "DVH-001"}, uncovered


def test_defects_are_described_and_target_a_known_technique():
    for d in DEFECTS.values():
        assert d.description
        assert d.technique in TECHNIQUES
