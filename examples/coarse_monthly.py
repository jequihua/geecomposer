"""Coarse-first national monthly composites: illustrative, makes no Earth Engine call.

Running this file prints how a national (MX-AEA) request is checked before any
Earth Engine object is built:

* without a completed snapshot the request fails (``missing_metadata``): nothing
  is fetched;
* a synthetic snapshot row whose zone-12 tile reaches beyond 5 degrees of its
  zone meridian over the requested cells is refused (``zone_offset``), never
  dropped.

With an initialized Earth Engine session and real completed C1/C2 rows (band
``dimensions`` included), ``national_monthly(snapshot)`` returns twelve lazy
images on the national grid; see docs/coarse.md. These offline checks are not
server evidence: FC01-FC14 have not run.
"""

from geecomposer import coarse, grouping

AOI = {
    "type": "Polygon",
    "coordinates": [[[-107.0, 27.1], [-106.0, 27.1], [-106.0, 27.4], [-107.0, 27.4], [-107.0, 27.1]]],
}
S2 = dict(dataset="sentinel2", aoi=AOI, select=["B2", "B3", "B4", "B8"], mask="s2_cloud_score_plus",
          grid=coarse.grid(1000))


def national_monthly(snapshot):
    """Twelve 2025 monthly S2 medians on the national MX-AEA 1 km grid."""
    return coarse.composite_periods(grouping.monthly_periods(2025), snapshot=snapshot,
                                    temporal=("median",), **S2)


def synthetic_snapshot():
    """One synthetic zone-12 Sentinel-2 tile row (illustrative, not catalogue data)."""
    band = {"crs": "EPSG:32612", "transform": [10, 0, 899960, 0, -10, 3100020], "dimensions": [10980, 10980]}
    row = {"id": "SYNTH_TILE_Z12", "time_ms": 1749579630000, "SPACECRAFT_NAME": "Sentinel-2A",
           "DATATAKE_IDENTIFIER": "GS2A_20250610T182030_000007_N05.11",
           "projections": {b: dict(band) for b in ("B2", "B3", "B4", "B8", "cs_cdf")}}
    return coarse.build_snapshot(start="2025-01-01", end="2026-01-01", candidate_rows=[row],
                                 completion_rows=[row], **{k: v for k, v in S2.items() if k != "temporal"})


def national_june(snapshot):
    """One national month; admission of its members runs before any Earth Engine object."""
    return coarse.composite(start="2025-06-01", end="2025-07-01", snapshot=snapshot, temporal=("median",), **S2)


def main():
    try:
        national_monthly(None)
    except coarse.CoarseError as err:
        print(f"national MX-AEA without a snapshot: {err.code}; nothing is fetched")
    try:
        national_june(synthetic_snapshot())
    except coarse.CoarseError as err:
        print(f"national MX-AEA with an inadmissible source: {err.code}; the request is refused, not truncated")


if __name__ == "__main__":
    main()
