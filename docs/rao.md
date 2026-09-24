# Rao Q

`rao_q` builds an exact Earth Engine graph; it never starts exports or authenticates.
Use `RaoGrid(crs, (xScale, xShear, xOrigin, yShear, yScale, yOrigin))` explicitly.
All bands are aligned to this grid with EE's default nearest-neighbor sampling;
perform any desired continuous-band aggregation beforehand. Export with exactly
the same CRS and affine transform, not a scale alone.

```python
from geecomposer import RaoGrid, fit_normalization, rao_q

grid = RaoGrid("EPSG:32618", (20, 0, 500000, 0, -20, 1100000))
# image, region are caller-supplied EE objects; initialize EE explicitly first.
model = fit_normalization(image, ["nirv"], grid=grid, region=region,
                          decimal_places=2)
product = rao_q(image, ["nirv"], grid=grid, region=region,
                model=model, decimal_places=2)
```

`fit_normalization` makes two synchronous small-statistic requests, including
population covariance computed from centered products. The first request also
reduces each band's minimum and maximum. Equal extrema identify a constant:
its exact observed value replaces the rounded-off mean, its covariance row and
column become zero, and its scale is 1. No variance tolerance suppresses genuinely
different values. Saved model JSON is unchanged; existing models are read exactly
as stored (refit an old contaminated calibration to obtain corrected statistics).
`from_statistics` accepts caller-supplied covariance without inferring constancy
from small variance alone. It does not download
pixels. Fit outside `ImageCollection.map`. No sampling, `bestEffort` coarsening,
per-window fitting or PCA whitening occurs. The calibration contains band order,
means, population scales, PCA rows, actual retained variance, original dimension,
reference description, sample count and quantization. Save `model.to_json()` and
reload with `RaoNormalization.from_json()` to reuse the exact calibration across
dates or processing tiles. The default calibration description records region
geometry and grid: keep it private when either is sensitive.

`rao(bands, grid=..., model=...)` is the transform factory for `compose`.
Its model is shared across scenes; this differs from fitting each scene independently.

Q uses `2/m² * sum(unique-pair Euclidean distances)`, including multiplicities.
GN divides distances between standardized, optionally PCA-projected vectors by
`2*n_sigmas*sqrt(ceil(original_dimension*actual_retained_fraction))`.
Default `n_sigmas=6`, PCA retained variance target `.98`; `None` disables PCA.
The one-variable bound is 12. Constant reference variables have scale 1 and
contribute zero in that reference domain. At least two common reference pixels
are required. Out-of-domain values are not clipped. A tied PCA eigenspace at
the cutoff is retained in full to avoid selecting an arbitrary orientation.

Raw transforms default to no rounding. A model and its transform must have
identical `decimal_places`; explicit rounding uses decimal scaling with binary
double arithmetic and ties-to-even on the scaled value. The named NIRv protocol
uses 2 decimal places before normalization. This is an explicit interpretation
of the preprint, not a claim of historical bitwise replication.

Outputs are `rao_raw` or `rao_gn`, `rao_valid_count`, and `rao_coverage`.
Strict mode requires every selected band of every window pixel to be valid;
invalid output is masked, not zero. True valid constants yield zero.
`missing="valid_subset", min_valid=...` is a separately named alternative.
It re-normalizes probabilities over valid neighbors but still requires a valid
center. Windows 1, 3, 5 and 7 are admitted; pair counts are 0, 36, 300 and 1176.
Larger windows are rejected rather than silently approximated.

`region` is a real scientific sampling boundary: windows crossing it are invalid
in strict mode. Processing tiles must reuse the full region/model and read a
halo of `(window_size-1)/2` cells; crop/export tiles only after computing Rao.
Coverage diagnostics are present even where the Rao band is masked.

