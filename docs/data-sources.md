# Data sources, licences and attribution

RT-Plan Guardian's own tests need **no patient data**: they use synthetic plans built in code.
For the real-data evaluation ([real-data-evaluation.md](real-data-evaluation.md)) the project uses
only openly licensed datasets that accompany open-access publications. They are **never committed**
to this repository (`.gitignore` blocks DICOM files and `open_data/`); fetch them yourself:

```bash
python scripts/fetch_open_data.py --list
python scripts/fetch_open_data.py gamma_knife brachy_breast tg263 --dest open_data --yes
RTPG_REAL_DATA=$PWD/open_data pytest -m realdata
```

The script prints each dataset's licence and citation, verifies SHA-256 checksums where the file is
fixed, extracts archives with path-traversal and size checks, and refuses to run without `--yes`.

## Datasets used

| Key | Dataset | Licence | Size | Used for |
|---|---|---|---|---|
| `gamma_knife` | TCIA **Vestibular-Schwannoma-SEG**, patients VS-SEG-001..004: RTPLAN, RTDOSE, RTSTRUCT (Elekta GammaPlan) | CC BY 4.0 | ~35 MB | non-linac radiosurgery plans; multi-plan folders; isodose prescriptions |
| `brachy_breast` | Zenodo **Interstitial HDR breast** (record 18496579): Varian and Elekta TPS and Monte-Carlo reference cases (192Ir, multi-catheter) | CC BY 4.0 | 148 MB | brachytherapy plans from two vendors |
| `tg263` | AAPM **TG-263 nomenclature worksheet** 20170815 (.xls) | AAPM states no reuse licence | 1.7 MB | STRUCT-006 naming check; kept local, **not redistributed** |

### Citations (please cite these if you reuse the data)

* Shapey J, Kujawa A, Dorent R, Wang G, et al. *Segmentation of vestibular schwannoma from MRI, an
  open annotated dataset and baseline algorithm.* Scientific Data 2021;8:286.
  doi:[10.1038/s41597-021-01064-w](https://doi.org/10.1038/s41597-021-01064-w) (open access).
  Data: doi:[10.7937/TCIA.9YTJ-5Q73](https://doi.org/10.7937/TCIA.9YTJ-5Q73).
* Clark K, et al. *The Cancer Imaging Archive (TCIA): maintaining and operating a public
  information repository.* J Digit Imaging 2013;26:1045-1057 (TCIA's required citation).
* Peppa V, Thomson R, Enger S, Paiva Fonseca G, Lee C, Lucero J, Mourtada F, Siebert F-A, Vijande J,
  Papagiannis P. *A MC-based anthropomorphic test case for commissioning model-based dose
  calculation in interstitial breast 192-Ir HDR brachytherapy.* Medical Physics 2023;50.
  doi:[10.1002/mp.16455](https://doi.org/10.1002/mp.16455). Data: Zenodo
  doi:[10.5281/zenodo.18496579](https://doi.org/10.5281/zenodo.18496579).
* Mayo CS, Moran JM, Bosch W, et al. *AAPM Task Group 263: Standardizing nomenclatures in radiation
  oncology.* Int J Radiat Oncol Biol Phys 2018;100(4):1057-1066.
  doi:[10.1016/j.ijrobp.2017.12.013](https://doi.org/10.1016/j.ijrobp.2017.12.013) (executive summary).

## Datasets looked at and not used

| Dataset | Why not |
|---|---|
| TCIA **NRG-1308** (photon vs proton, 12 patients, includes RT Ion plans) | limited access: requires a request to TCIA; licence "TCIA Restricted" |
| TCIA **HNSCC / Head-Neck-CT-Atlas** (has RTPLAN/RTDOSE/RTSTRUCT) | image data under NIH controlled-access policy, not retrievable anonymously |
| TCIA **HNC-IMRT-70-33** | DICOM data listed as TCIA Restricted |
| DoseRAD2026 (photon + proton dose calculation) | CC BY-NC; not RT-Ion DICOM objects (CT/MRI + dose maps + JSON) |
| Erasmus **TROTS** (protons) | MATLAB/HDF5, not DICOM |
| Zenodo **TPS Data Ringtrial Single-Isocenter Multitarget SRS 2026** (23 centres, linac SRS, CC BY 4.0, 1.6 GB) | open and highly relevant; not downloaded in this evaluation (recommended next) |

**Consequence:** there is no real open proton or electron plan data that this project could use, so
the ION and ELEC rules are verified on synthetic cases built from the DICOM Standard only. Treat
them as less proven than the photon, Gamma Knife and brachytherapy rules, and validate them on
your own anonymised exports before relying on them.

## Handling downloaded data

Downloaded files and archives are untrusted input. The fetch script extracts each archive into its
own new directory with safety checks. When you review them, keep the data outside the source tree
and run the interpreter isolated (`python -I -m rt_plan_guardian.cli ...`) so a stray file next to
the data can never be imported as code. Reports contain the patient identifiers present in the
files; these datasets are de-identified by their publishers, but treat any report from real
clinical data as PHI.
