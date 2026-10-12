"""Report writers. All formats are rendered from the same ReviewReport.to_dict() payload,
so JSON (machine), Markdown (review/PR comments) and HTML (sign-off) can never disagree."""

from __future__ import annotations

from pathlib import Path

from ..engine import ReviewReport
from .common import display_status, show
from .html import render_html
from .markdown import render_markdown

FORMATS = ("json", "md", "html")

__all__ = ["FORMATS", "display_status", "render_html", "render_markdown", "show", "write_reports"]


def write_reports(
    report: ReviewReport,
    out_dir: str | Path,
    formats: tuple[str, ...] = FORMATS,
    stem: str = "rtpg_report",
) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    renderers = {
        "json": report.to_json,
        "md": lambda: render_markdown(report),
        "html": lambda: render_html(report),
    }
    written = []
    for fmt in formats:
        if fmt not in renderers:
            raise ValueError(f"unknown report format {fmt!r}; choose from {FORMATS}")
        path = out / f"{stem}.{fmt}"
        path.write_text(renderers[fmt](), encoding="utf-8")
        written.append(path)
    return written
