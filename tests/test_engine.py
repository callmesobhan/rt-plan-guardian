"""Engine semantics: the fail-safe guarantees ("unknown is never a pass")."""

import copy
from datetime import datetime, timezone

import pytest

from rt_plan_guardian.config import parse_ruleset
from rt_plan_guardian.engine import run_review
from rt_plan_guardian.models import Check, Severity
from rt_plan_guardian.rules import REGISTRY, Rule
from rt_plan_guardian.synthetic import DEFECTS

from .conftest import failed_ids, status_of


def _with_rule(monkeypatch, rule_cls):
    monkeypatch.setitem(REGISTRY, rule_cls.id, rule_cls)
    return parse_ruleset({})


class _Base(Rule):
    category = "plan"
    description = "test rule"


def test_every_rule_appears_in_the_report(case, review):
    report = review(case)
    assert [r.rule_id for r in report.results] == list(REGISTRY)


def test_missing_dose_skips_dose_rules_and_fails_input_check(case, review):
    DEFECTS["missing_dose_file"].apply(case)
    report = review(case)
    assert status_of(report, "INPUT-001") == "FAIL"
    for rid in ("DOSE-001", "DOSE-004", "DOSE-006", "LINK-002", "LINK-003"):
        assert status_of(report, rid) == "SKIPPED"
    skipped = next(r for r in report.results if r.rule_id == "DOSE-004")
    assert "RTDOSE" in skipped.summary
    assert report.verdict.value == "FAIL"


def test_skipped_rules_never_count_as_passes(case, ruleset):
    """If the ruleset says dose is optional, its rules are SKIPPED - visibly - not PASS."""
    rs = parse_ruleset({"rules": {"INPUT-001": {"params": {"required": ["rtplan", "rtstruct"]}}}})
    case.dose = None
    report = run_review(case.to_context(), rs)
    assert report.verdict.value == "PASS"
    assert report.summary["skipped"] > 0
    assert report.summary["pass"] + report.summary["skipped"] + report.summary[
        "not_applicable"
    ] == len(REGISTRY)


def test_crashing_rule_is_error_and_verdict_incomplete(case, monkeypatch):
    class Crash(_Base):
        id, title = "TEST-001", "always crashes"

        def evaluate(self, ctx, params):
            raise RuntimeError("boom")

    report = run_review(case.to_context(), _with_rule(monkeypatch, Crash))
    assert status_of(report, "TEST-001") == "ERROR"
    assert "boom" in next(r for r in report.results if r.rule_id == "TEST-001").summary
    assert report.verdict.value == "INCOMPLETE"


def test_crash_does_not_hide_a_real_failure(case, monkeypatch):
    class Crash(_Base):
        id, title = "TEST-001", "always crashes"

        def evaluate(self, ctx, params):
            raise RuntimeError("boom")

    DEFECTS["wrong_patient_id"].apply(case)
    report = run_review(case.to_context(), _with_rule(monkeypatch, Crash))
    assert report.verdict.value == "FAIL"


def test_rule_that_yields_nothing_is_an_error_not_a_pass(case, monkeypatch):
    class Empty(_Base):
        id, title = "TEST-002", "checks nothing"

        def evaluate(self, ctx, params):
            return []

    report = run_review(case.to_context(), _with_rule(monkeypatch, Empty))
    assert status_of(report, "TEST-002") == "ERROR"


def test_rule_can_report_failure_with_custom_check(case, monkeypatch):
    class Bad(_Base):
        id, title = "TEST-003", "always fails"

        def evaluate(self, ctx, params):
            yield Check("x", False, observed=1, expected=2)

    report = run_review(case.to_context(), _with_rule(monkeypatch, Bad))
    assert failed_ids(report) == {"TEST-003"}


def test_disabled_rule_is_listed_as_disabled(case):
    rs = parse_ruleset({"rules": {"DOSE-004": {"enabled": False}}})
    report = run_review(case.to_context(), rs)
    assert status_of(report, "DOSE-004") == "DISABLED"
    assert report.summary["disabled"] == 1


def test_not_applicable_when_no_patterns_configured(case):
    rs = parse_ruleset({"rules": {"STRUCT-002": {"params": {"required_roi_patterns": []}}}})
    report = run_review(case.to_context(), rs)
    assert status_of(report, "STRUCT-002") == "NOT_APPLICABLE"
    assert report.verdict.value == "PASS"


def test_warning_failure_gives_pass_with_warnings(case, review):
    DEFECTS["unapproved_plan"].apply(case)
    report = review(case)
    assert report.verdict.value == "PASS_WITH_WARNINGS"
    assert report.summary["fail_warning"] == 1 and report.summary["fail_error"] == 0


def test_severity_override_promotes_warning_to_blocking(case):
    rs = parse_ruleset({"rules": {"PLAN-001": {"severity": "error"}}})
    DEFECTS["unapproved_plan"].apply(case)
    report = run_review(case.to_context(), rs)
    assert report.verdict.value == "FAIL"
    assert next(r for r in report.results if r.rule_id == "PLAN-001").severity is Severity.ERROR


def test_content_hash_is_deterministic_and_ignores_timestamp(case, ruleset):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2030, 6, 6, tzinfo=timezone.utc)
    a = run_review(case.to_context(), ruleset, now=t1)
    b = run_review(copy.deepcopy(case).to_context(), ruleset, now=t2)
    assert a.generated_at != b.generated_at
    assert a.content_sha256 == b.content_sha256


def test_content_hash_changes_when_anything_changes(case, ruleset):
    clean = run_review(case.to_context(), ruleset).content_sha256
    DEFECTS["hotspot"].apply(case)
    assert run_review(case.to_context(), ruleset).content_sha256 != clean
    tweaked = parse_ruleset({"rules": {"DOSE-004": {"params": {"max_percent_of_rx": 120.0}}}})
    assert run_review(make_clean(), tweaked).content_sha256 != clean


def make_clean():
    from rt_plan_guardian.synthetic import make_case

    return make_case().to_context()


@pytest.mark.parametrize("name", ["wrong_patient_id", "hotspot", "missing_dose_file"])
def test_summary_counts_add_up(name, case, review):
    DEFECTS[name].apply(case)
    s = review(case).summary
    assert sum(v for k, v in s.items() if k != "total") == s["total"] == len(REGISTRY)


def test_reports_list_rules_in_category_order():
    from rt_plan_guardian.rules import CATEGORY_ORDER

    order = [CATEGORY_ORDER.index(cls.category) for cls in REGISTRY.values()]
    assert order == sorted(order)
    assert next(iter(REGISTRY)) == "INPUT-001" and list(REGISTRY)[-1] == "DVH-001"
