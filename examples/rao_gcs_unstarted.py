"""Construct an unstarted Rao task from an initialized EE session.

The caller supplies an image, bands, AOI, exact computation grid, pre-existing
authorized bucket and fresh run/product prefix. No login or export runs on import.
Initialize with caller-provided verified credentials before calling build_task.
"""

from geecomposer import RaoGrid, export_to_gcs, rao_q


def build_task(*, image, bands, region, grid: RaoGrid, bucket, prefix):
    product = rao_q(image, bands, grid=grid, region=region, window_size=3)
    return export_to_gcs(
        product.toDouble(),
        "rao_product",
        bucket,
        region,
        file_name_prefix=prefix,
        crs=grid.crs,
        crs_transform=grid.transform,
        max_pixels=2500,
        cloud_optimized=True,
        no_data=-9999,
        shard_size=256,
        file_dimensions=512,
        skip_empty_tiles=True,
    )
