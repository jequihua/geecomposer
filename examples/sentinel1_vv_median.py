"""Sentinel-1 dB VV median composite over an explicit user-supplied AOI.

Canonical radar workflow on the dB-scaled preset. For ratio or
algebraic SAR features (VH/VV, RVI, ...), use ``sentinel1_float`` and
see ``sentinel1_float_gamma_map.py``.

Run from the package root with:

    .venv/Scripts/python.exe examples/sentinel1_vv_median.py

Set ``START_EXPORT = True`` to launch a Drive export task.
"""

from __future__ import annotations

import os
from pathlib import Path

from geecomposer import compose, export_to_drive, initialize


PROJECT = os.environ.get("GEECOMPOSER_PROJECT")
AOI_PATH = Path(os.environ["GEECOMPOSER_AOI"]) if "GEECOMPOSER_AOI" in os.environ else None
DRIVE_FOLDER = "geecomposer-dev"
EXPORT_DESCRIPTION = "geecomposer_s1_vv_median_2025"
START_EXPORT = False


def main() -> None:
    if not PROJECT or AOI_PATH is None:
        raise SystemExit("Set GEECOMPOSER_PROJECT and GEECOMPOSER_AOI explicitly before running")
    initialize(project=PROJECT, authenticate=False)

    img = compose(
        dataset="sentinel1",
        aoi=str(AOI_PATH),
        start="2025-01-01",
        end="2026-01-01",
        select="VV",
        reducer="median",
        filters={
            "instrumentMode": "IW",
            "polarizations": ["VV"],
            "orbitPass": "ASCENDING",
        },
    )

    print("Bands:     ", img.bandNames().getInfo())
    print("Properties:", img.getInfo()["properties"])

    task = export_to_drive(
        image=img,
        description=EXPORT_DESCRIPTION,
        folder=DRIVE_FOLDER,
        region=str(AOI_PATH),
        scale=10,
    )

    if START_EXPORT:
        task.start()
        print("Export task started.")
    else:
        print("Skipping start. Set START_EXPORT = True to launch.")
    print("Task status:", task.status())


if __name__ == "__main__":
    main()
