"""Streamlit front-end. Run with `rtpg app` (needs the `app` extra).

A thin layer over the same engine the CLI uses: it adds no rules and no logic of its own.
Uploaded files are processed in memory only - this app never writes patient data to disk
and deliberately does not use Streamlit's caching for uploads.
"""

from __future__ import annotations

import streamlit as st

from rt_plan_guardian import __version__
from rt_plan_guardian.config import builtin_rulesets, load_ruleset, load_ruleset_bytes
from rt_plan_guardian.engine import ReviewReport, run_review
from rt_plan_guardian.errors import GuardianError
from rt_plan_guardian.loader import load_streams_cases
from rt_plan_guardian.report import display_status, render_html, render_markdown, show
from rt_plan_guardian.report.markdown import DISCLAIMER
from rt_plan_guardian.synthetic import DEFECTS, TECHNIQUES, make_case

CLEAN = "(none - clean case)"

st.set_page_config(page_title="RT-Plan Guardian", page_icon=None, layout="wide")


def _demo_context(technique: str, defect: str):
    case = make_case(technique=technique)
    if defect != CLEAN:
        DEFECTS[defect].apply(case)
    return case.to_context()


def _sidebar():
    st.sidebar.header("Ruleset")
    names = list(builtin_rulesets())
    choice = st.sidebar.radio(
        "Rules", ["Built-in ruleset", "Upload YAML"], label_visibility="collapsed"
    )
    ruleset = None
    if choice == "Built-in ruleset":
        name = st.sidebar.selectbox("Built-in ruleset", names, label_visibility="collapsed")
        ruleset = load_ruleset(None if name == "default" else name)
    else:
        up = st.sidebar.file_uploader("Ruleset YAML", type=["yaml", "yml"])
        if up is not None:
            try:
                ruleset = load_ruleset_bytes(up.getvalue(), up.name)
            except GuardianError as exc:
                st.sidebar.error(str(exc))
    if ruleset is not None:
        st.sidebar.caption(f"{ruleset.name} v{ruleset.version}")
        for citation in ruleset.sources:
            st.sidebar.caption(f"Source: {citation}")
    st.sidebar.header("Data")
    source = st.sidebar.radio(
        "Source", ["Upload DICOM-RT files", "Synthetic demo case"], label_visibility="collapsed"
    )
    return ruleset, source


