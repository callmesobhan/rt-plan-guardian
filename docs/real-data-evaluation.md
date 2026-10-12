# Evaluation on real open-access data

RT-Plan Guardian v0.1 was written and tested only against synthetic photon plans. This page records
what happened when the tool met **real exports from three treatment-planning systems**, what it
found, which of its own assumptions broke, and what was changed. Everything here is reproducible:
[data-sources.md](data-sources.md) lists the datasets, licences and citations, and
`tests/realdata/` turns every statement below into a regression test.

> These are research and benchmark datasets. Findings describe the *files*, not anyone's clinical
> judgement, and several are properties of how benchmark cases were assembled. Nothing here is a
> claim about clinical safety or about any vendor's product.

## Data and method

| Dataset | Objects | Systems |
|---|---|---|
| TCIA *Vestibular-Schwannoma-SEG*, patients VS-SEG-001 to 004 (CC BY 4.0) | 8 plans + 8 doses + 8 structure sets (two of each per patient) | Elekta GammaPlan (Gamma Knife radiosurgery) |
| Zenodo *Interstitial HDR breast* (CC BY 4.0) | 4 cases: {Varian, Elekta} x {TPS dose, Monte-Carlo reference dose}; 80 CT slices each (ignored) | Varian ARIA/BrachyVision, Nucletron Oncentra (192Ir HDR) |

No proton, electron or multi-vendor linac data could be used: none exists openly with RT plan
objects (TCIA's proton collection NRG-1308 is access-restricted; the public TCIA API exposes RTPLAN
objects in a single collection). Ion and electron support is therefore verified synthetically only.

Each folder was reviewed with the CLI, in an isolated interpreter, with the data outside the repo:

```bash
python -I -m rt_plan_guardian.cli check open_data/gamma_knife/VS-SEG-001 --out reports/
python -I -m rt_plan_guardian.cli check "open_data/brachy_breast/Breast_Varian/Reference TPS"
```

## Before and after

"Before" is the unmodified v0.1 photon tool (for Gamma Knife the files had to be paired by hand,
because v0.1 refused a folder holding two plans). "After" is the current tool.

| Case | Before (v0.1) | After |
|---|---|---|
| VS-SEG-001 (folder with 2 plans) | rejected; a hand-picked pair gave **FAIL** (4 errors, 1 warning, 5 rules skipped) | 2 cases assembled by UID; both **PASS_WITH_WARNINGS** |
| VS-SEG-002 / 003 / 004 | not run | 6 cases, all **PASS_WITH_WARNINGS** |
| Varian TPS brachytherapy | **FAIL** (2 errors) | **PASS_WITH_WARNINGS** |
| Elekta TPS brachytherapy | **FAIL** (1 error) | **PASS_WITH_WARNINGS** (1 rule SKIPPED: no prescription in plan) |
| Varian MC brachytherapy | **FAIL** (3 errors) | **FAIL** (2 errors, both genuine - see below) |
| Elekta MC brachytherapy | **FAIL** (1 error) | **PASS_WITH_WARNINGS** (1 rule SKIPPED) |

Every "before" error that disappeared was a false positive caused by a photon-linac assumption.
The two that remain are real inconsistencies inside the published benchmark files.

## What real data broke, and the fix

| # | Symptom on real data | Cause (my assumption) | Fix |
|---|---|---|---|
| 1 | A folder with 2 plans/doses/structure sets was rejected | one object of each kind per run | Cases assembled by the UID references the files carry; unlinkable objects are reported (INPUT-002), never guessed |
| 2 | MU limit failed (0.9 *minutes*) | beam meterset assumed to be MU | Limits per dosimeter unit (MU, MINUTE, NP) |
| 3 | 19/19 beams failed "MLC/jaw positions" | Gamma Knife exports X/Y collimators with 0 jaw pairs | Physical checks (BEAM-007) independent of declaration; conformance split into BEAM-008 (warning) |
| 4 | 80 of 89 contours "malformed" (brachytherapy) | all contours assumed closed planar polygons | `OPEN_NONPLANAR` catheters and `POINT`s validated against their own geometry type |
| 5 | "No treatment beams" on every brachytherapy plan | all plans assumed to have beams | Technique-aware rules (`applies_to`); PLAN-002 requires beams *or* application setups |
| 6 | Prescription unresolved, 5 rules skipped | targets assumed to be VOLUME references to an ROI | SITE/POINT references matched to an ROI by description ("A:TV" -> TV, "PTV"); unresolved targets say so |
| 7 | Prescription arithmetic off by +117% on all 8 Gamma Knife plans | assumed sum(BeamDose) is the prescription dose | Radiosurgery prescribes to an isodose line; see below |
| 8 | Hotspot 213% of prescription | same | Hotspot judged against the 100% (maximum-dose) level |
| 9 | Elekta source "SourceStrength must be > 0" | misread DICOM: `SourceStrengthUnits=AIR_KERMA_RATE` carries its value in `ReferenceAirKermaRate` | Strength field chosen by units; regression defect added |
| 10 | 80 CT file names printed per case | one line per ignored file | Ignored files grouped by reason |
| 11 | Default "require a PTV and a BODY ROI" failed on `TV` | policy baked into the default ruleset | Default is empty; naming is the opt-in STRUCT-006 / site ruleset |