The method follows the combined protocol in the project's scientific contract:
[Gomarasca preprint](https://doi.org/10.21203/rs.3.rs-6829244/v1),
[Gomarasca 2024](https://doi.org/10.1088/2752-664X/ad87f9), and
[Pacheco-Labrador 2023](https://doi.org/10.1111/2041-210X.14163).
Code is independently implemented; no reference GPL source is included.

Offline tests check analytic statistics, fitted calibration and actual EE graph
serialization. They cannot establish evaluated server pixel correctness; live
synthetic-raster comparison and a small Sentinel export are separate evidence.

## Named Sentinel workflow

```python
from geecomposer import nirv_seasonal_rao, export_to_drive

result = nirv_seasonal_rao(
    region=region, grid=grid,
    start="2023-01-01", end="2024-01-01",
    reference_start="2017-03-28", reference_end="2024-04-01",
)
task = export_to_drive(
    result.mean_image.addBands(result.observation_count).toDouble(),
    description="rao_site_example", folder="geecomposer_rao_validation",
    region=region, crs=grid.crs, crs_transform=grid.transform,
    max_pixels=50_000,
)
# Review AOI, dates, project, destination and bounds before explicit submission:
# task.start()
```

The reference interval determines the exact R-type-7 NDVI quantile; the output
interval determines which observations can contribute to the result. Both are
half-open. The public example above can exceed a site's scene limit: narrow
the interval or deliberately set `max_scenes`, never silently truncate it.

Sentinel B4/B8 reflectance is scaled by .0001 and averaged on the explicit grid
before NDVI/NIRv. Every contributing fine pixel must survive SCL and WorldCover
masks. Default SCL classes are 4/5. Default WorldCover 2021 v200 classes are
10/20/30/40/60/90/95/100, including bare/sparse vegetation; pass explicit classes
or `world_cover_classes=None` to disable this mask. These are disclosed choices,
not an implicit switch to Cloud Score+.
WorldCover v200 is one fixed 2021 map applied across all observation dates.

Tiles with the same spacecraft, sensing orbit and UTC day form one observation.
Prepared tiles are mosaicked in ascending `system:index` order; the last valid
tile wins overlaps. Missing acquisition metadata or duplicate source IDs fail.
NIRv preparation is per tile before this same-acquisition mosaic; Rao and its
reference model are computed once on the complete mosaic. Scientific AOI edges
remain strict. Source IDs and all observation summaries are retained.

Observations need input coverage >= .5 (valid pixels / ALL grid centers in the
AOI) and at least one complete Rao window. Only quality-accepted observations
in the reference interval enter the NDVI quantile. The default comparison is
strict `>` against q=.90; `comparison="ge"` and a different quantile are explicit
alternatives. Empty input, no reference data or no retained season raise
`NoRaoDataError`. They never produce a successful zero product.

`result.scene_images` holds selected per-observation Rao rasters, including
valid-window counts and coverage. `result.scene_summaries` includes excluded
observations and reasons. `result.mean_image` is the per-pixel temporal mean;
`result.observation_count` has zero where no selected observation contributes.
`result.site_estimate` is the mean of selected scene spatial medians; it is NOT
the median of the temporal-mean image. `summary_table()` creates a geometry-free
EE table for an optional explicit table export.

The orchestrator performs synchronous bounded summary calls and must not run
inside a server-side map. Spatial medians set `maxRaw=max_pixels` to use EE's
[direct median computation](https://developers.google.com/earth-engine/apidocs/ee-reducer-median)
within the admitted pixel budget, avoiding its histogram path. The API documentation
does not specify the even-count convention. The evaluated 2x2 fixture
`[0, 1, 4, 9]` returned 2.5, and the 3x3 squared-rank fixture returned 16;
both matched `numpy.median`. This establishes agreement for those direct-path
fixtures only, not every input or the histogram path. See the
[current validation status](qualification.md)
for the measured results and evidence scope.
EE may count band multiplicity against its reduction
pixel cap: a multiband fit can reach the cap sooner than a single-band fit.
Increase bounds only deliberately, never enable bestEffort. Normalization can
be frozen by supplying `model=...`, an explicitly named alternative to the
default independent per-observation calibration.
Frozen model bands and quantization are checked before any remote evaluation.
Provenance records the default or string collection ID (`custom` for an EE object),
`max_pixels`, and `max_scenes`. Exclusion reasons distinguish inadequate input
coverage from fewer than two calibration pixels.

## Drive validation

The examples `validate_rao_drive.py` and `rao_sentinel_drive.py` use an existing
interactive EE login only when explicitly run with `--project`. They default to
evaluation without export; `--start-export` starts one new export. Give each run
a new private `--receipt` path (for example under ignored `tmp/rao-validation`).
Do not track credentials or private validation receipts. The owner-authorized
personal validation does not change the future cloud runner identity model.

Run from the package root with a prepared project venv, only after separate
live evaluation/export authorization. These commands explicitly opt into export;
keep each receipt path private and fresh.

Windows PowerShell:

```powershell
.venv/Scripts/python.exe examples/validate_rao_drive.py --project YOUR_EE_PROJECT --receipt tmp/rao-validation/synthetic.json --start-export
.venv/Scripts/python.exe examples/rao_sentinel_drive.py --project YOUR_EE_PROJECT --receipt tmp/rao-validation/sentinel.json --start-export
```

POSIX shell:

```sh
.venv/bin/python examples/validate_rao_drive.py --project YOUR_EE_PROJECT --receipt tmp/rao-validation/synthetic.json --start-export
.venv/bin/python examples/rao_sentinel_drive.py --project YOUR_EE_PROJECT --receipt tmp/rao-validation/sentinel.json --start-export
```

The synthetic example compares live EE pixels with an independent NumPy oracle
before submission. The Sentinel example uses a public 150 m forest AOI and a
short June/July 2024 reference interval to validate software, not reproduce the
paper. Exports preserve the calculation's affine grid and remain unstarted in
the general export helper. Drive accepts a folder NAME rather than an ID; inspect
the actual task destination URI, particularly when duplicate folder names exist.
After submission, retain the task ID and check its status rather than repeating
the command after an uncertain response. Export completion and downloaded-file
validation are separate checks. Current live results are in [qualification status](qualification.md).

The audited GeoTIFFs encode missing cells as NaN, with no NoData tag and GDAL
`all_valid` masks. A mask-only reader is insufficient; use finite values:

```python
import numpy as np

# mean and count are arrays read from the paired exported bands.
valid = np.isfinite(mean) & np.isfinite(count) & (count > 0)
supported_mean = np.ma.array(mean, mask=~valid)
```

See [qualification status](qualification.md) for the historical v100 scope.
The v200 default remains without live Sentinel validation.
