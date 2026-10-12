# Safety, limitations & validation guidance

## Intended use

RT-Plan Guardian is a **research and quality-assurance decision-support tool** that screens DICOM-RT exports for cross-object inconsistencies and records the outcome. It is meant to *add* a fast, repeatable check alongside, not instead of, the independent physics plan review and the verification built into your treatment-planning and record-and-verify systems.

It is **not** a medical device, has **not** been cleared or approved by any regulator, and has **not** been clinically validated. Whether and how it may be used in a clinical workflow is a decision for your institution under your local regulations and quality system.

A `PASS` means *the rules in the ruleset, as implemented, found no inconsistency in these files*. It does not mean the plan is clinically appropriate or safe to deliver.

## Known limitations (v0.2)

Scope:
* **Technique maturity differs.** Photon (linac and Gamma Knife) and HDR brachytherapy rules were exercised on real open-access exports from three planning systems ([real-data-evaluation.md](real-data-evaluation.md)). **Electron and proton/ion rules are verified on synthetic cases only**, built from the DICOM Standard and the AAPM electron checklist items, because no openly licensed real plans of those types exist. Validate them on your own anonymised exports before relying on them.
* Beam-based plans of other types (neutron, mixed beams) are flagged by PLAN-002, not checked.
* Brachytherapy is checked structurally (sources, TRAK, channels, dwell pairs, decay, fraction scheme, step size). Applicator geometry, catheter reconstruction against images and dose-volume parameters (e.g. V100/D90) are not evaluated; supply dose-volume constraints through a cited ruleset.
* Prescription arithmetic (`PLAN-004`) is evaluated for a single fraction group and a single prescription level; SIB and multi-phase plans are reported as `SKIPPED`.
* CT images are listed but not read or cross-checked.

Method:
* Dose statistics (`DOSE-004`, `DOSE-006`, `DVH-001`) sample dose-grid **voxel centres**; they are a screen and will differ from the TPS's DVH for small or thin structures.
* Dose grids must be axial (standard orientation) and little-endian uncompressed; otherwise the dose rules are `SKIPPED`.
* Contours are assumed closed planar polygons for volumes; overlapping polygons on one slice combine even-odd.
* `BEAM-006` (isocentre inside target) is a warning because legitimate exceptions exist (e.g. Gamma Knife shots at a target edge).
* **Vendor private elements:** the only one read is the Elekta GammaPlan prescription isodose, an empirical reading that was *not* checked against vendor documentation. It is labelled wherever used and can be disabled.
* Dose-volume limits ship only where an openly readable consensus document states them (HyTEC normal-brain V12Gy). Brainstem, optic-pathway and cochlear limits are **not** included: the source paper gives none. See [standards-provenance.md](standards-provenance.md).
* Thresholds shipped in the default ruleset and `configs/` are **placeholders**, not clinical standards.
* Checks run on what is *in the files*. The tool cannot know that an export is stale relative to the TPS, only that the UID chain and values are inconsistent. AAPM MPPG 11.a items that need other information (secondary dose calculation, collision check, EMR comparison) cannot be checked.

## Failure modes of the tool itself, and how it defends

| Failure mode | Defence |
|---|---|
| Silent pass because data was missing | Rules with absent inputs are `SKIPPED`; `INPUT-001` fails when a required object is absent |
| Silent pass because a rule crashed | Crash ⇒ `ERROR` ⇒ verdict `INCOMPLETE`; zero-check rules are `ERROR` |
| Silent pass because a rule was disabled or mistyped | Disabled rules are listed as `DISABLED`; unknown rule ids/parameters are load errors |
| Misreading data it does not understand | Unsupported orientation/units/encoding ⇒ `SKIPPED`, never a best guess |
| A rule that cries wolf or misses its target | Fault-injection matrix: exact failing-rule sets asserted per defect; clean case must pass everything |
| Report tampering / mismatch | Input and ruleset SHA-256, resolved parameters and a content hash are embedded |

## Patient data handling

* **Reports contain PHI** (patient ID, name, birth date, UIDs). Store and share them as PHI.
* The Streamlit app processes uploads **in memory only**, does not write them to disk and does not use Streamlit caching for them. If you deploy it, put it behind your institution's authentication and over TLS; it ships with neither.
* HTML reports make **no external requests** and embed all styling, so they can be opened offline.
* Loader records only base file names (directory names often carry patient identifiers).
* Never commit DICOM files or generated reports (`.gitignore` blocks `*.dcm`, `reports/` and `open_data/`), and never paste real identifiers into issues or pull requests. Tests and demos use synthetic data generated in code; the optional real-data tests use only openly licensed, de-identified datasets fetched on demand ([data-sources.md](data-sources.md)).

## If you intend to use it clinically

None of this is clinical advice; it is a sensible starting point for a validation plan owned by a qualified medical physicist:

1. **Own the ruleset.** Review every default; encode your policy in a versioned, access-controlled YAML file. The report records its hash.
2. **Pin versions.** Record the tool version and (for reproducibility) the pydicom/numpy versions; the report includes them.
3. **Validate on known-bad data.** Reproduce the failure modes you care about (your own historical near-misses) as test cases, in the spirit of `synthetic.py`, and confirm each is caught.
4. **Validate on known-good data.** Run representative approved plans from each technique and machine; investigate every `FAIL`/`WARN`/`SKIPPED` to confirm the rule is right and the threshold sensible.
5. **Decide what a non-`PASS` means procedurally** (who reviews, who may waive, where waivers are recorded) before the tool is in the workflow. Waivers are not yet a feature of the tool (roadmap).
6. **Re-validate on change:** TPS upgrade, new export settings, new machine, new tool version.
7. **Consider a risk-based approach** (e.g. FMEA as described in AAPM TG-100) to decide which failure modes justify an automated check and what residual checks remain manual.

## Further reading

* AAPM MPPG 11.a: *Plan and chart review in external beam radiotherapy and brachytherapy* (J Appl Clin Med Phys 2021;22(9):4-19, doi:10.1002/acm2.13366; CC BY 4.0).
* AAPM TG-275: *Strategies for effective physics plan and chart review in radiation therapy* (Ford et al., Med Phys 2020).
* HyTEC: Milano et al., *Single- and multifraction stereotactic radiosurgery dose/volume tolerances of the brain* (Int J Radiat Oncol Biol Phys 2021;110(1):68-86).
* AAPM TG-263: *Standardizing nomenclatures in radiation oncology* (Mayo et al., 2018).
* AAPM TG-100: *Application of risk analysis methods to radiation therapy quality management* (Huq et al., Med Phys 2016).
* DICOM PS3.3 - Information Object Definitions: RT Plan, RT Structure Set and RT Dose IODs and modules.

## Reporting problems

If you find a case where the tool passes something it should not, or fails something valid, please open an issue **with a synthetic reproduction only** (the `synthetic/` builders make this easy) - never with real patient files.
