#!/usr/bin/env python3
"""Fetch the open-access DICOM-RT datasets used to evaluate RT-Plan Guardian on real data.

    python scripts/fetch_open_data.py --list
    python scripts/fetch_open_data.py gamma_knife brachy_breast --dest ./open_data --yes

Nothing is downloaded without --yes. Data is written under --dest (default ./open_data, which is
git-ignored) and must never be committed. Every dataset prints its licence and the citation you
must give. Zips are extracted with path-traversal and size checks, each into its own directory.

Layout produced (what tests/realdata and docs/real-data-evaluation.md expect):
    open_data/gamma_knife/VS-SEG-00N/<MODALITY>_<n>/*.dcm
    open_data/brachy_breast/Breast_{Varian,Elekta}/{Reference TPS,Reference MC}/*.dcm
    open_data/tg263/TG263_Nomenclature_Worksheet_20170815.xls         (local use only)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

NBIA = "https://services.cancerimagingarchive.net/nbia-api/services/v1"
DATASETS = {
    "gamma_knife": {
        "title": "TCIA Vestibular-Schwannoma-SEG: Gamma Knife RTPLAN/RTDOSE/RTSTRUCT, patients 001-004",
        "size": "about 35 MB download",
        "licence": "CC BY 4.0",
        "citation": (
            "Shapey J et al. Segmentation of vestibular schwannoma from MRI, an open annotated dataset "
            "and baseline algorithm. Sci Data 2021;8:286. doi:10.1038/s41597-021-01064-w. "
            "Data: doi:10.7937/TCIA.9YTJ-5Q73. Clark K et al. The Cancer Imaging Archive (TCIA). "
            "J Digit Imaging 2013;26:1045-1057."
        ),
    },
    "brachy_breast": {
        "title": "Zenodo 10.5281/zenodo.18496579 'Interstitial HDR breast' (Varian and Elekta TPS)",
        "size": "148.4 MB download (57.2 + 91.3 MB zips)",
        "licence": "CC BY 4.0",
        "citation": (
            "Peppa V, Thomson R, Enger S, Paiva Fonseca G, Lee C, Lucero J, Mourtada F, Siebert F-A, "
            "Vijande J, Papagiannis P. A MC-based anthropomorphic test case for commissioning model-based "
            "dose calculation in interstitial breast 192-Ir HDR brachytherapy. Med Phys 2023;50. "
            "doi:10.1002/mp.16455. Data: Zenodo doi:10.5281/zenodo.18496579."
        ),
        "files": {
            "Breast_Varian_dataset.zip": "7e0ba6701017a59d7ca7fcb79f8f688fc0297c3ea6db84aa6f5fd784f5743e19",
            "Breast_Elekta_dataset.zip": "271909260c30cf08b2ec7be605cd68439fd86ae0aaf89cc90638c0089f10c262",
        },
    },
    "tg263": {
        "title": "AAPM TG-263 nomenclature worksheet (20170815, .xls)",
        "size": "1.7 MB",
        "licence": "AAPM states no reuse licence: keep local, do not redistribute",
        "citation": "Mayo CS et al. AAPM TG-263. Int J Radiat Oncol Biol Phys 2018;100(4):1057-1066.",
        "sha256": "5ff0b9e2ebf578793f6fa8f59c2357b3feb61fdc0f87171e9103a0495d93d150",
    },
}


def _get(url: str, timeout: int = 600) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _safe_extract(zip_path: Path, out: Path, max_member_mb: int = 200) -> None:
    out.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if member.filename.startswith("/") or ".." in Path(member.filename).parts:
                raise SystemExit(f"refusing unsafe path in archive: {member.filename}")
            if member.file_size > max_member_mb * 10**6:
                raise SystemExit(f"refusing oversized member: {member.filename}")
        zf.extractall(out)


def _verify(path: Path, expected: str) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise SystemExit(f"checksum mismatch for {path.name}: {digest} != {expected}")


def fetch_gamma_knife(dest: Path) -> None:
    root = dest / "gamma_knife"
    for pid in ("VS-SEG-001", "VS-SEG-002", "VS-SEG-003", "VS-SEG-004"):
        query = urllib.parse.urlencode(
            {"Collection": "Vestibular-Schwannoma-SEG", "PatientID": pid, "format": "json"}
        )
        series = json.loads(_get(f"{NBIA}/getSeries?{query}"))
        for n, s in enumerate(
            x for x in series if x["Modality"] in ("RTPLAN", "RTDOSE", "RTSTRUCT")
        ):
            target = root / pid / f"{s['Modality']}_{n:02d}"
            zip_path = root / pid / f"{s['Modality']}_{n:02d}.zip"
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            zip_path.write_bytes(
                _get(f"{NBIA}/getImage?SeriesInstanceUID={s['SeriesInstanceUID']}")
            )
            _safe_extract(zip_path, target)
            zip_path.unlink()
        print(f"  {pid}: done")


def fetch_brachy(dest: Path) -> None:
    root = dest / "brachy_breast"
    for name, sha in DATASETS["brachy_breast"]["files"].items():
        zip_path = root / name
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(
            _get(
                f"https://zenodo.org/api/records/18496579/files/{urllib.parse.quote(name)}/content"
            )
        )
        _verify(zip_path, sha)
        _safe_extract(zip_path, root / name.replace("_dataset.zip", ""))
        zip_path.unlink()
        print(f"  {name}: verified and extracted")


def fetch_tg263(dest: Path) -> None:
    path = dest / "tg263" / "TG263_Nomenclature_Worksheet_20170815.xls"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _get(
            "https://www.aapm.org/pubs/reports/RPT_263_Supplemental/TG263_Nomenclature_Worksheet_20170815.xls"
        )
    )
    _verify(path, DATASETS["tg263"]["sha256"])
    print("  worksheet verified")


FETCHERS = {"gamma_knife": fetch_gamma_knife, "brachy_breast": fetch_brachy, "tg263": fetch_tg263}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("datasets", nargs="*", choices=[*DATASETS, []], help="datasets to fetch")
    ap.add_argument("--dest", type=Path, default=Path("open_data"))
    ap.add_argument("--list", action="store_true")
    ap.add_argument(
        "--yes", action="store_true", help="confirm the download(s) and the licence terms"
    )
    args = ap.parse_args()
    if args.list or not args.datasets:
        for key, d in DATASETS.items():
            print(
                f"{key:14} {d['title']}\n{'':14} {d['size']} | {d['licence']}\n{'':14} cite: {d['citation']}\n"
            )
        return 0
    if not args.yes:
        print(
            "Refusing to download without --yes (you accept each dataset's licence and citation duty)."
        )
        return 2
    for key in args.datasets:
        if (args.dest / FETCHERS[key].__name__.removeprefix("fetch_")).exists():
            print(f"{key}: {args.dest} already has it - remove the directory to re-fetch")
            continue
        print(f"Fetching {key}: {DATASETS[key]['title']}")
        FETCHERS[key](args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
