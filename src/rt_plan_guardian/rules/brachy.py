"""Brachytherapy rules (RT Plan with BrachyApplicationSetupSequence).

AAPM MPPG 11.a Table 7 lists the initial HDR plan-check items: source activity/air-kerma strength
against the decay table, channel assignment, index length/offset, number and first dwell
position, step size, applicator, prescription and approval. The DICOM Standard (PS3.3 C.8.8.15
RT Brachy Application Setups Module) defines the attributes; two facts used below were verified
against real Varian and Elekta exports:

* Total Reference Air Kerma = sum over channels of (air kerma rate x channel time / 3600), in uGy
  at 1 m (both vendors reproduce it to 5 significant figures);
* control points come in pairs for stepwise sources: the pair (2k, 2k+1) sits at one dwell
  position and cumulative time weight is flat between pairs. Dwell time = ChannelTotalTime x
  delta CumulativeTimeWeight / FinalCumulativeTimeWeight (vendors differ in the weight scale).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import numpy as np

from ..context import PlanContext
from ..errors import CannotEvaluate, NotApplicable
from ..models import Check, Severity
from ..plan_model import BRACHY, brachy_setups
from .base import Rule, fmt, register

#: Half-lives in days (BIPM DDEP evaluated values, rounded) used to sanity-check SourceIsotopeHalfLife.
KNOWN_HALF_LIVES_DAYS = {
    "ir192": 73.83,
    "co60": 1925.28,
    "i125": 59.40,
    "pd103": 16.99,
    "cs131": 9.689,
    "cs137": 10972.0,
    "yb169": 32.02,
}


def _isotope_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _setups(ctx: PlanContext) -> list[Any]:
    setups = brachy_setups(ctx.plan)
    if not setups:
        raise NotApplicable("no brachytherapy application setups in this plan")
    return setups


def _sources(plan: Any) -> dict[int, Any]:
    return {int(s.SourceNumber): s for s in plan.get("SourceSequence", []) if "SourceNumber" in s}


def _note(problems: list[str], limit: int = 3) -> str:
    return "; ".join(problems[:limit]) + (
        f" (+{len(problems) - limit} more)" if len(problems) > limit else ""
    )


def _channel_label(setup: Any, channel: Any) -> str:
    return f"Setup {setup.get('ApplicationSetupNumber', '?')} channel {channel.get('ChannelNumber', '?')}"


def dwell_positions(channel: Any) -> list[tuple[float, float]]:
    """[(position mm, dwell seconds)] for a STEPWISE channel; [] if it cannot be derived."""
    cps = list(channel.get("BrachyControlPointSequence", []))
    final = channel.get("FinalCumulativeTimeWeight")
    total = channel.get("ChannelTotalTime")
    if len(cps) < 2 or len(cps) % 2 or not final or float(final) <= 0 or total is None:
        return []
    out = []
    for k in range(0, len(cps), 2):
        delta = float(cps[k + 1].get("CumulativeTimeWeight", 0)) - float(
            cps[k].get("CumulativeTimeWeight", 0)
        )
        out.append(
            (
                float(cps[k].get("ControlPointRelativePosition", math.nan)),
                float(total) * delta / float(final),
            )
        )
    return out


def _parse_datetime(date: Any, time: Any) -> datetime | None:
    d = str(date or "").strip()
    if len(d) != 8 or not d.isdigit():
        return None
    t = re.sub(r"[^0-9]", "", str(time or "").split(".")[0]).ljust(6, "0")[:6]
    try:
        return datetime.strptime(d + t, "%Y%m%d%H%M%S")
    except ValueError:
        return None


@register
class BrachySourceDefinition(Rule):
    id = "BRACHY-001"
    title = "Brachytherapy sources are fully and plausibly defined"
    category = "brachy"
    description = (
        "Each source needs a unique number, an isotope name, a positive half-life that agrees with "
        "the isotope when it is a recognised one, and a positive strength (air kerma rate for gamma "
        "emitters, including exports that declare SourceStrengthUnits=AIR_KERMA_RATE; SourceStrength "
        "for DOSE_RATE_WATER beta sources). Dose is proportional to this strength, so an "
        "implausible value propagates directly into every dwell time."
    )
    reference = (
        "DICOM PS3.3 C.8.8.15 Source Sequence; AAPM MPPG 11.a Table 7 (source strength); "
        "half-lives: BIPM DDEP evaluations"
    )
    requires = ("rtplan",)
    applies_to = frozenset({BRACHY})
    default_params = {"half_life_tolerance_percent": 2.0}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        _setups(ctx)
        tol = float(params["half_life_tolerance_percent"])
        sources = list(ctx.plan.get("SourceSequence", []))
        yield Check("Plan defines at least one source", bool(sources), len(sources), ">= 1")
        numbers = [int(s.SourceNumber) for s in sources if "SourceNumber" in s]
        dup = sorted(n for n, c in Counter(numbers).items() if c > 1)
        yield Check("Source numbers are unique", not dup, dup or "all unique", "unique")
        for s in sources:
            label = f"Source {s.get('SourceNumber', '?')}"
            name = str(s.get("SourceIsotopeName", "") or "")
            half_life = float(s.get("SourceIsotopeHalfLife", 0) or 0)
            problems: list[str] = []
            if not name:
                problems.append("SourceIsotopeName missing")
            if half_life <= 0:
                problems.append("SourceIsotopeHalfLife must be > 0")
            known = KNOWN_HALF_LIVES_DAYS.get(_isotope_key(name))
            note = ""
            if known and half_life > 0 and abs(half_life - known) > known * tol / 100.0:
                problems.append(
                    f"half-life {fmt(half_life)} d differs from {name} ({fmt(known)} d) by > {fmt(tol)}%"
                )
            elif not known and name:
                note = (
                    f"isotope name {name!r} not in the half-life table; half-life not cross-checked"
                )
            units = str(s.get("SourceStrengthUnits", "") or "").upper()
            if units == "DOSE_RATE_WATER":  # beta sources: strength in its own units
                strength, field_name = s.get("SourceStrength"), "SourceStrength"
            else:  # gamma emitters, incl. SourceStrengthUnits=AIR_KERMA_RATE (Elekta exports this)
                strength, field_name = s.get("ReferenceAirKermaRate"), "ReferenceAirKermaRate"
            if strength is None or float(strength) <= 0:
                problems.append(f"{field_name} must be > 0")
            yield Check(
                f"{label} ({name or 'unnamed'}, T1/2 {fmt(half_life)} d)",
                not problems,
                "plausible" if not problems else f"{len(problems)} problem(s)",
                "plausible",
                note=_note(problems) or note,
            )


@register
class TotalReferenceAirKerma(Rule):
    id = "BRACHY-002"
    title = "Total Reference Air Kerma equals source strength x total channel time"
    category = "brachy"
    description = (
        "Two independent quantities in the plan must agree: TotalReferenceAirKerma (uGy at 1 m) and "
        "the sum over channels of the source's air kerma rate (uGy/h at 1 m) times channel time / "
        "3600. A mismatch means a dwell time or source strength was edited after the total was "
        "written. Verified on real Varian and Elekta exports (agreement to 5 significant figures)."
    )
    reference = "DICOM PS3.3 C.8.8.15 - Total Reference Air Kerma (300A,0250), Channel Total Time (300A,0286)"
    requires = ("rtplan",)
    applies_to = frozenset({BRACHY})
    default_params = {"tolerance_percent": 1.0}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        tol = float(params["tolerance_percent"])
        sources = _sources(ctx.plan)
        for setup in _setups(ctx):
            expected = 0.0
            for ch in setup.get("ChannelSequence", []):
                src = sources.get(int(ch.get("ReferencedSourceNumber", -1)))
                if src is None or "ChannelTotalTime" not in ch:
                    raise CannotEvaluate(
                        "a channel lacks a resolvable source or ChannelTotalTime (see BRACHY-003)"
                    )
                rate = float(src.get("ReferenceAirKermaRate", 0) or 0)
                expected += rate * float(ch.ChannelTotalTime) / 3600.0
            raw = setup.get("TotalReferenceAirKerma")
            actual = float(raw) if raw is not None else None
            deviation = (
                100.0 * (actual - expected) / expected
                if actual is not None and expected > 0
                else math.nan
            )
            yield Check(
                f"Setup {setup.get('ApplicationSetupNumber', '?')} TotalReferenceAirKerma",
                actual is not None and expected > 0 and abs(deviation) <= tol,
                f"{fmt(actual)} uGy" if actual is not None else "(missing)",
                f"{fmt(expected)} uGy +/- {fmt(tol)}%",
                note=f"deviation {deviation:+.3f}%" if not math.isnan(deviation) else "",
            )


@register
class BrachyChannelIntegrity(Rule):
    id = "BRACHY-003"
    title = "Channels and dwell control points are structurally consistent"
    category = "brachy"
    description = (
        "Per channel: unique number, existing source, positive length and total time, the right "
        "number of control points (2N for stepwise movement), indices 0..n-1, cumulative time weight "
        "starting at 0, never decreasing, ending at FinalCumulativeTimeWeight and flat between dwell "
        "pairs, the two control points of a pair at one position, and positions inside the channel. "
        "AAPM MPPG 11.a Table 7 lists channel assignment, index length and first dwell position."
    )
    reference = "DICOM PS3.3 C.8.8.15 Channel Sequence, Brachy Control Point Sequence; AAPM MPPG 11.a Table 7"
    requires = ("rtplan",)
    applies_to = frozenset({BRACHY})
    default_params = {"time_weight_tolerance": 1e-6, "position_tolerance_mm": 0.01}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        wtol = float(params["time_weight_tolerance"])
        ptol = float(params["position_tolerance_mm"])
        sources = _sources(ctx.plan)
        for setup in _setups(ctx):
            channels = list(setup.get("ChannelSequence", []))
            numbers = [int(c.ChannelNumber) for c in channels if "ChannelNumber" in c]
            dup = sorted(n for n, c in Counter(numbers).items() if c > 1)
            sid = setup.get("ApplicationSetupNumber", "?")
            yield Check(
                f"Setup {sid} has channels with unique numbers",
                bool(channels) and not dup,
                dup or f"{len(channels)} channels",
                "unique, >= 1",
            )
            for ch in channels:
                problems: list[str] = []
                cps = list(ch.get("BrachyControlPointSequence", []))
                movement = str(ch.get("SourceMovementType", "")).upper()
                if int(ch.get("ReferencedSourceNumber", -1)) not in sources:
                    problems.append(
                        f"ReferencedSourceNumber {ch.get('ReferencedSourceNumber')} not in SourceSequence"
                    )
                length = float(ch.get("ChannelLength", 0) or 0)
                if length <= 0:
                    problems.append("ChannelLength must be > 0")
                if float(ch.get("ChannelTotalTime", 0) or 0) <= 0:
                    problems.append("ChannelTotalTime must be > 0")
                declared = int(ch.get("NumberOfControlPoints", -1))
                if declared != len(cps):
                    problems.append(f"declares {declared} control points, has {len(cps)}")
                if movement == "STEPWISE" and len(cps) % 2:
                    problems.append(
                        f"{len(cps)} control points is odd for STEPWISE movement (need 2N)"
                    )
                if [int(cp.get("ControlPointIndex", -1)) for cp in cps] != list(range(len(cps))):
                    problems.append("ControlPointIndex is not 0..n-1 in order")
                weights = [float(cp.get("CumulativeTimeWeight", math.nan)) for cp in cps]
                positions = [float(cp.get("ControlPointRelativePosition", math.nan)) for cp in cps]
                if (
                    not weights
                    or any(math.isnan(w) for w in weights)
                    or any(math.isnan(p) for p in positions)
                ):
                    problems.append(
                        "CumulativeTimeWeight/ControlPointRelativePosition missing on some control points"
                    )
                else:
                    final = float(ch.get("FinalCumulativeTimeWeight", weights[-1]))
                    if abs(weights[0]) > wtol:
                        problems.append(f"first time weight is {fmt(weights[0])}, expected 0")
                    if abs(weights[-1] - final) > wtol * max(1.0, abs(final)):
                        problems.append(
                            f"last time weight {fmt(weights[-1])} != FinalCumulativeTimeWeight {fmt(final)}"
                        )
                    if np.any(np.diff(weights) < -wtol):
                        problems.append("cumulative time weight decreases")
                    if movement == "STEPWISE" and len(cps) % 2 == 0:
                        for k in range(0, len(cps), 2):
                            if abs(positions[k] - positions[k + 1]) > ptol:
                                problems.append(
                                    f"dwell {k // 2}: its two control points are at different positions"
                                )
                            if k + 2 < len(cps) and abs(weights[k + 2] - weights[k + 1]) > wtol:
                                problems.append(
                                    f"time accumulates while moving between dwell {k // 2} and {k // 2 + 1}"
                                )
                    if length > 0 and (min(positions) < -ptol or max(positions) > length + ptol):
                        problems.append(
                            f"dwell position outside 0..{fmt(length)} mm channel length"
                        )
                yield Check(
                    f"{_channel_label(setup, ch)} ({len(cps)} control points)",
                    not problems,
                    "consistent" if not problems else f"{len(problems)} problem(s)",
                    "consistent",
                    note=_note(problems),
                )


@register
class SourceDecayAndReferenceDate(Rule):
    id = "BRACHY-004"
    title = "Source strength reference date is not in the future and not stale"
    category = "brachy"
    description = (
        "The planned source strength is valid at the SourceStrengthReference date/time. That must "
        "not be after the plan date, and (warning) should not be many days before it unless the "
        "strength was decay-corrected: AAPM MPPG 11.a Table 7 asks the physicist to check the source "
        "strength against the decay table. The note reports the decay factor between reference and "
        "plan date for the source's half-life. `max_source_age_days` suits Ir-192 on a quarterly "
        "exchange; set it for your isotope and exchange cycle."
    )
    reference = "DICOM PS3.3 C.8.8.15 - Source Strength Reference Date/Time; AAPM MPPG 11.a Table 7"
    default_severity = Severity.WARNING
    requires = ("rtplan",)
    applies_to = frozenset({BRACHY})
    default_params = {"max_source_age_days": 120.0}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        _setups(ctx)
        limit = float(params["max_source_age_days"])
        plan = ctx.plan
        plan_dt = _parse_datetime(plan.get("RTPlanDate"), plan.get("RTPlanTime") or "000000")
        if plan_dt is None:
            raise CannotEvaluate("RTPlanDate missing or malformed")
        for src in plan.get("SourceSequence", []):
            ref_dt = _parse_datetime(
                src.get("SourceStrengthReferenceDate"),
                src.get("SourceStrengthReferenceTime") or "000000",
            )
            label = f"Source {src.get('SourceNumber', '?')}"
            if ref_dt is None:
                yield Check(
                    f"{label} reference date", False, "(missing or malformed)", "a valid date"
                )
                continue
            age = (plan_dt - ref_dt).total_seconds() / 86400.0
            half_life = float(src.get("SourceIsotopeHalfLife", 0) or 0)
            decay = math.exp(-math.log(2.0) * age / half_life) if half_life > 0 else math.nan
            yield Check(
                f"{label} strength reference is {age:.0f} d before the plan date",
                -1.0 / 24 <= age <= limit,
                f"{age:.1f} d",
                f"0..{fmt(limit)} d",
                note=f"decay factor between reference and plan date {decay:.3f}"
                if not math.isnan(decay)
                else "",
            )


@register
class BrachyFractionScheme(Rule):
    id = "BRACHY-005"
    title = "Application setups agree with the fraction scheme"
    category = "brachy"
    description = (
        "Setup numbers must be unique, the fraction group must declare the right number of setups and "
        "reference only setups that exist, and every setup must be delivered by some fraction group. "
        "Otherwise the prescription is attached to an applicator that is not in the plan (or the "
        "plan carries one that is never treated)."
    )
    reference = "DICOM PS3.3 - Referenced Brachy Application Setup Sequence (300C,000A), Application Setup Number (300A,0234)"
    requires = ("rtplan",)
    applies_to = frozenset({BRACHY})

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        setups = _setups(ctx)
        numbers = [int(s.ApplicationSetupNumber) for s in setups if "ApplicationSetupNumber" in s]
        dup = sorted(n for n, c in Counter(numbers).items() if c > 1)
        yield Check("Application setup numbers are unique", not dup, dup or "all unique", "unique")
        referenced: set[int] = set()
        groups = list(ctx.plan.get("FractionGroupSequence", []))
        if not groups:
            yield Check("Plan has a fraction group", False, "no FractionGroupSequence", ">= 1")
            return
        for fg in groups:
            refs = [
                int(r.ReferencedBrachyApplicationSetupNumber)
                for r in fg.get("ReferencedBrachyApplicationSetupSequence", [])
                if "ReferencedBrachyApplicationSetupNumber" in r
            ]
            referenced.update(refs)
            fgn = fg.get("FractionGroupNumber", "?")
            declared = int(fg.get("NumberOfBrachyApplicationSetups", -1))
            yield Check(
                f"Fraction group {fgn}: NumberOfBrachyApplicationSetups matches its references",
                declared == len(refs),
                declared,
                len(refs),
            )
            missing = sorted(set(refs) - set(numbers))
            yield Check(
                f"Fraction group {fgn}: referenced setups exist",
                not missing,
                missing or "all exist",
                "all exist",
            )
        orphans = sorted(set(numbers) - referenced)
        yield Check(
            "Every application setup is delivered by a fraction group",
            not orphans,
            orphans or "all delivered",
            "all delivered",
        )


@register
class DwellTimesAndStepSize(Rule):
    id = "BRACHY-006"
    title = "Dwell times are non-negative and bounded; step sizes are allowed"
    category = "brachy"
    description = (
        "Dwell times derived from the time weights must not be negative or exceed `max_dwell_s`, "
        "and a channel must have at least one dwell position. If `allowed_step_sizes_mm` is set, "
        "every spacing between consecutive dwell positions must be a whole multiple of one of "
        "those steps (AAPM MPPG 11.a Table 7 lists step size, number of dwell positions and first "
        "dwell position among the HDR checks)."
    )
    reference = "AAPM MPPG 11.a Table 7; DICOM PS3.3 C.8.8.15 Control Point Relative Position"
    requires = ("rtplan",)
    applies_to = frozenset({BRACHY})
    default_params = {"max_dwell_s": 600.0, "allowed_step_sizes_mm": [], "step_tolerance_mm": 0.01}

    def evaluate(self, ctx: PlanContext, params: Mapping[str, Any]) -> Iterable[Check]:
        max_dwell = float(params["max_dwell_s"])
        steps = [float(s) for s in params["allowed_step_sizes_mm"]]
        tol = float(params["step_tolerance_mm"])
        for setup in _setups(ctx):
            for ch in setup.get("ChannelSequence", []):
                dwells = dwell_positions(ch)
                problems: list[str] = []
                if not dwells:
                    problems.append("no dwell positions could be derived")
                else:
                    if any(t < -1e-9 for _, t in dwells):
                        problems.append("negative dwell time")
                    if any(t > max_dwell for _, t in dwells):
                        problems.append(
                            f"dwell time above {fmt(max_dwell)} s (max {fmt(max(t for _, t in dwells))} s)"
                        )
                    if steps:
                        pos = [p for p, _ in dwells]
                        gaps = np.abs(np.diff(pos))
                        bad = [
                            float(g)
                            for g in gaps
                            if g > tol
                            and not any(abs(g / s - round(g / s)) * s <= tol for s in steps)
                        ]
                        if bad:
                            problems.append(
                                f"spacing(s) {sorted({round(g, 3) for g in bad})} mm not a multiple of {steps}"
                            )
                observed = (
                    sorted({round(float(g), 3) for g in np.abs(np.diff([p for p, _ in dwells]))})
                    if len(dwells) > 1
                    else []
                )
                yield Check(
                    f"{_channel_label(setup, ch)}: {len(dwells)} dwell positions",
                    not problems,
                    "ok" if not problems else f"{len(problems)} problem(s)",
                    "ok",
                    note=_note(problems) or (f"spacings: {observed} mm" if observed else ""),
                )
