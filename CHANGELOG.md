# Changelog

## 0.2.0

**Beyond photons, and proven on real data.**

* **Techniques:** electron (ELEC-001..003), proton/ion via RT Ion Plan (ION-001..006) and
  brachytherapy (BRACHY-001..006), with technique-aware applicability (`applies_to`). 51 rules.
* **Cases:** a folder with several plans/doses/structure sets is assembled into cases by UID
  references; unlinkable objects are reported (INPUT-002); several doses per plan (INPUT-003).
* **Rulesets:** `extends`, per-ruleset `sources` citations, named built-ins (`rtpg rulesets`):
  `aapm_mppg11a`, `hytec_brain_srs`, `tg263_naming`; dose-volume constraints from data (DVH-001),
  TG-263 naming rule (STRUCT-006).
* **Real-data evaluation** on open-access Gamma Knife (TCIA) and HDR brachytherapy (Zenodo)
  exports from three planning systems; 11 wrong photon-linac assumptions found and fixed - see
  `docs/real-data-evaluation.md`. Fetch script, licences and citations in `docs/data-sources.md`.
* **Changed (rulesets):** BEAM-004 `allowed_energies_mv` -> `allowed_energies` (per radiation
  type); BEAM-005 `min_mu`/`max_mu` -> `limits` (per dosimeter unit); BEAM-007 split into
  BEAM-007 (physical) and BEAM-008 (declaration conformance, warning); PLAN-002 now requires beams
  *or* application setups; the default ruleset no longer requires PTV/BODY ROI names.
* **New:** prescription isodose handling for radiosurgery (PLAN-004, DOSE-004), report fields
  `case_id`, `techniques`, `unassigned_files`, `ruleset.extends/sources/parents_sha256`.

## 0.1.0

Initial release: photon plan consistency checks, strict YAML rulesets, JSON/Markdown/HTML
reports, Streamlit app, synthetic fault-injection test-suite.
