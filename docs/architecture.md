# Architecture & design principles

## Goals

1. **Catch cross-object inconsistencies** that single-file validators cannot see.
2. **Leave an audit trail** a physicist can sign: what was checked, against what, with what result.
3. **Fail safe.** The worst outcome for a QA tool is a silent pass. Every design choice below serves that.
4. **Work across techniques** (photon, electron, proton/ion, brachytherapy) without special-casing in every rule.
5. **Be provable.** Correctness is established by fault injection on synthetic cases *and* by regression tests on real open-access exports.

## Data flow

```
files/folders ──► loader ──► case assembly ──► PlanContext (one per case) ──► engine ──► ReviewReport ──► JSON | Markdown | HTML
                   │              │                   ▲                          ▲              │
        SOP-class classification  │       cached ROI geometry,             ruleset (YAML)       └─► CLI exit code / Streamlit
        SHA-256 per file          │       decoded dose grid, technique     extends, citations
                    UID links: plan→structure set, dose→plan; unassigned objects reported
```

| Layer | Module | Responsibility |
|---|---|---|
| Intake | `loader.py` | Classify by **SOP Class UID** (never by file name), hash raw bytes, treat RT Plan and RT Ion Plan as plans, list non-RT files as *ignored with a reason* (grouped), header-only peek keeps large CT folders cheap. |
| Case assembly | `loader.assemble` | One of each kind supplied → one case, taken at face value (a mismatched dose is *reported*, never repaired). Several of any kind → cases built by the UID references the files carry; what cannot be linked is `unassigned`. |
| Plan model | `plan_model.py` | One technique-aware view: beams (`BeamSequence` or `IonBeamSequence`), control points, brachy setups, dosimeter unit, vendor isodose adapter. |
| Context | `context.py`, `geometry.py`, `dvh.py` | Read-only `PlanContext` per case. Lazily caches parsed contours and the decoded dose grid; resolves *targets* (plan-declared, matched to ROIs by number or description, ROI-name regex fallback) and prescription isodose; dose-volume metrics. |
| Rules | `rules/*.py` | Small stateless classes yielding `Check`s. No I/O, no global state. |
| Config | `config.py`, `data/rulesets/` | Strict YAML → resolved per-rule settings; `extends`; citations; built-in named rulesets. |
| Engine | `engine.py` | Runs rules, applies status semantics and technique applicability, computes verdict and content hash. |
| Output | `report/`, `cli.py`, `app.py` | Render the one `ReviewReport.to_dict()` payload. The Streamlit app adds no logic. |

## Techniques and applicability

DICOM stores photon/electron plans in `BeamSequence` (RT Plan), ion plans in `IonBeamSequence` (RT
Ion Plan) and brachytherapy in `BrachyApplicationSetupSequence`. Rules use the `plan_model`
accessors, never those sequences directly, and declare `applies_to`:

| `applies_to` | Meaning |
|---|---|
| *(empty)* | every plan (identity, linkage, structures, approval, fractions, prescription, dose integrity) |
| `EXTERNAL_BEAM` | photon, electron, proton, ion: control points, angles, energy, meterset, hotspot/coverage |
| `PHOTON_ONLY` | MLC/jaw checks |
| `CHARGED_PARTICLE` | the ION-* rules |
| `{electron}` / `{brachy}` | ELEC-* / BRACHY-* |

