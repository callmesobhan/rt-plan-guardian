"""Dose-volume metrics on voxel-centre samples.

Metric grammar (case-sensitive, as in the protocol documents they come from):

    Dmax  Dmean  Dmin  Dmedian        dose statistics                    -> Gy or %Rx
    D95%                               minimum dose to the hottest 95 %   -> Gy or %Rx
    D2cc                               minimum dose to the hottest 2 cc   -> Gy or %Rx
    V12Gy                              volume receiving >= 12 Gy          -> cc or %
    V95%                               volume receiving >= 95 % of Rx     -> cc or %

These are consistency-screen values computed from the dose grid's voxel centres; they will differ
from a planning system's DVH engine for thin or small structures (see docs/architecture.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

_DOSE_UNITS = ("Gy", "%Rx")
_VOLUME_UNITS = ("cc", "%")
OPERATORS = {
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
}


@dataclass(frozen=True)
class Metric:
    kind: str  # dmax | dmean | dmin | dmedian | d_pct | d_cc | v_gy | v_pct_rx
    arg: float = 0.0
    text: str = ""

    @property
    def is_dose(self) -> bool:
        return self.kind.startswith("d")

    @property
    def valid_units(self) -> tuple[str, ...]:
        return _DOSE_UNITS if self.is_dose else _VOLUME_UNITS


_NUM = r"(\d+(?:\.\d+)?)"


def parse_metric(text: str) -> Metric:
    t = text.strip()
    simple = {"Dmax": "dmax", "Dmean": "dmean", "Dmin": "dmin", "Dmedian": "dmedian"}
    if t in simple:
        return Metric(simple[t], 0.0, t)
    for pattern, kind in (
        (rf"^D{_NUM}%$", "d_pct"),
        (rf"^D{_NUM}cc$", "d_cc"),
        (rf"^V{_NUM}Gy$", "v_gy"),
        (rf"^V{_NUM}%$", "v_pct_rx"),
    ):
        m = re.match(pattern, t)
        if m:
            return Metric(kind, float(m.group(1)), t)
    raise ValueError(
        f"unknown metric {text!r}; use Dmax, Dmean, Dmin, Dmedian, D<x>%, D<x>cc, V<x>Gy or V<x>%"
    )


def compute(
    metric: Metric, doses_gy: np.ndarray, voxel_cc: float, rx_gy: float | None, unit: str
) -> float:
    """Value of `metric` in `unit` for the voxel doses of one structure."""
    if doses_gy.size == 0:
        raise ValueError("no voxels")
    if metric.is_dose:
        gy = {
            "dmax": lambda: float(doses_gy.max()),
            "dmean": lambda: float(doses_gy.mean()),
            "dmin": lambda: float(doses_gy.min()),
            "dmedian": lambda: float(np.median(doses_gy)),
            "d_pct": lambda: float(np.percentile(doses_gy, 100.0 - metric.arg)),
            "d_cc": lambda: float(
                np.sort(doses_gy)[::-1][
                    min(doses_gy.size, max(1, round(metric.arg / voxel_cc))) - 1
                ]
            ),
        }[metric.kind]()
        if unit == "%Rx":
            if not rx_gy:
                raise ValueError("a prescription is needed for %Rx")
            return 100.0 * gy / rx_gy
        return gy
    threshold = (
        metric.arg if metric.kind == "v_gy" else (metric.arg / 100.0) * (rx_gy or float("nan"))
    )
    if np.isnan(threshold):
        raise ValueError("a prescription is needed for V<x>%")
    count = int(np.count_nonzero(doses_gy >= threshold))
    return count * voxel_cc if unit == "cc" else 100.0 * count / doses_gy.size
