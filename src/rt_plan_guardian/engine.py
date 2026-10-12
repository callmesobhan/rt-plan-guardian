"""The rule engine and the ReviewReport it produces.

Fail-safe semantics, in one place:
  * every enabled rule ends in exactly one status - nothing is ever silently dropped;
  * a rule that cannot run (missing input, unsupported data) is SKIPPED, never PASS;
  * a rule that crashes is ERROR, never PASS, and makes the verdict INCOMPLETE at best;
  * a rule that yields no checks is an ERROR (a rule that checked nothing proves nothing).
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pydicom

from . import __version__
from .config import RuleSet
from .context import PlanContext
from .errors import CannotEvaluate, NotApplicable
from .models import Check, RuleResult, Severity, Status, Verdict, to_jsonable
from .rules import REGISTRY, Rule

log = logging.getLogger(__name__)

REPORT_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class ReviewReport:
    ruleset: RuleSet
    context: PlanContext
    results: tuple[RuleResult, ...]
    generated_at: str
    tool_version: str = __version__

    # ---- verdict & counts
    @property
    def verdict(self) -> Verdict:
        if any(r.status is Status.FAIL and r.severity is Severity.ERROR for r in self.results):
            return Verdict.FAIL
        if any(r.status is Status.ERROR for r in self.results):
            return Verdict.INCOMPLETE
        if any(r.status is Status.FAIL for r in self.results):
            return Verdict.PASS_WITH_WARNINGS
        return Verdict.PASS

    @property
    def summary(self) -> dict[str, int]:
        c: Counter[str] = Counter()
        for r in self.results:
            if r.status is Status.FAIL:
                c["fail_error" if r.severity is Severity.ERROR else "fail_warning"] += 1
            else:
                c[r.status.value.lower()] += 1
        keys = (
            "pass",
            "fail_error",
            "fail_warning",
            "skipped",
            "error",
            "not_applicable",
            "disabled",
        )
        out = {k: c.get(k, 0) for k in keys}
        out["total"] = len(self.results)
        return out

    def by_status(self, *statuses: Status) -> list[RuleResult]:
        return [r for r in self.results if r.status in statuses]

    # ---- serialisation
    def _content(self) -> dict[str, Any]:
        """Everything that defines *what was concluded* - excludes timestamps and environment,
        so identical inputs + ruleset + tool version always hash identically."""
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "tool": {"name": "rt-plan-guardian", "version": self.tool_version},
            "ruleset": {
                "name": self.ruleset.name,
                "version": self.ruleset.version,
                "description": self.ruleset.description,
                "source": self.ruleset.source,
                "sha256": self.ruleset.sha256,
                "extends": list(self.ruleset.extends),
                "parents_sha256": dict(self.ruleset.parents_sha256),
                "sources": list(self.ruleset.sources),
                "resolved": {rid: s.to_dict() for rid, s in self.ruleset.settings.items()},
            },
            "inputs": [
                {
                    "kind": f.kind,
                    "filename": f.name,
                    "sha256": f.sha256,
                    "sop_class_uid": f.sop_class_uid,
                    "sop_instance_uid": f.sop_instance_uid,
                }
                for f in self.context.files
            ],
            "case_id": self.context.case_id,
            "techniques": sorted(self.context.techniques),
            "ignored_files": [
                {"filename": i.name, "reason": i.reason} for i in self.context.ignored
            ],
            "unassigned_files": [
                {"filename": i.name, "reason": i.reason} for i in self.context.unassigned
            ],
            "verdict": self.verdict.value,
            "summary": self.summary,
            "results": [r.to_dict() for r in self.results],
        }

    @property
    def content_sha256(self) -> str:
        blob = json.dumps(
            self._content(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        body = self._content()
        body["run"] = {
            "generated_at": self.generated_at,
            "environment": {
                "python": platform.python_version(),
                "pydicom": pydicom.__version__,
                "numpy": np.__version__,
            },
        }
        body["content_sha256"] = self.content_sha256
        return body

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(to_jsonable(self.to_dict()), indent=indent, ensure_ascii=False)


def _evaluate_rule(
    rule_cls: type[Rule], ctx: PlanContext, severity: Severity, params: Mapping[str, Any]
) -> RuleResult:
    def result(status: Status, summary: str, checks: tuple[Check, ...] = ()) -> RuleResult:
        return RuleResult(
            rule_id=rule_cls.id,
            title=rule_cls.title,
            category=rule_cls.category,
            severity=severity,
            status=status,
            summary=summary,
            checks=checks,
            params=dict(params),
            reference=rule_cls.reference,
        )

    missing = [k for k in rule_cls.requires if not ctx.has(k)]
    if missing:
        return result(
            Status.SKIPPED, f"Not evaluated: {', '.join(m.upper() for m in missing)} not supplied"
        )
    if rule_cls.applies_to and not (ctx.techniques & rule_cls.applies_to):
        from .plan_model import describe_techniques

        return result(
            Status.NOT_APPLICABLE,
            f"Not applicable: rule is for {', '.join(sorted(rule_cls.applies_to))} plans; "
            f"this plan is {describe_techniques(ctx.techniques)}",
        )
    try:
        checks = tuple(rule_cls().evaluate(ctx, params))
    except NotApplicable as exc:
        return result(Status.NOT_APPLICABLE, f"Not applicable: {exc}")
    except CannotEvaluate as exc:
        return result(Status.SKIPPED, f"Not evaluated: {exc}")
    except Exception as exc:  # a crashing rule must surface, never pass
        log.debug("rule %s crashed", rule_cls.id, exc_info=True)
        return result(Status.ERROR, f"Rule crashed ({type(exc).__name__}: {exc})")
    if not checks:
        return result(
            Status.ERROR, "Rule produced no checks (a rule that checks nothing proves nothing)"
        )
    failed = [c for c in checks if not c.ok]
    if failed:
        return result(Status.FAIL, f"{len(failed)} of {len(checks)} checks failed", checks)
    return result(Status.PASS, f"{len(checks)} of {len(checks)} checks passed", checks)


def run_review(ctx: PlanContext, ruleset: RuleSet, *, now: datetime | None = None) -> ReviewReport:
    results: list[RuleResult] = []
    for rule_id, rule_cls in REGISTRY.items():
        setting = ruleset.settings[rule_id]
        if not setting.enabled:
            results.append(
                RuleResult(
                    rule_id=rule_id,
                    title=rule_cls.title,
                    category=rule_cls.category,
                    severity=setting.severity,
                    status=Status.DISABLED,
                    summary="Disabled by ruleset",
                    params=dict(setting.params),
                    reference=rule_cls.reference,
                )
            )
            continue
        results.append(_evaluate_rule(rule_cls, ctx, setting.severity, setting.params))
    stamp = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    return ReviewReport(ruleset=ruleset, context=ctx, results=tuple(results), generated_at=stamp)