A plan's techniques come from what it contains (a beam's `RadiationType`, application setups). A
rule that does not apply is `NOT_APPLICABLE` with the reason ("rule is for photon plans; this plan
is brachy"). One universal rule, PLAN-002, fails when a plan has *no* deliverable content, so an
empty plan cannot pass by being "not applicable" everywhere.

## Status semantics (the heart of the fail-safe design)

| Status | Meaning | Affects verdict? |
|---|---|---|
| `PASS` | All checks passed | no |
| `FAIL` | At least one check failed | **yes** - `error` severity → `FAIL`; `warning` → `PASS_WITH_WARNINGS` |
| `NOT_APPLICABLE` | The rule does not apply to this plan (technique, nothing configured) | no |
| `SKIPPED` | Applies, but could not be evaluated: object missing, grid rotated, units relative, multiple prescriptions, target not linked to an ROI... | no - but listed prominently in every report |
| `ERROR` | The rule crashed, or yielded zero checks | **yes** - verdict `INCOMPLETE` at best |
| `DISABLED` | Switched off by the ruleset | no - but listed, so a disabled check is visible |

Rules choose between the two "didn't run" outcomes by raising `NotApplicable` (a statement about the *plan*) or `CannotEvaluate` (a statement about the *data*). A rule never needs to catch its own errors: an unexpected exception becomes `ERROR`, never `PASS`.

**Verdict precedence:** any error-severity `FAIL` → `FAIL`; else any `ERROR` → `INCOMPLETE`; else any warning-severity `FAIL` → `PASS_WITH_WARNINGS`; else `PASS`.

Missing inputs are handled by two cooperating mechanisms: `INPUT-001` fails when an object the ruleset **requires** is absent, while every rule that `requires` an absent object is `SKIPPED`.

## Cases

* **Exactly one of each kind** → one case. Objects are *not* matched by UID, so a dose from another plan is reported by LINK-002 instead of silently paired.
* **More than one of any kind** (real exports often hold several plans/doses/structure sets) → each RTPLAN becomes a case with the structure set it references and the doses that reference it. Objects that cannot be linked appear as `unassigned` in every case's report and fail INPUT-002 (warning). Several doses per plan are allowed: the plan-summation dose is the case dose; INPUT-003 warns if that choice is ambiguous.
* Reports are written per case (`<out>/<case_id>/`) with an `index.json`; the CLI exit code is the worst over cases.

## Rulesets

* Every registered rule runs even if the file does not mention it.
* Unknown rule ids, unknown parameter names, wrong parameter types and bad severities raise `ConfigError`; rules may add deep validation (`validate_params`), e.g. every dose-volume constraint must parse and carry a `source`.
* A ruleset may `extends` a built-in name or a file (cycles rejected); the child overrides per rule and per parameter.
* The raw file's SHA-256, its parents' SHA-256 and the **fully resolved** settings of every rule, plus the `sources` citations, are embedded in the report, so the report alone proves which thresholds applied and where they came from.
* Numeric clinical limits live only in rulesets (see [standards-provenance.md](standards-provenance.md)); rules carry defaults chosen to catch gross errors.

## Reproducibility

`ReviewReport.content_sha256` hashes the canonical JSON of: schema version, tool version, resolved ruleset (with lineage), case id and techniques, input file hashes/UIDs, verdict, summary and every result. It deliberately **excludes** the timestamp and runtime environment, so identical inputs + ruleset + tool version always produce an identical hash, and any change to any of them changes it.

## Geometry conventions & assumptions

Stated here because they are where a screening tool can be silently wrong:

* Patient (DICOM) coordinates in mm. ROI volumes are closed planar polygons on axial slices; overlapping polygons on one slice combine **even-odd** (an inner ring is a hole). Open non-planar contours (brachytherapy catheters) and POINTs are validated but are not volumes.
* A contour slice represents a slab ± half the median slice spacing around its z.
* Dose grids must be axial with `ImageOrientationPatient = 1,0,0,0,1,0` (small numeric noise is tolerated); anything else is `SKIPPED`, never misread. Frame order may be ascending or descending.
* Dose is decoded from `PixelData × DoseGridScaling`, compared in absolute terms only when `DoseUnits = GY`.
* Dose statistics sample **voxel centres** inside the contour: a consistency screen, not a planning system's DVH engine.
* **Prescription isodose:** linac plans prescribe at 100%; radiosurgery prescribes to an isodose line and exports `BeamDose` at the maximum-dose point. The isodose comes from the ruleset or from a vendor-specific private element (Elekta GammaPlan; an *empirical* reading, labelled in every report that uses it, switchable, and cross-checked by PLAN-004).
* Frame of Reference is compared as UIDs; no registration or transform is applied.

## Extension points

* **New rule:** add a class in `rules/` and a defect in `synthetic/` - see [writing-rules.md](writing-rules.md).
* **New technique:** add `applies_to` constants, a synthetic builder, rules and defects.
* **Site policy / protocol:** a ruleset YAML; no code for thresholds, whitelists, severities, constraints or enabling/disabling.
* **New report format:** write a renderer over `ReviewReport.to_dict()`; do not read rule internals.

## Deliberate non-goals

Replacing the TPS, recomputing dose, machine-specific deliverability (MLC speed, gantry speed, dose rate), network/PACS integration, and any write-back to clinical systems. The tool is read-only by construction: it never modifies or transmits DICOM.
