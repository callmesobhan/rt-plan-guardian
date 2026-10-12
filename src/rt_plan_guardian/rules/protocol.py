"""Protocol dose-volume constraints, driven entirely by ruleset data.

The rule carries no clinical numbers of its own. A ruleset (for example the packaged
`hytec_brain_srs`) supplies a list of constraints, each one citing the document it comes from,
so a report shows not only *that* a limit failed but *where the limit is from*.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from ..context import PlanContext
from ..dvh import OPERATORS, Metric, compute, parse_metric
from ..errors import CannotEvaluate, NotApplicable
from ..models import Check
from .base import Rule, fmt, register

_REQUIRED = ("id", "roi", "metric", "op", "limit", "unit", "source")


def _patterns(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


@register
class DoseVolumeConstraints(Rule):
    id = "DVH-001"
    title = "Protocol dose-volume constraints are met"
    category = "protocol"
    description = (
        "Evaluates each constraint of the ruleset's `constraints` list against the dose grid: ROI "
        "name regexes select (and union) the structures, optional `exclude` regexes are subtracted "
        "(e.g. brain minus target), `metric` is Dmax, Dmean, D95%, D2cc, V12Gy, V95%..., compared "
        "with `op` and `limit` in `unit`. Every constraint must carry a `source` citation. A "
        "constraint whose ROI is absent fails (an unverifiable constraint is not a passed one) "
        "unless it is marked `optional: true`, in which case the check is recorded as not "
        "contoured. Voxel-centre sampling: a consistency screen, not the planning system's DVH."
    )
    reference = (
        "Ruleset-supplied (see each constraint's `source`), e.g. HyTEC, QUANTEC, protocol documents"
    )
    requires = ("rtstruct", "rtdose")
    default_params = {"constraints": []}

    @classmethod
    def validate_params(cls, params: Mapping[str, Any]) -> None:
        seen: set[str] = set()
        for i, c in enumerate(params["constraints"]):
            where = f"constraints[{i}]"
            if not isinstance(c, dict):
                raise ValueError(f"{where} must be a mapping")
            missing = [k for k in _REQUIRED if k not in c]
            if missing:
                raise ValueError(
                    f"{where} is missing {missing} (every constraint needs a citation in `source`)"
                )
            unknown = set(c) - {*_REQUIRED, "exclude", "optional"}
            if unknown:
                raise ValueError(f"{where} has unknown key(s) {sorted(unknown)}")
            if c["id"] in seen:
                raise ValueError(f"{where}: duplicate constraint id {c['id']!r}")
            seen.add(str(c["id"]))
            try:
                metric = parse_metric(str(c["metric"]))
            except ValueError as exc:
                raise ValueError(f"{where}: {exc}") from None
            if c["unit"] not in metric.valid_units:
                raise ValueError(
                    f"{where}: unit {c['unit']!r} invalid for {c['metric']}; use {metric.valid_units}"
                )
            if c["op"] not in OPERATORS:
                raise ValueError(f"{where}: op must be one of {sorted(OPERATORS)}")
            if isinstance(c["limit"], bool) or not isinstance(c["limit"], int | float):
                raise ValueError(f"{where}: limit must be a number")
            if not str(c["source"]).strip():
                raise ValueError(f"{where}: source citation is empty")
            for key in ("roi", "exclude"):
                for p in _patterns(c.get(key, [])):
                    try:
                        re.compile(p)
                    except re.error as exc:
                        raise ValueError(f"{where}: bad regex in {key}: {exc}") from None
            if not _patterns(c["roi"]):
                raise ValueError(f"{where}: roi must name at least one pattern")

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        constraints = list(params["constraints"])
        if not constraints:
            raise NotApplicable(
                "no dose-volume constraints configured (see the hytec_brain_srs ruleset)"
            )
        grid = ctx.dose_grid
        rois = ctx.rois
        rx = ctx.max_prescription_gy()
        step = lambda axis: float(np.median(np.abs(np.diff(axis)))) if len(axis) > 1 else 1.0  # noqa: E731
        voxel_cc = step(grid.xs) * step(grid.ys) * step(grid.zs) / 1000.0
        masks: dict[int, np.ndarray] = {}

        def mask_of(number: int) -> np.ndarray:
            if number not in masks:
                masks[number] = rois[number].mask_on_grid(grid.xs, grid.ys, grid.zs)
            return masks[number]

        def select(patterns: list[str]) -> list[int]:
            regexes = [re.compile(p, re.IGNORECASE) for p in patterns]
            return [
                n for n, g in rois.items() if g.slices and any(r.search(g.name) for r in regexes)
            ]

        for c in constraints:
            metric: Metric = parse_metric(str(c["metric"]))
            unit, op, limit = str(c["unit"]), str(c["op"]), float(c["limit"])
            names = _patterns(c["roi"])
            subject = f"{c['id']}: {metric.text} of {'+'.join(names)} {op} {fmt(limit)} {unit}"
            cite = f"source: {c['source']}"
            chosen = select(names)
            if not chosen:
                optional = bool(c.get("optional", False))
                yield Check(
                    subject,
                    optional,
                    "no ROI matches" if not optional else "not contoured (optional constraint)",
                    f"{op} {fmt(limit)} {unit}",
                    note=cite,
                )
                continue
            mask = np.zeros(grid.dose_gy.shape, dtype=bool)
            for n in chosen:
                mask |= mask_of(n)
            excluded = select(_patterns(c.get("exclude", [])))
            for n in excluded:
                mask &= ~mask_of(n)
            doses = grid.dose_gy[mask]
            if doses.size == 0:
                yield Check(
                    subject,
                    False,
                    "no dose voxels inside the structure",
                    f"{op} {fmt(limit)} {unit}",
                    note=cite,
                )
                continue
            try:
                value = compute(metric, doses, voxel_cc, rx, unit)
            except ValueError as exc:
                raise CannotEvaluate(f"{c['id']}: {exc}") from None
            yield Check(
                subject,
                OPERATORS[op](value, limit),
                f"{value:.2f} {unit}",
                f"{op} {fmt(limit)} {unit}",
                note=f"{doses.size} voxels sampled from {[rois[n].name for n in chosen]}"
                + (f" minus {[rois[n].name for n in excluded]}" if excluded else "")
                + f"; {cite}",
            )
