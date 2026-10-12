# Where the rules and limits come from

Every rule in the catalog names its source in its `reference` field, and every built-in ruleset
lists its citations (shown by `rtpg rulesets` and embedded in each report's `ruleset.sources`).
This page states, honestly, **what is official, what is the tool's own encoding, and what is not
covered**. Nothing here is a clinical recommendation; clinical limits remain your institution's.

Rule of thumb used throughout:

* **Structural expectations** (what a valid plan *is*) come from the **DICOM Standard (PS3.3)**.
* **What to review** at initial plan check comes from **AAPM MPPG 11.a** (CC BY 4.0).
* **Numeric clinical limits** are *never* hard-coded in a rule. They live in rulesets, each with a
  citation, and only where an openly readable consensus document states the number.

## Built-in rulesets (`rtpg rulesets`)

| Ruleset | Source | What it encodes |
|---|---|---|
| `default` | DICOM PS3.3; AAPM MPPG 11.a Tables 7-9 | all 51 rules with placeholder thresholds; **no clinical numbers** |
| `aapm_mppg11a` | AAPM MPPG 11.a, J Appl Clin Med Phys 2021;22(9):4-19, doi:10.1002/acm2.13366 | blocking severity for the review items DICOM-RT can verify (approval, prescription vs plan, machine, fractionation, MU, field parameters, electron applicator/cutout, source strength vs decay, dwell step/position) |
| `hytec_brain_srs` | Milano et al., HyTEC, Int J Radiat Oncol Biol Phys 2021;110(1):68-86, doi:10.1016/j.ijrobp.2020.08.013 | normal brain V12Gy <= 10 cc (brain minus target) for single-fraction SRS |
| `tg263_naming` | Mayo et al., AAPM TG-263, Int J Radiat Oncol Biol Phys 2018;100(4):1057-1066 | names must be TG-263 primary names (list supplied locally), <= 16 characters |

### HyTEC: exactly what was taken, and what was not

Read from the author manuscript (PMC9387178), Section 8: the QUANTEC recommendation to limit
single-fraction V12Gy to <= 5-10 cc "remains prudent"; the risk of symptomatic necrosis for V12 =
10 cc is on the order of 15% after SRS for brain metastases; the UK consensus recommends < 12 Gy to
10 cc of brain minus GTV. **Encoded:** V12Gy <= 10 cc (the top of the range; override to 5 cc to be
stricter). **Not encoded, because the paper gives none:** brainstem, optic nerve/chiasm and
cochlear dose-volume limits (the paper explicitly reports no brainstem metrics and defers the optic
pathway to another HyTEC paper). The ruleset therefore has one constraint, not a full SRS table.

### TG-263

The worksheet (717 names in version 20170815; every primary name <= 16 characters, verified) is
read from a local file the user converts with `scripts/tg263_xls_to_txt.py`; it is not bundled
because AAPM states no reuse licence. The ruleset's `additional_allowed_patterns` (GTV/CTV/ITV/PTV
with free suffix; a `z` prefix for planning structures) are **this tool's encoding** of TG-263's
target and planning-structure conventions, *not* worksheet entries: review them before adopting.

## Rule-by-rule sources

### AAPM MPPG 11.a (CC BY 4.0) - which review items map to which rules

| MPPG 11.a item | Table | Rule(s) | Verifiable from DICOM-RT? |
|---|---|---|---|
| Patient name, MRN, DOB | 8 | IDENT-001, IDENT-002 | yes (identity consistency across objects; not against the EMR) |
| Plan approved by physician and physicist; "shall not proceed if revoked" | 7, 8, sec. 3.1.4 | PLAN-001 | yes |
| Prescription matches plan; fractional and total dose agree | 7, 8 | PLAN-004, PLAN-003 | internal consistency only; **not against the EMR** |
| Field parameters reasonable: MU, collimator, gantry, MLC | 8 | BEAM-002, 003, 004, 005, 007, 008 | partially (structural/physical plausibility; thresholds are the site's) |
| Maximum MU for high-dose fields | 8 | BEAM-005 | yes |
| Dose grid includes all critical contours | 8 | DOSE-005 | targets only (critical OARs: roadmap) |
| Treatment scheduled on the correct machine | 8 | PLAN-005 | machine name/whitelist |
| Schedule agrees with prescription | 8 | PLAN-003 | fractions only |
| Plan approval and prescription (brachytherapy) | 7 | PLAN-001, PLAN-004 | yes |
| Source activity / air kerma strength checked against the decay table | 7 | BRACHY-001, 002, 004 | yes (reference date and decay factor; the actual decay table is the site's) |
| Channel assignment, index length/offset, number and first dwell position | 7 | BRACHY-003 | yes |
| Step size | 7 | BRACHY-006 | yes (when allowed steps are configured) |
| Applicator, cone, block code (electron) | 9 | ELEC-001, ELEC-002 | yes |

**Not covered** (cannot be established from DICOM-RT files alone): independent secondary MU/dose
calculation (< 5% difference); collision/beam-clearance check; isocenter vs simulation documents
and skin marks; correct CT dataset and ROI density overrides; couch inclusion beyond a naming
pattern; tolerance table and dose-tracking settings in the record-and-verify system; consent and
patient identification at the console; image co-registration; catheter orientation; treatment
plan optimisation quality. MPPG 11.a **excludes proton plans**, so the ION rules are not
guideline-derived.

### DICOM Standard PS3.3 (NEMA) - structural rules

| Rules | Section read |
|---|---|
| LINK-001..004, INPUT-001..003 | RT General Plan, RT Dose, Frame of Reference, Structure Set modules (UID references) |
| STRUCT-001..005 | Structure Set / ROI Contour / RT ROI Observations modules (incl. contour geometric types) |
| BEAM-001..008 | RT Beams module, Control Point Sequence, Beam Limiting Device attributes |
| ION-001..006 | **C.8.8.25** RT Ion Beams Module and **C.8.8.25.7** Ion Control Point Sequence: two control points per segment; first carries spot weights summing to the meterset step; second carries zeros; spot positions identical within a segment; `Scan Spot Position Map` is 2N values |
| BRACHY-001..006 | **C.8.8.15** RT Brachy Application Setups Module: Source Sequence, Application Setup (Total Reference Air Kerma, uGy at 1 m), Channel Sequence, Brachy Control Point Sequence (control-point count 2N for stepwise movement) |
| ELEC-001..003 | RT Beams module: Applicator Sequence, Block Sequence, Source to Surface Distance |
| DOSE-001..006 | RT Dose module: units/type/summation, grid geometry, scaling |

### Facts established empirically on real files (not from a document)

* **Total Reference Air Kerma = sum(rate x channel time / 3600)** - confirmed on the Varian and
  Elekta exports (5 significant figures); the DICOM text states the definition in words.
* **Dwell time = ChannelTotalTime x delta(CumulativeTimeWeight) / FinalCumulativeTimeWeight** -
  vendors use different weight scales (Varian: seconds; Elekta: arbitrary).
* **Elekta GammaPlan prescription isodose** in private element (0009,1027), creator `ELEKTA
  GAMMAPLAN`: sum(BeamDose) x isodose/100 = prescription on all 8 plans evaluated. Vendor
  documentation was not consulted; the reading is flagged in every report that uses it.

### Numbers that are parameters, not standards

Machine energies, MU ceilings, dose-grid spacing, hotspot (110%) and coverage (D95 >= 95%) screens,
SSD range, maximum snout position, scan-field size and source-age limits are **defaults chosen to
catch gross errors**, not clinical standards. Set them for your machines and protocols (see
`configs/` and the ruleset format in the architecture document).
