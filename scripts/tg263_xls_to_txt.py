#!/usr/bin/env python3
"""Convert the official AAPM TG-263 nomenclature worksheet (.xls) to a plain name list.

    pip install xlrd
    python scripts/tg263_xls_to_txt.py TG263_Nomenclature_Worksheet_20170815.xls tg263_names.txt

The output has one name per line and is what rule STRUCT-006 reads (set `nomenclature_file`
or the RTPG_NOMENCLATURE_FILE environment variable). The worksheet is AAPM's: obtain it from
https://www.aapm.org/pubs/reports/RPT_263_Supplemental/ yourself. AAPM states no reuse licence, so
neither the .xls nor this derived list is bundled in or committed to the repository.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PRIMARY = "TG263-Primary Name"
REVERSE = "TG-263-Reverse Order Name"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("xls", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument(
        "--include-reverse", action="store_true", help="also allow the reverse-order names"
    )
    args = ap.parse_args()
    try:
        import xlrd
    except ImportError:
        print("error: this converter needs xlrd (pip install xlrd)", file=sys.stderr)
        return 2
    sheet = xlrd.open_workbook(args.xls).sheet_by_index(0)
    header = [str(c).strip() for c in sheet.row_values(0)]
    if PRIMARY not in header:
        print(f"error: column {PRIMARY!r} not found; columns are {header}", file=sys.stderr)
        return 2
    columns = [header.index(PRIMARY)]
    if args.include_reverse and REVERSE in header:
        columns.append(header.index(REVERSE))
    names: list[str] = []
    for r in range(1, sheet.nrows):
        row = sheet.row_values(r)
        names += [str(row[c]).strip() for c in columns if str(row[c]).strip()]
    args.out.write_text(
        "# AAPM TG-263 names extracted from "
        + args.xls.name
        + " (local use only; not redistributable)\n"
        + "\n".join(names)
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(names)} names to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
