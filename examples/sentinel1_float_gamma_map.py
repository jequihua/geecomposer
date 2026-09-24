"""Sentinel-1 float + opt-in Gamma MAP + VH/VV ratio composite.

This is the canonical biomass-style SAR feature workflow:

1. load ``sentinel1_float`` (linear power units required for ratios)
2. preprocess each image with ``gamma_map()`` (speckle reduction)
3. transform per image with a VH/VV expression
4. reduce across time with ``median``

``gamma_map()`` is opt-in and is intended for ``sentinel1_float`` only.
Applying it to dB-scaled imagery silently produces wrong values.

Run from the package root with:

    .venv/Scripts/python.exe examples/sentinel1_float_gamma_map.py

Set ``START_EXPORT = True`` to launch a Drive export task.
"""

from __future__ import annotations

import os
from pathlib import Path

from geecomposer import compose, export_to_drive, initialize
from geecomposer.datasets.sentinel1_preprocessing import gamma_map
from geecomposer.transforms.expressions import expression_transform


PROJECT = os.environ.get("GEECOMPOSER_PROJECT")
AOI_PATH = Path(os.environ["GEECOMPOSER_AOI"]) if "GEECOMPOSER_AOI" in os.environ else None
DRIVE_FOLDER = "geecomposer-dev"
EXPORT_DESCRIPTION = "geecomposer_s1f_vh_vv_ratio_gammamap_2025"
START_EXPORT = False


def main() -> None:
    if not PROJECT or AOI_PATH is None:
        raise SystemExit("Set GEECOMPOSER_PROJECT and GEECOMPOSER_AOI explicitly before running")
    initialize(project=PROJECT, authenticate=False)

    vh_vv = expression_transform(
        expression="vh / vv",
        band_map={"vh": "VH", "vv": "VV"},
        name="vh_vv_ratio",
    )

    img = compose(
        dataset="sentinel1_float",
        aoi=str(AOI_PATH),
        start="2025-01-01",
        end="2026-01-01",
        preprocess=gamma_map(),
        transform=vh_vv,
        reducer="median",
        filters={
            "instrumentMode": "IW",
            "polarizations": ["VV", "VH"],
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
