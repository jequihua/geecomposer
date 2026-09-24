"""Bounded live synthetic Rao check and opt-in Drive export.

Run with the project venv; --help is offline. Existing EE user credentials are
used only for this explicitly invoked validation, never by the cloud runner.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import ee
import numpy as np

from geecomposer import RaoGrid, export_to_drive, fit_normalization, rao_q


def reference_rao(values, *, valid=None, model=None, min_valid=9):
    """Independent full ordered-pair CPU oracle for a 3x3 synthetic raster."""
    x = np.asarray(values, dtype=float)
    if x.ndim == 2:
        x = x[:, :, None]
    present = np.isfinite(x).all(axis=2)
    if valid is not None:
        present &= valid
    result = np.full(x.shape[:2], np.nan)
    for row in range(x.shape[0]):
        for col in range(x.shape[1]):
            if not present[row, col]:
                continue
            patch = [
                x[y, z]
                for y in range(max(0, row - 1), min(x.shape[0], row + 2))
                for z in range(max(0, col - 1), min(x.shape[1], col + 2))
                if present[y, z]
            ]
            if len(patch) < min_valid:
                continue
            vectors = np.asarray(patch)
            if model:
                vectors = ((vectors - model.means) / model.scales) @ np.asarray(
                    model.loadings
                ).T
            distance = np.linalg.norm(vectors[:, None, :] - vectors[None, :, :], axis=2)
            result[row, col] = distance.mean() / (model.bound if model else 1)
    return result


def synthetic_case():
    grid = RaoGrid("EPSG:32618", (20, 0, 500000, 0, -20, 1100000))
    region = ee.Geometry.Rectangle(
        [500000, 1099820, 500180, 1100000], proj=grid.crs, geodesic=False
    )
    # Pixel coordinates are center coordinates (.5, 1.5, ...), not indices.
    coords = ee.Image.pixelCoordinates(grid.projection()).floor()
    image = (
        coords.select("x").add(coords.select("y").multiply(2)).divide(100).rename("v")
    )
    image = image.toDouble().clip(region).reproject(grid.projection())
    valid = coords.select("x").neq(4).Or(coords.select("y").neq(4))
    masked = image.updateMask(valid)
    model = fit_normalization(
        image,
        ["v"],
        grid=grid,
        region=region,
        max_pixels=100,
        calibration="synthetic_9x9_gradient",
    )
    raw = rao_q(image, ["v"], grid=grid, region=region).select("rao_raw")
    gn = rao_q(image, ["v"], grid=grid, region=region, model=model).select("rao_gn")
    strict = (
        rao_q(masked, ["v"], grid=grid, region=region)
        .select("rao_raw")
        .rename("strict_missing")
    )
    subset = (
        rao_q(
            masked, ["v"], grid=grid, region=region, missing="valid_subset", min_valid=5
        )
        .select("rao_raw")
        .rename("subset_missing")
    )
    values = (np.arange(9)[None, :] + 2 * np.arange(9)[:, None]) / 100
    mask = np.ones((9, 9), dtype=bool)
    mask[4, 4] = False
    expected = dict(
        rao_raw=reference_rao(values),
        rao_gn=reference_rao(values, model=model),
        strict_missing=reference_rao(values, valid=mask),
        subset_missing=reference_rao(values, valid=mask, min_valid=5),
    )
    return (
        raw.addBands(gn).addBands(strict).addBands(subset),
        expected,
        grid,
        region,
        model,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--folder", default="geecomposer_rao_validation")
    parser.add_argument("--start-export", action="store_true")
    parser.add_argument(
        "--receipt",
        type=Path,
        required=True,
        help="New private local JSON receipt; never a credential file",
    )
    args = parser.parse_args()
    if args.receipt.exists():
        parser.error("receipt already exists; choose a new path")
    ee.Initialize(
        credentials=ee.data.get_persistent_credentials(), project=args.project
    )
    product, expected, grid, region, model = synthetic_case()
    sampled = product.sampleRectangle(region=region, defaultValue=-9999).getInfo()[
        "properties"
    ]
    comparisons = {}
    for band, reference in expected.items():
        actual = np.asarray(sampled[band], dtype=float)
        valid = np.isfinite(reference)
        np.testing.assert_array_equal(actual != -9999, valid)
        np.testing.assert_allclose(
            actual[valid], reference[valid], atol=1e-10, rtol=1e-8
        )
        comparisons[band] = dict(
            valid_pixels=int(valid.sum()),
            max_abs_error=float(np.max(np.abs(actual[valid] - reference[valid]))),
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    receipt = dict(
        project=args.project,
        synthetic_comparisons=comparisons,
        normalization=json.loads(model.to_json()),
        crs=grid.crs,
        transform=grid.transform,
        export_started=False,
    )
    # Write evidence before submission; never blindly retry an uncertain start.
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    if args.start_export:
        task = export_to_drive(
            product,
            "rao_synthetic_" + stamp,
            args.folder,
            region,
            crs=grid.crs,
            crs_transform=grid.transform,
            max_pixels=100,
        )
        task.start()
        receipt.update(
            export_started=True, task_id=task.id, description="rao_synthetic_" + stamp
        )
        args.receipt.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
