"""Sentinel-2 NDVI max composite over the case-study AOI.

Demonstrates the per-image transform stage: NDVI is computed for each
cloud-masked image, then reduced across time with ``max``.

Run from the repo root with:

    .venv/Scripts/python.exe 08_pkg/examples/sentinel2_ndvi_max.py

Set ``START_EXPORT = True`` to launch a Drive export task.
"""

from __future__ import annotations

from pathlib import Path

from geecomposer import compose, export_to_drive, initialize
from geecomposer.transforms.indices import ndvi


PROJECT = "manglariars"
AOI_PATH = (
    Path(__file__).resolve().parents[2]
    / "01_data"
    / "case_studies"
    / "rbmn.geojson"
)
DRIVE_FOLDER = "geecomposer-dev"
EXPORT_DESCRIPTION = "rbmn_s2_ndvi_max_2025"
START_EXPORT = False


def main() -> None:
    initialize(project=PROJECT, authenticate=False)

    img = compose(
        dataset="sentinel2",
        aoi=str(AOI_PATH),
        start="2025-01-01",
        end="2025-12-31",
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
