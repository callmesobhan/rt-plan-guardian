import hashlib
from pathlib import Path

import pytest

from rt_plan_guardian.config import load_ruleset, load_ruleset_bytes, parse_ruleset
from rt_plan_guardian.errors import ConfigError
from rt_plan_guardian.models import Severity
from rt_plan_guardian.rules import REGISTRY

REPO = Path(__file__).resolve().parents[1]


def test_default_ruleset_covers_every_rule():
    rs = load_ruleset()
    assert list(rs.settings) == list(REGISTRY)
    assert rs.sha256 and rs.name


def test_unlisted_rules_run_with_defaults():
    rs = parse_ruleset({})
    for rid, cls in REGISTRY.items():
        assert rs.settings[rid].enabled
        assert rs.settings[rid].severity is cls.default_severity
        assert dict(rs.settings[rid].params) == dict(cls.default_params)


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ({"rules": {"NOPE-999": {}}}, "unknown rule id"),
        ({"rules": {"DOSE-004": {"params": {"max_percent": 1}}}}, "unknown parameter"),
        ({"rules": {"DOSE-004": {"params": {"max_percent_of_rx": "high"}}}}, "expected float"),
        ({"rules": {"DOSE-004": {"severity": "critical"}}}, "severity must be"),
        ({"rules": {"DOSE-004": {"enabled": "yes"}}}, "true or false"),
        ({"rules": {"DOSE-004": {"colour": "red"}}}, "settings must be a mapping"),
        ({"rules": ["DOSE-004"]}, "'rules' must be a mapping"),
        ({"rulesets": {}}, "unknown top-level"),
        ({"ruleset": {"owner": "x"}}, "'ruleset' must be"),
        (["x"], "top level must be a mapping"),
        ({"rules": {"INPUT-001": {"params": {"required": "rtplan"}}}}, "expected list"),
    ],
)
def test_invalid_rulesets_are_rejected(bad, fragment):
    with pytest.raises(ConfigError, match=fragment):
        parse_ruleset(bad)


def test_integer_accepted_for_float_parameter():
    rs = parse_ruleset({"rules": {"DOSE-004": {"params": {"max_percent_of_rx": 112}}}})
    assert rs.settings["DOSE-004"].params["max_percent_of_rx"] == 112


def test_severity_is_case_insensitive():
    assert (
        parse_ruleset({"rules": {"PLAN-001": {"severity": "ERROR"}}}).settings["PLAN-001"].severity
        is Severity.ERROR
    )


def test_malformed_yaml_and_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_ruleset_bytes(b"rules: [unclosed")
    with pytest.raises(ConfigError, match="not found"):
        load_ruleset(tmp_path / "missing.yaml")


def test_sha256_matches_file_bytes(tmp_path):
    path = tmp_path / "site.yaml"
    path.write_bytes(b"ruleset:\n  name: Site\n  version: '3'\n")
    rs = load_ruleset(path)
    assert rs.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (rs.name, rs.version, rs.source) == ("Site", "3", "site.yaml")


@pytest.mark.parametrize("path", sorted((REPO / "configs").glob("*.yaml")), ids=lambda p: p.name)
def test_shipped_example_configs_are_valid(path):
    assert load_ruleset(path).settings
