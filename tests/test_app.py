from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

import rt_plan_guardian

APP = str(Path(rt_plan_guardian.__file__).with_name("app.py"))


def _demo_app():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    at.sidebar.radio[1].set_value("Synthetic demo case").run()
    return at


def test_app_starts_and_prompts_for_upload():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert at.title[0].value == "RT-Plan Guardian"
    assert at.button[0].disabled  # nothing to review yet


def test_demo_clean_case_passes():
    at = _demo_app()
    at.button[0].click().run()
    assert not at.exception
    assert "PASS" in at.success[0].value


def test_demo_defect_is_reported_as_failure():
    at = _demo_app()
    next(b for b in at.selectbox if b.label == "Inject a defect").select("wrong_patient_id").run()
    at.button[0].click().run()
    assert not at.exception
    assert "FAIL" in at.error[0].value
    assert any("IDENT-001" in e.label for e in at.expander)


def test_demo_proton_case_runs_ion_rules():
    at = _demo_app()
    next(b for b in at.selectbox if b.label == "Technique").select("proton").run()
    at.button[0].click().run()
    assert not at.exception
    assert "PASS" in at.success[0].value
