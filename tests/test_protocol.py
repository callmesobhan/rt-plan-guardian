"""Dose-volume metrics, the DVH-001 constraint rule and the HyTEC ruleset."""

import re

import numpy as np
import pytest

from rt_plan_guardian.config import load_ruleset, parse_ruleset
from rt_plan_guardian.dvh import compute, parse_metric
from rt_plan_guardian.engine import run_review
from rt_plan_guardian.errors import ConfigError
from rt_plan_guardian.synthetic import make_case

from .conftest import failed_ids, ruleset_with, status_of


# ---- metric engine
@pytest.mark.parametrize(
    "text, kind, arg",
    [
        ("Dmax", "dmax", 0),
        ("Dmean", "dmean", 0),
        ("D95%", "d_pct", 95),
        ("D2cc", "d_cc", 2),
        ("V12Gy", "v_gy", 12),
        ("V95%", "v_pct_rx", 95),
        ("D0.03cc", "d_cc", 0.03),
    ],
)
def test_parse_metric(text, kind, arg):
    m = parse_metric(text)
    assert (m.kind, m.arg) == (kind, arg)


@pytest.mark.parametrize("bad", ["dmax", "D95", "V12", "Vx", "D%", "Dmax2", ""])
def test_parse_metric_rejects_garbage(bad):
    with pytest.raises(ValueError, match="unknown metric"):
        parse_metric(bad)


def test_compute_known_values():
    doses = np.arange(1.0, 101.0)  # 100 voxels of 1..100 Gy, 0.5 cc each
    c = lambda text, unit, rx=None: compute(parse_metric(text), doses, 0.5, rx, unit)  # noqa: E731
    assert c("Dmax", "Gy") == 100 and c("Dmin", "Gy") == 1 and c("Dmean", "Gy") == 50.5
    assert c("D95%", "Gy") == pytest.approx(np.percentile(doses, 5))
    assert c("D2cc", "Gy") == 97.0  # hottest 2 cc = 4 voxels -> the 4th hottest is 97
    assert c("V50Gy", "cc") == 25.5 and c("V50Gy", "%") == 51.0
    assert c("V50%", "%", rx=100.0) == 51.0 and c("Dmax", "%Rx", rx=200.0) == 50.0
    with pytest.raises(ValueError, match="prescription"):
        c("Dmax", "%Rx")


# ---- DVH-001 on the synthetic photon case (PTV ~ 62 Gy plateau, cord ~ 0.5 Gy, 60 Gy Rx)
def constraint(**kw):
    base = {
        "id": "T1",
        "roi": ["^PTV$"],
        "metric": "Dmax",
        "op": "<=",
        "limit": 100,
        "unit": "Gy",
        "source": "unit test",
    }
    return {**base, **kw}


def run_constraints(case, *constraints):
    rs = ruleset_with({"DVH-001": {"params": {"constraints": list(constraints)}}})
    return run_review(case.to_context(), rs)


def checks(report):
    return next(r for r in report.results if r.rule_id == "DVH-001").checks


def test_passing_and_failing_constraints(case):
    report = run_constraints(
        case,
        constraint(id="cov", metric="D95%", op=">=", limit=57.0),
        constraint(id="hot", metric="Dmax", op="<=", limit=55.0),
        constraint(id="mean-pct", metric="Dmean", op="<=", limit=100.0, unit="%Rx"),
        constraint(id="cord", roi="^SpinalCord$", metric="Dmax", op="<=", limit=50.0),
    )
    by_id = {c.subject.split(":")[0]: c.ok for c in checks(report)}
    assert by_id == {"cov": True, "hot": False, "mean-pct": False, "cord": True}
    assert failed_ids(report) == {"DVH-001"}
    assert all("unit test" in c.note for c in checks(report))  # every result cites its source


def test_absent_roi_fails_unless_optional(case):
    required, optional = checks(
        run_constraints(
            case, constraint(id="a", roi="^Lung"), constraint(id="b", roi="^Lung", optional=True)
        )
    )
    assert not required.ok and "no ROI matches" in required.observed
    assert (
        optional.ok and "not contoured" in optional.observed
    )  # visible in the audit trail, not silent


def test_exclude_subtracts_structures(case):
    both = checks(
        run_constraints(
            case,
            constraint(id="all", roi="^BODY$", metric="V50Gy", op=">=", limit=0, unit="cc"),
            constraint(
                id="minus",
                roi="^BODY$",
                exclude="^PTV$",
                metric="V50Gy",
                op=">=",
                limit=0,
                unit="cc",
            ),
        )
    )
    volume = [float(re.match(r"[\d.]+", c.observed).group()) for c in both]
    assert 30 < volume[0] - volume[1] < 60  # the PTV cylinder is ~44 cc


def test_percent_of_rx_needs_a_prescription(case):
    del case.plan.DoseReferenceSequence
    report = run_constraints(case, constraint(roi="^PTV$", metric="Dmax", unit="%Rx"))
    assert status_of(report, "DVH-001") == "SKIPPED"


def test_no_constraints_is_not_applicable(case):
    assert status_of(run_review(case.to_context(), ruleset_with({})), "DVH-001") == "NOT_APPLICABLE"


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ({"id": "x"}, "missing"),
        (constraint(source=""), "citation is empty"),
        (constraint(metric="D95"), "unknown metric"),
        (constraint(unit="cc"), "invalid for Dmax"),
        (constraint(op="=="), "op must be"),
        (constraint(limit="ten"), "limit must be a number"),
        (constraint(roi=["("]), "bad regex"),
        (constraint(colour="red"), "unknown key"),
    ],
)
def test_constraint_validation_happens_at_load_time(bad, fragment):
    with pytest.raises(ConfigError, match=fragment):
        parse_ruleset({"rules": {"DVH-001": {"params": {"constraints": [bad]}}}})


def test_duplicate_constraint_ids_rejected():
    with pytest.raises(ConfigError, match="duplicate"):
        parse_ruleset(
            {"rules": {"DVH-001": {"params": {"constraints": [constraint(), constraint()]}}}}
        )


# ---- the shipped HyTEC ruleset
def _brain_case(scale=None):
    case = make_case()
    case.struct.StructureSetROISequence[1].ROIName = "Brain"  # reuse the large BODY cylinder
    if scale:
        case.dose.DoseGridScaling = scale
    return case


def test_hytec_ruleset_flags_large_v12():
    report = run_review(_brain_case().to_context(), load_ruleset("hytec_brain_srs"))
    result = next(r for r in report.results if r.rule_id == "DVH-001")
    assert result.status.value == "FAIL" and result.severity.value == "warning"
    assert report.verdict.value == "PASS_WITH_WARNINGS"
    assert "HyTEC" in result.checks[0].note and "minus ['PTV']" in result.checks[0].note


def test_hytec_ruleset_passes_when_v12_is_small():
    report = run_review(_brain_case(scale=0.0001).to_context(), load_ruleset("hytec_brain_srs"))
    assert status_of(report, "DVH-001") == "PASS"


def test_hytec_ruleset_fails_without_a_brain_roi(case):
    report = run_review(case.to_context(), load_ruleset("hytec_brain_srs"))
    assert failed_ids(report) == {"DVH-001"}  # unverifiable is not 'met'


def test_every_shipped_constraint_cites_its_source():
    from rt_plan_guardian.config import builtin_rulesets

    for name in builtin_rulesets():
        rs = load_ruleset(None if name == "default" else name)
        assert rs.sources, f"{name} has no citations"
        for c in rs.settings["DVH-001"].params["constraints"]:
            assert c["source"].strip()
