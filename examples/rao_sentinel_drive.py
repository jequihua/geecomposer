"""Small real Sentinel smoke, using explicit personal test authentication.

The default AOI is an illustrative public forest location near Munich, not a
publication site. The short reference interval validates software, not ecology.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import ee

from geecomposer import RaoGrid, export_to_drive, nirv_seasonal_rao


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--folder", default="geecomposer_rao_validation")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--start-export", action="store_true")
    args = parser.parse_args()
    if args.receipt.exists():
        parser.error("receipt exists; choose a new path")
    ee.Initialize(
        credentials=ee.data.get_persistent_credentials(), project=args.project
    )
    grid = RaoGrid("EPSG:32632", (20, 0, 0, 0, -20, 0))
    region = ee.Geometry.Point([11.414, 48.124]).buffer(150)
    result = nirv_seasonal_rao(
        region=region,
        grid=grid,
        start="2024-06-01",
        end="2024-08-01",
        max_scenes=60,
        max_pixels=2500,
    )
    # Both bands use one dtype; the mean retains its mask and the count diagnoses support.
    image = result.mean_image.addBands(result.observation_count).toDouble()
    receipt = dict(
        project=args.project,
        provenance=result.provenance,
        site_estimate=result.site_estimate,
        scene_summaries=result.scene_summaries,
        export_started=False,
        example_location="public forest near Munich; 150 m radius",
    )
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    if args.start_export:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        description = "rao_sentinel_" + stamp
        task = export_to_drive(
            image,
            description,
            args.folder,
            region,
            crs=grid.crs,
            crs_transform=grid.transform,
            max_pixels=2500,
        )
        task.start()
        receipt.update(export_started=True, task_id=task.id, description=description)
        args.receipt.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in receipt.items() if key != "scene_summaries"},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
