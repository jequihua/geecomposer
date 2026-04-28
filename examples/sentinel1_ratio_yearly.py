"""Sentinel-1 float yearly VH/VV ratio composites.

Demonstrates two things together:

1. ``compose_yearly()`` for one image per calendar year.
2. ``sentinel1_float`` for linear-unit SAR algebra (the dB preset is
   inappropriate for ratios).

For physically meaningful biomass-style features, prefer this float
path. Combine with ``preprocess=gamma_map()`` for speckle reduction;
see ``sentinel1_float_gamma_map.py`` for that variant.

Run from the repo root with:

    .venv/Scripts/python.exe 08_pkg/examples/sentinel1_ratio_yearly.py

Set ``START_EXPORT = True`` to launch one Drive export per year.
"""

from __future__ import annotations

from pathlib import Path

from geecomposer import compose_yearly, export_to_drive, initialize
from geecomposer.transforms.expressions import expression_transform


PROJECT = "manglariars"
AOI_PATH = (
    Path(__file__).resolve().parents[2]
    / "01_data"
    / "case_studies"
    / "rbmn.geojson"
)
DRIVE_FOLDER = "geecomposer-dev"
YEARS = [2023, 2024, 2025]
START_EXPORT = False


def main() -> None:
    initialize(project=PROJECT, authenticate=False)

    vh_vv = expression_transform(
        expression="vh / vv",
        band_map={"vh": "VH", "vv": "VV"},
        name="vh_vv_ratio",
    )

    yearly = compose_yearly(
        years=YEARS,
        dataset="sentinel1_float",
        aoi=str(AOI_PATH),
        transform=vh_vv,
        reducer="median",
        filters={
            "instrumentMode": "IW",
            "polarizations": ["VV", "VH"],
        },
    )

    for year, img in yearly.items():
        print(f"Year {year}: bands={img.bandNames().getInfo()}")
        task = export_to_drive(
            image=img,
            description=f"rbmn_s1f_vh_vv_ratio_{year}",
            folder=DRIVE_FOLDER,
            region=str(AOI_PATH),
            scale=10,
        )
        if START_EXPORT:
            task.start()
            print(f"  Started export for {year}.")
        else:
            print(f"  Skipping start for {year}; set START_EXPORT = True to launch.")
        print(f"  Status: {task.status()}")


if __name__ == "__main__":
    main()
