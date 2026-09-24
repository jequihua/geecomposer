"""Sentinel-2 NDVI max composite over an explicit user-supplied AOI.

Demonstrates the per-image transform stage: NDVI is computed for each
cloud-masked image, then reduced across time with ``max``.

Run from the package root with a prepared project venv. Live evaluation
requires separate authorization and explicit project/AOI inputs.

Windows PowerShell:

    .venv/Scripts/python.exe examples/sentinel2_ndvi_max.py

POSIX shell:

    .venv/bin/python examples/sentinel2_ndvi_max.py

Set ``START_EXPORT = True`` to launch a Drive export task.
"""

from __future__ import annotations

import os
from pathlib import Path

from geecomposer import compose, export_to_drive, initialize
from geecomposer.transforms.indices import ndvi


PROJECT = os.environ.get("GEECOMPOSER_PROJECT")
AOI_PATH = Path(os.environ["GEECOMPOSER_AOI"]) if "GEECOMPOSER_AOI" in os.environ else None
DRIVE_FOLDER = "geecomposer-dev"
EXPORT_DESCRIPTION = "geecomposer_s2_ndvi_max_2025"
START_EXPORT = False


def main() -> None:
    if not PROJECT or AOI_PATH is None:
        raise SystemExit("Set GEECOMPOSER_PROJECT and GEECOMPOSER_AOI explicitly before running")
    initialize(project=PROJECT, authenticate=False)

    img = compose(
        dataset="sentinel2",
        aoi=str(AOI_PATH),
        start="2025-01-01",
        end="2026-01-01",
        mask="s2_cloud_score_plus",
        transform=ndvi(),
        reducer="max",
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
