"""Self-contained HTML report (no external requests - reports contain PHI and may be opened offline)."""

from __future__ import annotations

from importlib import resources

from jinja2 import Environment

from ..engine import ReviewReport
from ..models import to_jsonable
from .common import VERDICT_TEXT, display_status, group_ignored, show
from .markdown import DISCLAIMER

_env = Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
_env.filters["show"] = show
_env.filters["label"] = display_status


def render_html(report: ReviewReport) -> str:
    source = (
        resources.files("rt_plan_guardian.report")
        .joinpath("templates/report.html.j2")
        .read_text("utf-8")
    )
    d = to_jsonable(report.to_dict())
    return _env.from_string(source).render(
        r=d,
        verdict_text=VERDICT_TEXT[d["verdict"]],
        disclaimer=DISCLAIMER,
        ignored_groups=group_ignored(d["ignored_files"]),
        findings=[x for x in d["results"] if x["status"] == "FAIL"],
        not_run=[x for x in d["results"] if x["status"] in ("SKIPPED", "ERROR")],
    )