## Findings on the Gamma Knife plans (8 plans, 4 patients)

* **Prescription isodose, verified exactly.** For all 8 plans, sum(BeamDose) x isodose% / 100
  equals the prescription: 28.261 x 46% = 13.0 Gy and 26.087 x 46% = 12.0 Gy (VS-SEG-001, 002),
  26.000 x 50% = 13.0 Gy (003, 004). The isodose is stored in a *private* element
  `(0009,1027)` of block `ELEKTA GAMMAPLAN`. **This is an empirical reading, not vendor
  documentation**; every report that uses it says so, it can be switched off
  (`use_vendor_isodose: false`) or replaced (`prescription_isodose_percent`), and PLAN-004 re-checks
  the identity, so a misreading would fail loudly.
* **No blocking failure** on any plan. Recurring warnings: ROI interpreted types are empty on all 8
  structure sets (STRUCT-005); the X/Y collimators are exported with 0 pairs (BEAM-008).
* **BEAM-006:** one shot of 8 (VS-SEG-002 plan ...737322) and two of 10 (VS-SEG-003 plan
  ...135877) have an isocentre outside the target contour. Plausible for shots at a target edge;
  flagged for a human, as intended.
* **DOSE-006 (target D95 < 95% of prescription), 3 plans.** VS-SEG-004 both plans: 92.0% and
  93.3%. **VS-SEG-001 plan ...470263 is an anomaly in the published data:** its RTDOSE peaks at
  10.75 Gy, 40 mm from the plan's own isocentres, and delivers 0.01 Gy to the target, whereas the
  other dose of the same patient peaks at 27.7 Gy within 1 mm of that plan's dose-specification point.
  The tool surfaced this only because it checks plan, structure set and dose against each other.

## Findings on the brachytherapy cases (4 cases, 2 vendors)

* **Total Reference Air Kerma identity holds** on both vendors: stored 1962.60 / 1962.64 uGy vs
  rate x time / 3600 to within 0.1% (BRACHY-002). Channel/dwell structure, source definitions,
  step sizes and the fraction scheme pass (BRACHY-001, 003, 005, 006). Vendors differ in the
  scale of the cumulative time weight (Varian: seconds; Elekta: arbitrary), and the dwell-time
  derivation handles both.
* **BRACHY-004 flags all four:** source strength reference dates are 322 d (Varian TPS), 121 d
  (Varian MC) and ~1200 d (Elekta) before the plan date. These benchmark cases reuse a fixed
  nominal source strength; the warning is correct, and benign here.
* **Varian MC case, genuine inconsistencies:** its RTDOSE references a plan UID that is not the
  supplied MC plan (LINK-002), and the MC plan states a 1 Gy prescription against 4.3 Gy per
  application setup (PLAN-004).
* **Elekta plans carry no prescription at all** (PLAN-004 SKIPPED rather than passed).
* All four are `UNAPPROVED` (PLAN-001 warning), as expected for test cases.

## Not covered by real data

Proton and electron plans (synthetic only); multi-vendor linac IMRT/VMAT plans; sequential
(multi-phase) and SIB prescriptions; CT linkage; per-beam dose files. A 23-centre linac SRS ring
trial (Zenodo 10.5281/zenodo.18257315, CC BY 4.0) would exercise several of these and is the
recommended next dataset.
