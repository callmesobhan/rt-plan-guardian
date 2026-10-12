"""Helpers shared by the Markdown and HTML renderers."""

from __future__ import annotations

import json
from typing import Any


def display_status(result: dict[str, Any]) -> str:
    """FAIL at warning severity is shown as WARN so a reader never mistakes it for a blocker."""
    if result["status"] == "FAIL" and result["severity"] == "warning":
        return "WARN"
    return str(result["status"])


def show(value: Any) -> str:
    """Compact, stable text for an observed/expected value."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


VERDICT_TEXT = {
    "PASS": "All enabled rules passed.",
    "PASS_WITH_WARNINGS": "No blocking failures, but one or more warnings need a human decision.",
    "FAIL": "One or more error-severity rules failed. Do not proceed until resolved or explicitly waived.",
    "INCOMPLETE": "Nothing failed, but at least one rule could not run correctly. Treat as NOT reviewed.",
}


def group_ignored(items: list[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    """[(reason, [file names])], largest first, so 80 CT slices read as one line."""
    groups: dict[str, list[str]] = {}
    for item in items:
        groups.setdefault(item["reason"], []).append(item["filename"])
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def summarise_names(names: list[str], limit: int = 3) -> str:
    shown = ", ".join(f"`{n}`" for n in names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")
