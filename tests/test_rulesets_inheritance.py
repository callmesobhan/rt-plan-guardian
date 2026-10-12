import hashlib

import pytest

from rt_plan_guardian.config import builtin_rulesets, load_ruleset, load_ruleset_bytes
from rt_plan_guardian.engine import run_review
from rt_plan_guardian.errors import ConfigError
from rt_plan_guardian.models import Severity
from rt_plan_guardian.rules import REGISTRY


def test_child_overrides_parent_rule_and_params(tmp_path):
    (tmp_path / "base.yaml").write_text(
        "ruleset: {name: Base, version: '1'}\nrules:\n  DOSE-004: {severity: error, params: {max_percent_of_rx: 105.0}}\n  PLAN-003: {params: {max_fractions: 10}}\n"
    )
    child = tmp_path / "child.yaml"
    child.write_text(
        "ruleset: {name: Child, extends: base.yaml}\nrules:\n  DOSE-004: {params: {max_percent_of_rx: 120.0}}\n"
    )
    rs = load_ruleset(child)
    s = rs.settings["DOSE-004"]
    assert s.severity is Severity.ERROR  # inherited
    assert s.params["max_percent_of_rx"] == 120.0  # overridden
    assert rs.settings["PLAN-003"].params["max_fractions"] == 10  # inherited rule untouched
    assert rs.extends == ("base.yaml",)
    assert (
        rs.parents_sha256["base.yaml"]
        == hashlib.sha256((tmp_path / "base.yaml").read_bytes()).hexdigest()
    )


def test_extends_a_builtin_ruleset_by_name():
    rs = load_ruleset_bytes(b"ruleset: {extends: aapm_mppg11a}\nrules: {}\n", "mine.yaml")
    assert rs.settings["PLAN-001"].severity is Severity.ERROR
    assert rs.extends == ("aapm_mppg11a", "default")


def test_cycles_and_unknown_parents_are_rejected(tmp_path):
    (tmp_path / "a.yaml").write_text("ruleset: {extends: b.yaml}\n")
    (tmp_path / "b.yaml").write_text("ruleset: {extends: a.yaml}\n")
    with pytest.raises(ConfigError, match="circular"):
        load_ruleset(tmp_path / "a.yaml")
    with pytest.raises(ConfigError, match="Unknown built-in ruleset"):
        load_ruleset_bytes(b"ruleset: {extends: nope}\n", "x.yaml")


def test_parent_typos_are_still_caught():
    with pytest.raises(ConfigError, match="unknown parameter"):
        load_ruleset_bytes(
            b"ruleset: {extends: default}\nrules:\n  DOSE-004: {params: {max_pct: 1}}\n", "x.yaml"
        )


def test_sources_must_be_a_list_of_strings():
    with pytest.raises(ConfigError, match="citation strings"):
        load_ruleset_bytes(b"ruleset: {sources: oops}\n", "x.yaml")


def test_report_embeds_ruleset_lineage_and_citations(case):
    report = run_review(case.to_context(), load_ruleset("hytec_brain_srs"))
    block = report.to_dict()["ruleset"]
    assert block["extends"] == ["default"] and block["parents_sha256"]["default"]
    assert any("HyTEC" in s for s in block["sources"])


@pytest.mark.parametrize("name", sorted(builtin_rulesets()))
def test_every_builtin_ruleset_loads_and_covers_all_rules(name):
    rs = load_ruleset(None if name == "default" else name)
    assert list(rs.settings) == list(REGISTRY) and rs.sources


def test_mppg11a_profile_makes_documented_items_blocking():
    rs = load_ruleset("aapm_mppg11a")
    for rid in ("PLAN-001", "PLAN-004", "ELEC-001", "ELEC-002", "BRACHY-004", "BEAM-008"):
        assert rs.settings[rid].severity is Severity.ERROR, rid
