import json
import re

import pytest

from rt_plan_guardian.report import render_html, render_markdown, write_reports
from rt_plan_guardian.rules import REGISTRY
from rt_plan_guardian.synthetic import DEFECTS


@pytest.fixture
def failing_report(case, review):
    DEFECTS["wrong_patient_id"].apply(case)
    DEFECTS["hotspot"].apply(case)
    return review(case)


def test_json_report_structure(failing_report):
    d = json.loads(failing_report.to_json())
    assert d["schema_version"] == "1.0"
    assert d["verdict"] == "FAIL"
    assert [r["rule_id"] for r in d["results"]] == list(
        REGISTRY
    )  # full audit trail, not just failures
    assert len(d["inputs"]) == 3 and all(len(i["sha256"]) == 64 for i in d["inputs"])
    assert d["content_sha256"] == failing_report.content_sha256
    assert set(d["run"]) == {"generated_at", "environment"}
    ident = next(r for r in d["results"] if r["rule_id"] == "IDENT-001")
    bad = [c for c in ident["checks"] if not c["ok"]]
    assert bad and bad[0]["observed"] == "RTPG-9999" and bad[0]["expected"] == "RTPG-0001"


def test_resolved_ruleset_is_embedded_for_audit(failing_report):
    d = json.loads(failing_report.to_json())
    resolved = d["ruleset"]["resolved"]
    assert set(resolved) == set(REGISTRY)
    assert resolved["DOSE-004"]["params"]["max_percent_of_rx"] == 110.0


def test_markdown_lists_failures_and_full_trail(failing_report):
    md = render_markdown(failing_report)
    assert "**Verdict: FAIL**" in md
    assert "### FAIL - IDENT-001" in md and "### WARN - DOSE-004" in md
    for rid in REGISTRY:
        assert rid in md  # every rule is in the audit trail
    assert "Decision-support output only" in md


def test_markdown_escapes_table_pipes(case, review):
    case.dose.PatientID = "A|B"
    assert "A\\|B" in render_markdown(review(case))


def test_html_is_escaped_and_offline(case, review):
    case.dose.PatientID = "<script>alert(1)</script>"
    html = render_html(review(case))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert not re.search(r"(src|href)\s*=\s*[\"']https?:", html), (
        "report must not fetch external resources"
    )
    assert "<script" not in html.replace("&lt;script", "")


def test_html_shows_verdict_and_every_rule(failing_report):
    html = render_html(failing_report)
    assert "FAIL" in html and "IDENT-001" in html
    assert all(rid in html for rid in REGISTRY)


def test_warning_is_displayed_as_warn_not_fail(case, review):
    DEFECTS["unapproved_plan"].apply(case)
    md = render_markdown(review(case))
    assert "### WARN - PLAN-001" in md and "### FAIL" not in md


def test_write_reports(tmp_path, failing_report):
    paths = write_reports(failing_report, tmp_path / "out")
    assert sorted(p.suffix for p in paths) == [".html", ".json", ".md"]
    assert all(p.read_text(encoding="utf-8") for p in paths)
    with pytest.raises(ValueError, match="unknown report format"):
        write_reports(failing_report, tmp_path, ("pdf",))