def _render(report: ReviewReport) -> None:
    d = report.to_dict()
    verdict = d["verdict"]
    banner = {
        "PASS": st.success,
        "PASS_WITH_WARNINGS": st.warning,
        "FAIL": st.error,
        "INCOMPLETE": st.error,
    }[verdict]
    banner(f"**{verdict.replace('_', ' ')}**")

    s = d["summary"]
    cols = st.columns(7)
    for col, (label, key) in zip(
        cols,
        [
            ("Passed", "pass"),
            ("Failed", "fail_error"),
            ("Warnings", "fail_warning"),
            ("Not evaluated", "skipped"),
            ("Crashed", "error"),
            ("N/A", "not_applicable"),
            ("Disabled", "disabled"),
        ],
        strict=True,
    ):
        col.metric(label, s[key])

    findings, audit, prov = st.tabs(["Findings", "Audit trail", "Provenance"])
    with findings:
        failed = [r for r in d["results"] if r["status"] == "FAIL"]
        if not failed:
            st.write("No failed rules.")
        for r in failed:
            with st.expander(f"{display_status(r)} - {r['rule_id']}: {r['title']}", expanded=True):
                st.caption(f"Severity: {r['severity']}. {r['summary']}. {r['reference']}")
                st.dataframe(
                    [
                        {
                            "Check": c["subject"],
                            "Observed": show(c["observed"]),
                            "Expected": show(c["expected"]),
                            "Note": c["note"],
                        }
                        for c in r["checks"]
                        if not c["ok"]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
        for r in (x for x in d["results"] if x["status"] in ("SKIPPED", "ERROR")):
            st.info(f"{r['status']} - {r['rule_id']}: {r['summary']}")
    with audit:
        statuses = sorted({display_status(r) for r in d["results"]})
        chosen = st.multiselect("Show results", statuses, default=statuses)
        rows = [
            {
                "Rule": r["rule_id"],
                "Result": display_status(r),
                "Severity": r["severity"],
                "Title": r["title"],
                "Detail": r["summary"],
            }
            for r in d["results"]
            if display_status(r) in chosen
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        rule_id = st.selectbox("Inspect checks for", [r["rule_id"] for r in d["results"]])
        picked = next(r for r in d["results"] if r["rule_id"] == rule_id)
        st.dataframe(
            [
                {
                    "OK": c["ok"],
                    "Check": c["subject"],
                    "Observed": show(c["observed"]),
                    "Expected": show(c["expected"]),
                    "Note": c["note"],
                }
                for c in picked["checks"]
            ]
            or [{"OK": "", "Check": picked["summary"], "Observed": "", "Expected": "", "Note": ""}],
            use_container_width=True,
            hide_index=True,
        )
    with prov:
        st.write(f"Tool `rtpg {d['tool']['version']}` - generated {d['run']['generated_at']}")
        st.write(
            f"Ruleset **{d['ruleset']['name']}** v{d['ruleset']['version']} (`{d['ruleset']['source']}`)"
        )
        st.code(f"content sha256: {d['content_sha256']}", language=None)
        st.dataframe(
            [
                {
                    "Kind": i["kind"].upper(),
                    "File": i["filename"],
                    "SOP Instance UID": i["sop_instance_uid"],
                    "SHA-256": i["sha256"],
                }
                for i in d["inputs"]
            ],
            use_container_width=True,
            hide_index=True,
        )
        for i in d["ignored_files"]:
            st.caption(f"Ignored: {i['filename']} - {i['reason']}")

    st.download_button("Download JSON", report.to_json(), "rtpg_report.json", "application/json")
    st.download_button(
        "Download Markdown", render_markdown(report), "rtpg_report.md", "text/markdown"
    )
    st.download_button("Download HTML", render_html(report), "rtpg_report.html", "text/html")


def main() -> None:
    st.title("RT-Plan Guardian")
    st.caption(
        f"v{__version__} - radiotherapy plan consistency & safety checker for photon, electron, "
        f"proton/ion and brachytherapy plans. {DISCLAIMER}"
    )
    ruleset, source = _sidebar()

    contexts = None
    error = ""
    if source == "Synthetic demo case":
        technique = st.selectbox("Technique", list(TECHNIQUES))
        defects = [CLEAN, *(n for n, d in DEFECTS.items() if d.technique == technique)]
        defect = st.selectbox("Inject a defect", defects, help="Mutates the clean synthetic plan.")
        if defect != CLEAN:
            st.caption(DEFECTS[defect].description)
        if st.button("Run review", type="primary", disabled=ruleset is None):
            contexts = [_demo_context(technique, defect)]
    else:
        st.caption("Files are processed in memory and never written to disk by this app.")
        uploads = st.file_uploader("RTPLAN, RTSTRUCT and RTDOSE files", accept_multiple_files=True)
        ready = bool(uploads) and ruleset is not None
        if st.button("Run review", type="primary", disabled=not ready):
            try:
                contexts = list(load_streams_cases((u.name, u.getvalue()) for u in uploads).cases)
            except GuardianError as exc:
                error = str(exc)

    if error:
        st.error(error)
    elif contexts is not None and ruleset is not None:
        st.session_state["reports"] = [run_review(ctx, ruleset) for ctx in contexts]
    reports = st.session_state.get("reports")
    if reports:
        if len(reports) > 1:
            labels = [f"{r.context.case_id} - {r.verdict.value}" for r in reports]
            picked = st.selectbox("Case", labels)
            _render(reports[labels.index(picked)])
        else:
            _render(reports[0])


main()
