import pytest

from rt_plan_guardian.config import load_ruleset, parse_ruleset
from rt_plan_guardian.engine import run_review
from rt_plan_guardian.synthetic import TECHNIQUES, SyntheticCase, make_case


def ruleset_with(rules: dict):
    """The default ruleset with per-rule overrides layered on top."""
    return parse_ruleset({"ruleset": {"extends": "default"}, "rules": rules})


@pytest.fixture
def case() -> SyntheticCase:
    """A fresh clean synthetic photon case per test (tests mutate it)."""
    return make_case()


@pytest.fixture(params=TECHNIQUES)
def any_case(request) -> SyntheticCase:
    """A fresh clean synthetic case for each technique (photon, electron, proton, brachy)."""
    return make_case(technique=request.param)


@pytest.fixture(scope="session")
def ruleset():
    return load_ruleset()


@pytest.fixture
def review(ruleset):
    """review(case, ruleset=None) -> ReviewReport"""

    def _review(c: SyntheticCase, rs=None):
        return run_review(c.to_context(), rs or ruleset)

    return _review


def failed_ids(report) -> set[str]:
    return {r.rule_id for r in report.results if r.status.value == "FAIL"}


def status_of(report, rule_id: str) -> str:
    return next(r for r in report.results if r.rule_id == rule_id).status.value
