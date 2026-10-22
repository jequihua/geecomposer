"""Native, unstarted Earth Engine exports to an existing GCS bucket."""

from __future__ import annotations

import math
import re

import ee
from ee.ee_exception import EEException

from ..aoi import to_ee_geometry
from ..exceptions import GeeComposerError
from ..rao import RaoGrid


def _finite(value, name, *, positive=True):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or (positive and value <= 0):
        raise GeeComposerError(
            f"{name} must be {'positive and ' if positive else ''}finite and numeric"
        )


def _integer(value, name):
    if type(value) is not int or value <= 0:
        raise GeeComposerError(f"{name} must be a positive integer")


def export_to_gcs(
    image,
    description: str,
    bucket: str,
    region,
    scale: int | float | None = None,
    file_name_prefix: str | None = None,
    max_pixels: float = 1e13,
    *,
    crs: str | None = None,
    crs_transform: list[float] | tuple[float, ...] | None = None,
    dimensions: int | str | None = None,
    shard_size: int = 256,
    file_dimensions: int | list[int] | tuple[int, int] | None = None,
    file_format: str = "GeoTIFF",
    cloud_optimized: bool = False,
    no_data: int | float | None = None,
    skip_empty_tiles: bool = False,
) -> ee.batch.Task:
    """Construct an unstarted EE-to-GCS task; never inspect or create a bucket.

    Preserve the stub's positional order. Choose exactly one of scale (meters),
    crs_transform (six affine coefficients, requiring crs), or dimensions (a
    positive integer maximum side or 'WIDTHxHEIGHT'). Use the calculation's
    affine grid to retain alignment. Regions use the existing AOI normalizer.

    bucket is a bare bucket name; file_name_prefix is a relative object prefix
    (default description), optionally ending in '/'. It is never normalized.
    Only GeoTIFF is supported. file_dimensions must be positive integer multiples
    of shard_size. no_data sets export metadata; this helper never unmasks image
    pixels. The caller is responsible for dtype/sentinel compatibility and for
    deliberately starting tasks after permissions and destination checks.
    """
    if not isinstance(description, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,100}", description
    ):
        raise GeeComposerError(
            "description must contain 1-100 letters, digits, underscores or hyphens"
        )
    if (
        not isinstance(bucket, str)
        or not 3 <= len(bucket) <= (222 if "." in bucket else 63)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*[a-z0-9]", bucket)
        or any(
            not part
            or len(part) > 63
            or not part[0].isalnum()
            or not part[-1].isalnum()
            for part in bucket.split(".")
        )
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", bucket)
        or bucket.startswith("goog")
        or "google" in bucket
        or "g00gle" in bucket
    ):
        raise GeeComposerError(
            "bucket must be a valid bare GCS bucket name, not a URI or path"
        )
    prefix = description if file_name_prefix is None else file_name_prefix
    if (
        not isinstance(prefix, str)
        or not prefix
        or prefix != prefix.strip()
        or prefix.startswith("/")
        or "\\" in prefix
        or ":" in prefix
        or any(ord(c) < 32 or ord(c) == 127 for c in prefix)
        or any(part in ("", ".", "..") for part in prefix.removesuffix("/").split("/"))
    ):
        raise GeeComposerError(
            "file_name_prefix must be a relative object prefix without empty or dot segments"
        )
    try:
        if len(prefix.encode("utf-8")) > 1024:
            raise ValueError
    except (UnicodeError, ValueError):
        raise GeeComposerError(
            "file_name_prefix must fit in 1024 UTF-8 bytes"
        ) from None
    _finite(max_pixels, "max_pixels")
    if max_pixels != int(max_pixels) or max_pixels > 2**63 - 1:
        raise GeeComposerError("max_pixels must be a positive integral Int64 count")
    if sum(value is not None for value in (scale, crs_transform, dimensions)) != 1:
        raise GeeComposerError(
            "choose exactly one of scale, crs_transform or dimensions"
        )
    if crs is not None and (not isinstance(crs, str) or not crs.strip()):
        raise GeeComposerError("crs must be nonempty")
    options = {}
    if crs is not None:
        options["crs"] = crs
    if crs_transform is not None:
        if not isinstance(crs_transform, (list, tuple)) or len(crs_transform) != 6:
            raise GeeComposerError(
                "crs_transform must contain six numeric coefficients"
            )
        for coefficient in crs_transform:
            _finite(coefficient, "crs_transform coefficient", positive=False)
        try:
            grid = RaoGrid(crs, crs_transform)
        except (ValueError, TypeError, OverflowError):
            raise GeeComposerError(
                "invalid affine export grid; supply CRS and an invertible transform"
            ) from None
        options["crsTransform"] = list(grid.transform)
    elif scale is not None:
        _finite(scale, "scale")
        options["scale"] = scale
    else:
        if isinstance(dimensions, str):
            if not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", dimensions):
                raise GeeComposerError(
                    "dimensions must be a positive integer or WIDTHxHEIGHT"
                )
            # Reject unrepresentable integer strings before the native client parses them.
            try:
                for side in dimensions.split("x"):
                    _finite(int(side), "dimensions")
            except ValueError:
                raise GeeComposerError(
                    "dimensions must contain finite positive integers"
                ) from None
        else:
            _integer(dimensions, "dimensions")
            _finite(dimensions, "dimensions")
        options["dimensions"] = dimensions
    _integer(shard_size, "shard_size")
    _finite(shard_size, "shard_size")
    if file_dimensions is not None:
        sides = (
            file_dimensions
            if isinstance(file_dimensions, (list, tuple))
            else [file_dimensions]
        )
        if isinstance(file_dimensions, (list, tuple)) and len(sides) != 2:
            raise GeeComposerError(
                "file_dimensions must be an integer or width/height pair"
            )
        for side in sides:
            _integer(side, "file_dimensions")
            _finite(side, "file_dimensions")
            if side % shard_size:
                raise GeeComposerError(
                    "file_dimensions must be multiples of shard_size"
                )
        options["fileDimensions"] = file_dimensions
    if file_format != "GeoTIFF":
        raise GeeComposerError("only GeoTIFF exports are supported")
    if type(cloud_optimized) is not bool or type(skip_empty_tiles) is not bool:
        raise GeeComposerError("cloud_optimized and skip_empty_tiles must be booleans")
    format_options = {"cloudOptimized": cloud_optimized}
    if no_data is not None:
        _finite(no_data, "no_data", positive=False)
        format_options["noData"] = no_data
    geometry = to_ee_geometry(region)
    try:
        return ee.batch.Export.image.toCloudStorage(
            image=image,
            description=description,
            bucket=bucket,
            region=geometry,
            fileNamePrefix=prefix,
            maxPixels=max_pixels,
            shardSize=shard_size,
            fileFormat=file_format,
            formatOptions=format_options,
            skipEmptyTiles=skip_empty_tiles,
            **options,
        )
    except Exception as exc:
        category = {
            EEException: "EEException",
            ValueError: "ValueError",
            TypeError: "TypeError",
        }.get(type(exc), "unknown")
        raise GeeComposerError(
            f"GCS task construction failed ({category}); check image, region and export options"
        ) from None
