from pathlib import Path

from rt_plan_guardian.catalog import catalog_markdown
from rt_plan_guardian.cli import main
from rt_plan_guardian.rules import REGISTRY, Rule
from rt_plan_guardian.synthetic import DEFECTS

REPO = Path(__file__).resolve().parents[1]


def make_demo(tmp_path, defect=None):
    args = ["demo", str(tmp_path / "case")]
    if defect:
        args += ["--defect", defect]
    assert main(args) == 0
    return str(tmp_path / "case")


def test_clean_case_exits_zero(tmp_path, capsys):
    assert main(["check", make_demo(tmp_path)]) == 0
    assert "Verdict: PASS" in capsys.readouterr().out


def test_error_failure_exits_one_and_names_the_rule(tmp_path, capsys):
    assert main(["check", make_demo(tmp_path, "wrong_patient_id")]) == 1
    out = capsys.readouterr().out
    assert "IDENT-001" in out and "RTPG-9999" in out and "Verdict: FAIL" in out


def test_warnings_pass_unless_fail_on_warning(tmp_path):
    case = make_demo(tmp_path, "hotspot")
    assert main(["check", case]) == 0
    assert main(["check", case, "--fail-on", "warning"]) == 1


def test_check_writes_reports(tmp_path):
    case = make_demo(tmp_path)
    out = tmp_path / "out"
    assert main(["check", case, "--out", str(out), "--format", "json,md"]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["rtpg_report.json", "rtpg_report.md"]


def test_incomplete_exit_code_when_a_rule_crashes(tmp_path, monkeypatch):
    class Crash(Rule):
        id, title, category, description = "TEST-001", "crash", "plan", "x"

        def evaluate(self, ctx, params):
            raise RuntimeError("boom")

    monkeypatch.setitem(REGISTRY, "TEST-001", Crash)
    assert main(["check", make_demo(tmp_path)]) == 3


def test_input_and_config_errors_exit_two(tmp_path, capsys):
    assert main(["check", str(tmp_path / "missing")]) == 2
    case = make_demo(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("rules:\n  NOPE-1: {}\n")
    assert main(["check", case, "--config", str(bad)]) == 2
    assert "unknown rule id" in capsys.readouterr().err


def test_custom_config_is_applied(tmp_path):
    case = make_demo(tmp_path, "hotspot")
    cfg = tmp_path / "strict.yaml"
    cfg.write_text("rules:\n  DOSE-004:\n    severity: error\n")
    assert main(["check", case, "--config", str(cfg)]) == 1


def test_rules_listing(capsys):
    assert main(["rules"]) == 0
    out = capsys.readouterr().out
    assert all(rid in out for rid in REGISTRY)


def test_demo_list_and_unknown_defect(tmp_path, capsys):
    assert main(["demo", "--list"]) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in DEFECTS)
    assert main(["demo", str(tmp_path / "x"), "--defect", "nope"]) == 2


def test_committed_rule_catalog_is_up_to_date():
    """docs/rule-catalog.md is generated from the registry; regenerate with `make docs`."""
    committed = (REPO / "docs" / "rule-catalog.md").read_text(encoding="utf-8")
    assert committed.strip() == catalog_markdown().strip()


def test_every_rule_has_documentation_fields():
    for rid, cls in REGISTRY.items():
        assert cls.title and cls.description and cls.category, rid
        assert rid.split("-")[1].isdigit()


def test_committed_template_is_up_to_date_and_loads():
    """configs/template_all_options.yaml is generated from the registry; regenerate with `make docs`."""
    from rt_plan_guardian.catalog import template_yaml
    from rt_plan_guardian.config import load_ruleset

    committed = (REPO / "configs" / "template_all_options.yaml").read_text(encoding="utf-8")
    assert committed.strip() == template_yaml().strip()
    rs = load_ruleset(REPO / "configs" / "template_all_options.yaml")
    assert list(rs.settings) == list(REGISTRY)
