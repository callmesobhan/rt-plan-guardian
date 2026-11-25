# Vendor-Neutral AAPM TG-275 / MPPG 11.a Commissioning Harness

Commercial automated plan checkers (**Radformation ClearCheck**, **SunCHECK PlanCHECK**, **Mobius3D**, **Eclipse ESAPI scripts**, **RayStation scripts**) must be commissioned before clinical deployment to verify both **sensitivity** (catching real delivery/consistency hazards) and **specificity** (not crying wolf on clean plans).

Historically, medical physicists have had to clone and manually corrupt exported clinical plans one by one. `rtpg commission` automates that entire workflow for **any** DICOM-RT plan checker.

## Workflow

### 1. Generate a UID-clean, PHI-stripped commissioning suite from your clinic's own anonymized plan (or built-in synthetic baselines)

```bash
# From your own anonymized Eclipse / RayStation / Monaco / Oncentra export:
rtpg commission generate /path/to/anonymized_clinical_plan --out tg275_suite --blind

# Or across all 4 modalities (photon, electron, proton/ion, brachytherapy):
rtpg commission generate --technique all --out tg275_suite_all
```

For every applicable mutation in the 32-item catalogue (`rtpg commission list`), the harness:
1. Deep-copies the DICOM-RT triad (`RTPLAN`, `RTSTRUCT`, `RTDOSE`).
2. **Re-mints fresh internally consistent UIDs** (`SOPInstanceUID`, `StudyInstanceUID`, `SeriesInstanceUID`, `FrameOfReferenceUID`, and cross-object references) so importing all mutants into a QA sandbox or commercial checker never triggers DICOM UID collisions.
3. Strips patient PHI by default (pass `--keep-phi` only in isolated sandboxes).
4. Injects obvious TG-275 defects, clinically subtle "silent killers" (e.g., 1.0 mm opposing MLC leaf overlap, stale plan/structure UID linkage, half-voxel RTDOSE origin shift, +10% brachy TRAK drift, proton spot-weight drift), and negative controls (`control_clean_clone`, `control_benign_metadata_edit`).
5. Writes `manifest.json` (with cryptographic SHA-256 hashes) and `results_template.csv`.

### 2. Run your commercial or in-house plan checker on `tg275_suite/cases/*`

Record `true` / `false` in `results_template.csv` (or run any CLI/scripted checker directly via `--runner-cmd`).

### 3. Score the checker and issue a signed TG-275 commissioning certificate

```bash
# Score external results (ClearCheck, SunCHECK, Mobius3D, ESAPI):
rtpg commission score tg275_suite \
  --results tg275_suite/results_template.csv \
  --checker "Radformation ClearCheck" \
  --checker-version "2.9" \
  --out tg275_certificate

# Or score any CLI checker automatically:
rtpg commission score tg275_suite \
  --runner-cmd "my_checker_cli {case_dir}" \
  --checker "Clinic ESAPI Checker" \
  --out tg275_certificate

# Or self-commission RT-Plan Guardian itself:
rtpg commission score tg275_suite --self-test --out rtpg_certificate
```

The certificate reports:
* Sensitivity, Specificity, High-Risk TG-275 Recall, Obvious vs. Subtle Defect Recall, and F1 with **exact Wilson 95% confidence intervals**.
* A **False-Negative Blind-Spot Register** naming every missed hazard and its TG-275 reference.
* A **False-Positive Register** naming any negative control that triggered a false alarm.
* Cryptographic `suite_sha256` and `content_sha256` hashes in **JSON**, **Markdown**, and **print-ready self-contained HTML**.
