# Writing a new rule

A rule is a small class that yields `Check`s. The engine decides PASS/FAIL, handles missing inputs and turns crashes into `ERROR`, so a rule only says *what it compared*.

## 1. Write the rule

Add it to the module for its category (`rules/beams.py`, `rules/dose.py`, …):

```python
@register
class CollimatorAngleReasonable(Rule):
    id = "BEAM-008"  # CATEGORY-NNN; ids are permanent once released
    title = "Collimator angle is within the configured limit"
    category = "beam"  # one of rules.base.CATEGORY_ORDER
    description = "Why this rule exists, in terms a reviewer cares about."
    reference = "RT Beams Module - BeamLimitingDeviceAngle"  # DICOM attribute / policy / guideline
    requires = ("rtplan",)  # engine SKIPS the rule if any of these is not loaded
    default_severity = Severity.WARNING
    default_params = {"max_abs_degrees": 45.0}  # the ONLY keys a ruleset may set

    def evaluate(self, ctx, params):
        beams = treatment_beams(ctx.plan)
        if not beams:
            raise NotApplicable("no treatment beams (see PLAN-002)")
        for beam in beams:
            angle = float(first_control_point(beam).BeamLimitingDeviceAngle)
            yield Check(
                f"{beam_label(beam)} collimator angle",
                ok=min(angle, 360 - angle) <= params["max_abs_degrees"],
                observed=angle,
                expected=f"within {params['max_abs_degrees']} deg of 0",
            )
```

### Contract

* **Yield one `Check` per concrete comparison** (per beam, per ROI, per object). Aggregate only when a unit would produce hundreds of rows (see `STRUCT-004`, which summarises per ROI and puts examples in `note`).
* **Never swallow errors.** Let unexpected exceptions propagate; the engine reports them as `ERROR`.
* **`NotApplicable`** → the plan does not call for this rule. **`CannotEvaluate`** → it does, but the data is missing/unsupported (shows as `SKIPPED`, loudly).
* A rule that yields nothing is an `ERROR`; return/raise explicitly instead.
* Use `ctx.plan`, `ctx.structure_set`, `ctx.dose` (non-optional - declare them in `requires`), `ctx.rois` (parsed contours), `ctx.dose_grid` (decoded Gy grid, raises `CannotEvaluate` if untrustworthy), `ctx.target_geometries(patterns)` (targets resolved to contours) and `ctx.prescription_isodose(...)`.
* Reach beams and control points through `plan_model` (`plan_beams`, `control_points`, `dosimeter_unit`, `brachy_setups`) so the rule works for `BeamSequence` and `IonBeamSequence` plans alike, and set `applies_to` instead of testing the technique by hand.
* Parameters with structure (lists of constraints, per-unit limits) can be validated at ruleset load time by overriding `validate_params` (raise `ValueError` with a clear message).
* Keep `observed`/`expected` JSON-friendly and human-readable; they are printed verbatim in reports.
* Severity heuristic: **error** = the data is inconsistent, impossible or unsafe. **warning** = a clinical/technical judgement threshold.

## 2. Prove it with a defect

Every rule must fail on at least one realistic mistake and stay quiet on the clean case. Add to `synthetic/photon.py` (or the technique's module):

```python
@_defect("extreme_collimator_angle", "Collimator rotated to 90 degrees", {"BEAM-008"})
def _(c: SyntheticCase) -> None:
    _beam_by_number(c, 1).ControlPointSequence[0].BeamLimitingDeviceAngle = 90.0
```

The third argument is the set of rules that must fail **and no others**; add `skips={...}` if the defect legitimately prevents other rules from evaluating, `technique="proton"|"electron"|"brachy"` for non-photon cases (defects live in `synthetic/<technique>.py`), and `rules={...}` when the rule only acts under configuration (a whitelist, an energy range, a dose-volume constraint). An empty failure set declares a *benign variant* that must not be flagged - use it to pin down a real-data quirk that once caused a false positive.

## 3. Regenerate docs and run everything

```bash
make docs                                        # regenerates docs/rule-catalog.md and configs/template_all_options.yaml
make check-all                                   # ruff, mypy, pytest
```

CI fails if the generated docs/template are stale, if a clean case stops passing any applicable rule, or if any rule has no defect covering it (rules proven by dedicated tests - INPUT-002/003, STRUCT-006, DVH-001 - are the explicit exceptions).

## Checklist

- [ ] Unique id, a `description` that explains *why*, a `reference`
- [ ] `requires` lists every object the rule touches
- [ ] Parameters (with defaults) instead of hard-coded thresholds
- [ ] `NotApplicable` / `CannotEvaluate` used instead of guessing
- [ ] Defect added; clean case still passes
- [ ] Optional: an edge-case test in `tests/test_rule_edge_cases.py` for parameters and unsupported data
