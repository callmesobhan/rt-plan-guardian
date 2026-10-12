"""Result types shared by the rule engine and the report writers.

Everything here is plain data and JSON-serialisable: a report must be
reproducible and diffable, so nothing in it may depend on live pydicom objects.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """How much a failed rule matters.

    ERROR   - the data is inconsistent, impossible, or unsafe to deliver.
    WARNING - outside a configured clinical/technical expectation; a human
              decides whether it is acceptable.
    """

    ERROR = "error"
    WARNING = "warning"


class Status(str, Enum):
    """Outcome of one rule. Every enabled rule always ends in exactly one."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # rule does not apply to this plan (e.g. no VMAT arcs)
    SKIPPED = "SKIPPED"  # rule applies but could not be evaluated (missing input, unsupported data)
    ERROR = "ERROR"  # the rule itself crashed - a bug or malformed data, never a silent pass
    DISABLED = "DISABLED"  # switched off by the ruleset


class Verdict(str, Enum):
    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"  # nothing failed, but at least one rule crashed


def to_jsonable(value: Any) -> Any:
    """Convert pydicom/numpy values to plain JSON types (stable and lossless enough for audit)."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, int):  # also pydicom IS
        return int(value)
    if isinstance(value, float):  # also pydicom DSfloat
        f = float(value)
        return f if math.isfinite(f) else str(f)
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset) or type(value).__name__ == "MultiValue":
        items = sorted(value, key=str) if isinstance(value, set | frozenset) else value
        return [to_jsonable(v) for v in items]
    if hasattr(value, "item") and callable(value.item):  # numpy scalar
        return to_jsonable(value.item())
    if hasattr(value, "tolist") and callable(value.tolist):  # numpy array
        return to_jsonable(value.tolist())
    return str(value)  # PersonName, UID, DSdecimal, ...


@dataclass(frozen=True)
class Check:
    """One concrete comparison made by a rule: the unit of audit evidence.

    A rule with ten beams produces ten Checks, so a reviewer can see exactly
    which beam failed and what was compared against what.
    """

    subject: str
    ok: bool
    observed: Any = None
    expected: Any = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "ok": self.ok,
            "observed": to_jsonable(self.observed),
            "expected": to_jsonable(self.expected),
            "note": self.note,
        }


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    title: str
    category: str
    severity: Severity
    status: Status
    summary: str
    checks: tuple[Check, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    reference: str = ""

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "category": self.category,
            "severity": self.severity.value,
            "status": self.status.value,
            "summary": self.summary,
            "reference": self.reference,
            "params": to_jsonable(self.params),
            "checks": [c.to_dict() for c in self.checks],
        }
