"""Markdown report: reviewable in a pull request, ticket or chart-round note."""

from __future__ import annotations

from typing import Any

from ..engine import ReviewReport
from ..models import to_jsonable
from .common import VERDICT_TEXT, display_status, group_ignored, show, summarise_names

DISCLAIMER = (
    "Decision-support output only. This report does not replace independent physics plan "
    "review, treatment-planning-system verification or clinical judgement."
)


def _cell(value: Any) -> str:
    return show(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines


def render_markdown(report: ReviewReport) -> str:
    d = to_jsonable(report.to_dict())
    results = d["results"]
    summary = d["summary"]
    out: list[str] = [
        "# RT-Plan Guardian review report",
        "",
        f"**Verdict: {d['verdict']}** - {VERDICT_TEXT[d['verdict']]}",
        "",
        f"Case: `{d['case_id']}` - technique: {', '.join(d['techniques']) or 'none detected'}",
        "",
        *_table(
            ["Pass", "Fail (error)", "Warn", "Skipped", "Crashed", "N/A", "Disabled"],
            [
                [
                    summary["pass"],
                    summary["fail_error"],
                    summary["fail_warning"],
                    summary["skipped"],
                    summary["error"],
                    summary["not_applicable"],
                    summary["disabled"],
                ]
            ],
        ),
        "",
        "## Provenance",
        "",
        f"- Generated: {d['run']['generated_at']}",
        f"- Tool: {d['tool']['name']} {d['tool']['version']} (python {d['run']['environment']['python']}, "
        f"pydicom {d['run']['environment']['pydicom']})",
        f"- Ruleset: {d['ruleset']['name']} v{d['ruleset']['version']} - `{d['ruleset']['source']}`"
        + (f" (sha256 `{d['ruleset']['sha256'][:16]}...`)" if d["ruleset"]["sha256"] else ""),
        f"- Content hash (sha256): `{d['content_sha256']}`",
        "",
        "### Inputs",
        "",
        *_table(
            ["Kind", "File", "SOP Instance UID", "SHA-256"],
            [
                [i["kind"].upper(), i["filename"], i["sop_instance_uid"], i["sha256"][:16] + "..."]
                for i in d["inputs"]
            ],
        ),
    ]
    if d["ignored_files"]:
        out += ["", "Ignored (not reviewed):", ""]
        for reason, names in group_ignored(d["ignored_files"]):
            out.append(f"- {len(names)} file(s): {reason} ({summarise_names(names)})")
    if d["unassigned_files"]:
        out += ["", "Supplied RT objects not part of this case:", ""]
        out += [f"- `{i['filename']}` - {i['reason']}" for i in d["unassigned_files"]]

    findings = [r for r in results if r["status"] == "FAIL"]
    out += ["", "## Findings", ""]
    if not findings:
        out.append("No failed rules.")
    for r in findings:
        out += [
            f"### {display_status(r)} - {r['rule_id']}: {r['title']}",
            "",
            f"*Severity: {r['severity']}. {r['summary']}.*",
            "",
            *_table(
                ["Check", "Observed", "Expected", "Note"],
                [
                    [c["subject"], c["observed"], c["expected"], c["note"]]
                    for c in r["checks"]
                    if not c["ok"]
                ],
            ),
            "",
        ]

    not_run = [r for r in results if r["status"] in ("SKIPPED", "ERROR")]
    if not_run:
        out += [
            "## Not evaluated",
            "",
            *_table(
                ["Rule", "Status", "Reason"],
                [[r["rule_id"], r["status"], r["summary"]] for r in not_run],
            ),
            "",
        ]

    out += [
        "## Full audit trail",
        "",
        *_table(
            ["Rule", "Result", "Severity", "Title", "Detail"],
            [
                [r["rule_id"], display_status(r), r["severity"], r["title"], r["summary"]]
                for r in results
            ],
        ),
        "",
        "---",
        f"_{DISCLAIMER}_",
        "",
    ]
    return "\n".join(out)
