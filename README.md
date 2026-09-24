# geecomposer

`geecomposer` is a lightweight Python library for building Google Earth Engine
composites with a small, explicit API.

It is intentionally narrow. v0.1 focuses on:

- Sentinel-2, Sentinel-1 (dB), and Sentinel-1 float (linear units) image collections
- AOIs from local vector files, GeoJSON-like dicts, and Earth Engine geometry objects
- per-image transforms plus temporal reducers
- yearly grouping
- raw and normalized Rao Q on explicit pixel grids
- Sentinel-2 NIRv peak-season Rao products
- Drive and direct GCS export task creation
- strict JSON recipes and a bounded CLI runner with private GCS recovery state

The main workflow is:

1. initialize Earth Engine
2. compose one image or a yearly set of images
3. optionally create export tasks
4. start and monitor those tasks in notebook or script code

`geecomposer` returns Earth Engine objects. It does not try to hide Earth
Engine behind a large abstraction layer.

## Recent Additions

Recent milestones added these workflow improvements:

- `sentinel1_float` for linear-unit Sentinel-1 workflows
- opt-in mono-temporal Gamma MAP speckle filtering via
  `geecomposer.datasets.sentinel1_preprocessing.gamma_map`
- exact Rao Q with reusable normalization and explicit missing-window handling
- Sentinel-2 NIRv peak-season selection, per-observation Rao, and temporal
  mean/count products
- official notebooks and runnable example scripts for Sentinel-2, Sentinel-1,
  and Rao workflows

The deterministic test suite verifies call patterns, pipeline behavior, and
Rao arithmetic against independent numerical calculations. Live Earth Engine
validation uses notebooks and runnable scripts; see the
[Rao validation evidence](docs/qualification.md) for observed results and limits.

## What The Package Can Do

- initialize Earth Engine with a thin helper: `initialize()`
- normalize AOIs from:
  - `ee.Geometry`, `ee.Feature`, `ee.FeatureCollection`
  - GeoJSON-like dicts
  - local vector files such as `.geojson`, `.shp`, and `.gpkg`
- compose Sentinel-2 collections with Cloud Score+ masking
- compose Sentinel-1 dB collections with explicit radar filters
- compose Sentinel-1 float (linear-unit) collections for ratio features
- optionally apply opt-in mono-temporal Gamma MAP speckle filtering for
  `sentinel1_float` workflows
- apply built-in or custom per-image transforms
- reduce across time with:
  - `median`
  - `mean`
  - `min`
  - `max`
  - `mosaic`
  - `count`
- calculate raw or normalized Rao Q for one or more bands with `rao_q()`
- prepare Sentinel-2 NIRv and build peak-season Rao products with
  `nirv_seasonal_rao()`
- retain per-pixel observation counts, scene summaries, and calibration metadata
- build yearly composites with `compose_yearly()`
- create Drive export tasks with `export_to_drive()`
- create native unstarted GCS tasks with `export_to_gcs()`

## What It Does Not Do

These are intentionally out of scope for v0.1:

- general monthly or seasonal grouping through `compose()`
- general-purpose task scheduling outside the bounded recipe runner
- visualization helpers
- local raster download pipelines
- multi-temporal Sentinel-1 speckle filtering, radiometric terrain
  flattening, additional border-noise correction, tide-aware filtering,
  or SAR texture features
  (mono-temporal Gamma MAP filtering for `sentinel1_float` is supported
  as an opt-in preprocess helper — see "Sentinel-1 Float With Gamma MAP
  Speckle Filtering" below)

## Public API

Top-level functions:

- `initialize(project=None, authenticate=False, *, credentials=...)`
- `compose(...)`
- `compose_yearly(years, **compose_kwargs)`
- `export_to_drive(image, description, folder, region, scale=None, ...)`
- `export_to_gcs(image, description, bucket, region, scale=None, ...)`
- `rao_q(image, bands, grid=..., ...)`
- `fit_normalization(image, bands, grid=..., region=..., ...)`
- `rao(bands, grid=..., ...)` — per-image transform factory
- `nirv(...)` — per-image NIRv transform factory
- `prepare_sentinel2_nirv(image, grid=..., region=..., ...)`
- `nirv_seasonal_rao(region=..., grid=..., start=..., end=..., ...)`

`RaoGrid` defines the calculation grid. `RaoNormalization` stores a reusable
calibration. `NoRaoDataError` reports a seasonal workflow with no usable data.

## Installation

Requires Python 3.11 or newer. Dependency minimums match the exercised local
baseline; older versions are not claimed as supported. See
[baseline repairs and qualification limits](docs/qualification.md).

From the package root (the directory containing this README):

```powershell
python -m pip install -e .[dev]
```

## Quick Usage

### 1. Initialize Earth Engine

```python
from geecomposer import initialize

initialize(project="my-ee-project", authenticate=False)
```

If local credentials are not already available:

```python
initialize(project="my-ee-project", authenticate=True)
```

For a caller-provided credential object, use
`initialize(project=project, credentials=credentials)`. The object is forwarded
unchanged to EE. This helper does not discover, refresh, replace or fall back
from explicit credentials; EE itself may use them during initialization.
Explicit credentials and `authenticate=True` are rejected together before any
EE call. Discovery strings and explicit `None` are rejected: EE would retain
previous credentials for `None`. Omitting the argument retains legacy notebook behavior.

Institutional execution must obtain and verify the expected attached or federated
identity before calling the helper. Arbitrary credential injection is not identity
enforcement, and ordinary ADC discovery alone does not prove institutional
credentials. The [recipe runner](docs/runner.md) verifies the attached platform
identity and has no notebook fallback. Federation and impersonation remain
unsupported until their complete source chains can be verified.

### Offline recipe CLI

Installing the package provides `geecomposer`. These commands never initialize
EE or discover credentials:

```text
geecomposer validate examples/recipes/sentinel2_ndvi.json
geecomposer run --recipe examples/recipes/yearly_ratio.json --dry-run
```

Online `run`, `status`, `cancel` and explicit `resume` require administrator
policy and an approved attached service account. See [runner usage and recovery](docs/runner.md),
[portable recipes](examples/recipes/) and [observed offline verification](docs/qualification.md).

### 2. Compose A Sentinel-2 Image

```python
from geecomposer import compose
from geecomposer.transforms.indices import ndvi

img = compose(
    dataset="sentinel2",
    aoi="path/to/your/aoi.geojson",
    start="2024-01-01",
    end="2025-01-01",
    mask="s2_cloud_score_plus",
    transform=ndvi(),
    reducer="max",
)
```

### 3. Compose A Sentinel-1 Image

```python
img = compose(
    dataset="sentinel1",
    aoi="path/to/your/aoi.geojson",
    start="2024-01-01",
    end="2025-01-01",
    select="VV",
    reducer="median",
    filters={
        "instrumentMode": "IW",
        "polarizations": ["VV"],
        "orbitPass": "ASCENDING",
    },
)
```

### 3b. Compose A Sentinel-1 Float Image (Linear Units)

Use `sentinel1_float` for physically meaningful ratio features like VH/VV:

```python
from geecomposer.transforms.expressions import expression_transform

vh_vv = expression_transform(
    expression="vh / vv",
    band_map={"vh": "VH", "vv": "VV"},
    name="vh_vv_ratio",
)

img = compose(
    dataset="sentinel1_float",
    aoi="path/to/your/aoi.geojson",
    start="2024-01-01",
    end="2025-01-01",
    filters={"polarizations": ["VV", "VH"]},
    transform=vh_vv,
    reducer="median",
)
```

> **When to use which:** `sentinel1` provides dB-scaled backscatter
> (suitable for direct band composites). `sentinel1_float` provides
> linear power values (required for ratio and algebraic SAR features
> such as VH/VV, VH−VV, and RVI).

### 3c. Sentinel-1 Float With Gamma MAP Speckle Filtering

Mono-temporal Gamma MAP speckle filtering is available as an **opt-in**
helper for `sentinel1_float` workflows. It is wired through the existing
`preprocess=` slot, not auto-applied:

```python
from geecomposer import compose
from geecomposer.datasets.sentinel1_preprocessing import gamma_map

img = compose(
    dataset="sentinel1_float",
    aoi="path/to/your/aoi.geojson",
    start="2024-01-01",
    end="2025-01-01",
    preprocess=gamma_map(),
    select="VV",
    reducer="median",
    filters={"instrumentMode": "IW", "polarizations": ["VV"]},
)
```

Combined with a ratio transform:

```python
from geecomposer import compose
from geecomposer.datasets.sentinel1_preprocessing import gamma_map
from geecomposer.transforms.expressions import expression_transform

vh_vv = expression_transform(
    expression="vh / vv",
    band_map={"vh": "VH", "vv": "VV"},
    name="vh_vv_ratio",
)

img = compose(
    dataset="sentinel1_float",
    aoi="path/to/your/aoi.geojson",
    start="2024-01-01",
    end="2025-01-01",
    preprocess=gamma_map(),
    transform=vh_vv,
    reducer="median",
    filters={"polarizations": ["VV", "VH"]},
)
```

> **When to use it:** Use `gamma_map()` only with `sentinel1_float`, where
> band values are in linear power units. Applying it to dB-scaled
> `sentinel1` imagery produces misleading values.
>
> **What it does:** mono-temporal, per-image Gamma MAP filtering on the
> backscatter bands. The `angle` band is preserved unchanged. Default
> `kernel_size=7`; ENL is fixed internally to 5.
>
> **What it does not do:** This is **not** a Sentinel-1 ARD stack.
> Multi-temporal filtering, radiometric terrain flattening, additional
> border-noise correction, tide-aware filtering, and SAR texture features
> are intentionally out of scope.

### 3d. Count Valid Observations

Use `reducer="count"` when you want a per-pixel count of the observations
available at the reduction step.

For Sentinel-2 with cloud masking, this gives a clear-observation count:

```python
from geecomposer.transforms.indices import ndvi

clear_ndvi_count = compose(
    dataset="sentinel2",
    aoi="path/to/your/aoi.geojson",
    start="2024-01-01",
    end="2025-01-01",
    mask="s2_cloud_score_plus",
    transform=ndvi(),
    reducer="count",
)
```

For `sentinel1` and `sentinel1_float`, the same reducer gives a count of
contributing acquisitions rather than a cloud-free count.

### 3e. Calculate Rao Q

Use `rao_q()` to measure variation within a moving window of one or more
image bands. Supply an explicit grid so calculation and export stay aligned:

```python
from geecomposer import RaoGrid, fit_normalization, rao_q

# Choose a CRS and grid origin appropriate for your AOI.
grid = RaoGrid("EPSG:32632", (20, 0, 0, 0, -20, 0))

# image and region are your prepared ee.Image and ee.Geometry.
model = fit_normalization(image, ["nirv"], grid=grid, region=region)
rao_image = rao_q(
    image,
    ["nirv"],
    grid=grid,
    region=region,
    window_size=3,
    model=model,
)
```

With a fitted model, the data band is `rao_gn` (normalized Rao). Omit `model`
to calculate `rao_raw`. Both outputs include `rao_valid_count` and
`rao_coverage`, which describe valid pixels **within each spatial window**.

> **Missing data:** The default requires every pixel and selected band in
> the window to be valid. Incomplete windows are masked; a valid constant
> window produces zero. Windows of 1, 3, 5, or 7 cells per side are supported.

Use `rao(...)` when you need a transform for `compose()`. Fit a shared model
before applying it to a collection. See [Rao methods and usage](docs/rao.md)
for normalization, optional PCA, rounding, and alternative missing-data rules.

### 3f. Create A Seasonal NIRv Rao Product

Use `nirv_seasonal_rao()` for the complete Sentinel-2 workflow. It prepares
NIRv, calculates normalized Rao per observation, selects peak-season
observations, and averages their Rao values across time:

```python
import ee
from geecomposer import RaoGrid, nirv_seasonal_rao

# Small public forest AOI for a bounded example.
region = ee.Geometry.Point([11.414, 48.124]).buffer(150)
grid = RaoGrid("EPSG:32632", (20, 0, 0, 0, -20, 0))

result = nirv_seasonal_rao(
    region=region,
    grid=grid,
    start="2024-06-01",
    end="2024-08-01",  # Exclusive end date.
    max_scenes=60,
    max_pixels=2500,
)

rao_mean = result.mean_image
observation_count = result.observation_count
print(result.site_estimate)
```

By default, the workflow uses SCL and WorldCover 2021 (`ESA/WorldCover/v200`)
masks, averages B4/B8
reflectance onto the grid before calculating NIRv, and rounds NIRv to two
decimal places before fitting normalization independently per observation.
Observations need at least 50% input coverage and one complete Rao window.
Peak-season selection keeps observations whose site-median NDVI is strictly
above the reference interval's 90th percentile.

The reference interval defaults to the output interval. Set `reference_start`
and `reference_end` to use a different baseline. Window size, quantile,
coverage threshold, and mask classes are explicit options. The WorldCover map
is fixed across observation dates; it is not an annual land-cover time series.

The result includes:

- `mean_image`: `rao_gn_mean`, the per-pixel mean of selected Rao observations
- `observation_count`: `rao_observation_count`, the number of valid selected
  observations contributing at each pixel over the output interval
- `scene_summaries`: observation statistics, selection decisions, and source IDs
- `site_estimate`: the mean of selected observations' spatial Rao medians

> **How to read the count:** A value of 8 means that pixel's mean uses eight
> valid selected observations. It is an annual count only when the output
> interval covers a year. Zero means no selected observation contributes;
> the corresponding Rao mean is masked.

The site estimate and temporal mean raster summarize the data differently.
Rao is calculated before temporal averaging. This helper makes synchronous,
bounded Earth Engine summary requests; run it from notebook or script code,
not inside `ImageCollection.map()`. No usable season raises `NoRaoDataError`.

### 4. Compose Yearly Images

```python
from geecomposer import compose_yearly
from geecomposer.transforms.indices import ndvi

yearly = compose_yearly(
    years=[2023, 2024, 2025],
    dataset="sentinel2",
    aoi="path/to/your/aoi.geojson",
    mask="s2_cloud_score_plus",
    transform=ndvi(),
    reducer="max",
)

img_2025 = yearly[2025]
```

### 5. Create A Drive Export Task

```python
from geecomposer import export_to_drive

task = export_to_drive(
    image=img_2025,
    description="geecomposer_s2_ndvi_max_2025",
    folder="geecomposer-dev",
    region="path/to/your/aoi.geojson",
    scale=10,
)

task.start()
print(task.status())
```

For a seasonal Rao result, export the mean and its observation count together
using the same grid as the calculation:

```python
task = export_to_drive(
    image=result.mean_image.addBands(result.observation_count).toDouble(),
    description="forest_nirv_rao_2024_summer",
    folder="geecomposer-dev",
    region=region,
    crs=grid.crs,
    crs_transform=grid.transform,
    max_pixels=2500,
)

# Start explicitly when ready:
# task.start()
```

Use `crs_transform` instead of `scale` to preserve exact pixel alignment.

### 6. Create An Unstarted GCS Export Task

```python
from geecomposer import export_to_gcs

# Reuse the exact grid used to calculate the Rao product.
# Double bands share a GeoTIFF-compatible type, preserving bands and masks.
# -9999 is outside the valid nonnegative Rao/count range.
task = export_to_gcs(
    image=rao_image.toDouble(),
    description="rao_product",
    bucket="your-existing-bucket",
    region=region,
    file_name_prefix="validation/run-id/rao_product",
    crs=grid.crs,
    crs_transform=grid.transform,
    max_pixels=2500,
    cloud_optimized=True,
    no_data=-9999,
    shard_size=256,
    file_dimensions=512,
    skip_empty_tiles=True,
)
# Native task remains unstarted.
```

EE writes directly to the pre-existing bucket when the caller later starts the
task. There is no Drive staging, local download, Storage SDK, bucket creation or
existence/permission check. The caller must check the destination and permissions.
Use a bare bucket name and relative slash-separated object prefix; a trailing
slash is allowed. URI, absolute, backslash and dot-segment destinations are
rejected, never normalized. An omitted prefix defaults to the description.
Use a fresh run/product prefix to avoid collisions; this helper does not provide
overwrite prevention or idempotent submission.

Choose exactly one resolution: positive finite `scale`, `crs_transform` with
`crs`, or `dimensions` (positive integer or `"WIDTHxHEIGHT"`). Only GeoTIFF is
supported, optionally cloud optimized, with finite `no_data`. File dimensions
must be positive integer multiples of shard size. Boolean numbers and malformed
options fail before AOI reads. The helper never unmasks the image; choose nodata
compatible with its pixel type and distinct from valid values. Task descriptions
use 1-100 ASCII letters, digits, underscores or hyphens.
`max_pixels` must be a positive integral Int64 count (integer-valued floats such
as the default `1e13` remain supported), so the client cannot silently truncate it.

See the portable [unstarted GCS example](examples/rao_gcs_unstarted.py),
[EE GCS API](https://developers.google.com/earth-engine/apidocs/export-image-tocloudstorage)
and [grid guidance](https://developers.google.com/earth-engine/guides/exporting_images).

## AOI Inputs

AOIs can be provided as:

- Earth Engine objects:
  - `ee.Geometry`
  - `ee.Feature`
  - `ee.FeatureCollection`
- GeoJSON-like dictionaries
- local vector file paths

Multi-feature AOIs are dissolved to a single geometry by default.

## Transform Model

Transforms are per-image callables with the shape:

```python
Callable[[ee.Image], ee.Image]
```

Built-in transform helpers include:

- `select_band(...)`
- `normalized_difference(...)`
- `ndvi(...)`
- `nirv(...)`
- `rao(...)` (available from `geecomposer`)
- `expression_transform(...)`

Custom callables are also supported as long as they return an `ee.Image`.

## Reducer Model

Reducers are applied across time after all per-image operations are complete.

Supported reducer names:

- `"median"`
- `"mean"`
- `"min"`
- `"max"`
- `"mosaic"`
- `"count"` (per-pixel observation count)

## Notes And Caveats

### `initialize()` is intentionally thin

`initialize()` only wraps:

- `ee.Authenticate()`
- `ee.Initialize()`

It also forwards explicit caller credentials. It does not implement institutional
credential discovery, principal verification or secret storage.

### Auth safety

To avoid credential leaks:

- do not paste OAuth tokens into source files or notebooks
- do not write secrets into the repository
- do not commit credential JSON files, token caches, or local auth state
- keep Earth Engine auth in the normal local Earth Engine / Google environment

If you need authentication, prefer:

```python
initialize(project="my-ee-project", authenticate=True)
```

and complete the Earth Engine flow interactively on your machine.

### Auth gotchas we already encountered

These are worth remembering when picking the project back up later:

- `ee.Authenticate()` may choose a `gcloud` flow by default in some environments
- that can fail when Google blocks the default client for certain scopes
- using notebook-style authentication can be more reliable for interactive notebook work
- the selected Google Cloud project must be registered to use Earth Engine
- authentication can succeed while `ee.Initialize(project=...)` still fails if the chosen project is not registered for Earth Engine use

In short:

- credentials must be valid
- the target project must be Earth Engine-enabled
- Drive permissions must be available for export workflows

### Export tasks are explicit and asynchronous

`export_to_drive()` and `export_to_gcs()` create tasks but do not start them.

You must call:

```python
task.start()
```

Then inspect:

```python
task.status()
```

Common states:

- `UNSUBMITTED`
- `READY`
- `RUNNING`
- `COMPLETED`
- `FAILED`
- `CANCELLED`

### Task descriptions should be unique

When working in notebooks, avoid reusing the same task description repeatedly.

If you rerun export cells with the same request/task setup, Earth Engine may
report errors such as:

- "A different operation was already started with the same request id"

Good practice:

- create a fresh task object if you rerun the export cell
- change the description slightly for retries, for example:
  - `geecomposer_s1_vv_median_2025_smoke`
  - `geecomposer_s1_vv_median_2025_smoke_v2`

### One image per export task

`export_to_drive()` exports one `ee.Image` at a time.

If you use `compose_yearly()`, you get:

- `dict[int, ee.Image]`

To export one raster per year, loop over that dictionary and create one task
per year.

## Current Project Status

Release candidate preparation only: Linux containers and institutional live execution
remain unqualified. The package has offline tests for:

- composition, Rao, seasonal NIRv, and export helpers are available
- all three dataset presets exist (`sentinel1`, `sentinel1_float`,
  `sentinel2`)
- yearly grouping exists
- raw/normalized Rao and the Sentinel-2 NIRv peak-season workflow exist
- Drive export supports scale-based and explicit affine-grid exports
- opt-in mono-temporal Gamma MAP speckle filtering is available for
  `sentinel1_float` workflows
- official notebooks and runnable example scripts now exist for the core
  workflows
- live notebook validation has begun, but re-execute the notebooks locally
  before treating their outputs as fresh evidence

Still deferred:

- live institutional GCS/Cloud Run qualification
- general monthly/seasonal grouping through the composition API
- multi-temporal speckle filtering, radiometric terrain flattening,
  additional border-noise correction, tide-aware filtering, SAR texture
  features
- broader release polish

## Where To Start

If you have not used the package before, in order:

1. Read this README to the end of "Public API".
2. Open the **official end-to-end smoke notebook**:
   `notebooks/005_live_end_to_end_smoke.ipynb`.
   It runs Sentinel-2, Sentinel-1 dB, yearly grouping, and a gated
   Drive export against your AOI from `GEECOMPOSER_AOI`.
3. For the linear-unit Sentinel-1 + Gamma MAP path, open the dedicated
   notebook:
   `notebooks/006_sentinel1_float_gamma_map_smoke.ipynb`.
4. For copy-paste scripts rather than notebooks, see
   `examples/` relative to this README:
   - `sentinel2_red_median.py`
   - `sentinel2_ndvi_max.py`
   - `sentinel1_vv_median.py`
   - `sentinel1_ratio_yearly.py` — yearly VH/VV ratio on the float
     preset
   - `sentinel1_float_gamma_map.py` — Gamma MAP + VH/VV ratio
   - [`validate_rao_drive.py`](examples/validate_rao_drive.py) — synthetic Rao
     comparison against independent NumPy results
   - [`rao_sentinel_drive.py`](examples/rao_sentinel_drive.py) — seasonal NIRv
     Rao with a two-band mean/count Drive export

Set `GEECOMPOSER_PROJECT` and `GEECOMPOSER_AOI` explicitly before running the
composition examples or notebooks. Use an authorized project and local vector
file with a declared CRS. They use existing credentials and never authenticate
automatically. Institutional hosted identity is implemented but live qualification remains pending.

The existing composition examples and notebooks default `START_EXPORT = False`.
The Rao scripts require `--start-export` to submit a task. See
[Rao Drive validation](docs/rao.md#drive-validation) for runnable commands.

### What is validated by what

- **Deterministic tests** (`tests/`, run via `pytest`) verify call patterns,
  pipeline behavior, and Rao arithmetic against independent numerical
  calculations. They do not execute live EE pixel calculations.
- **Rao validation scripts** (`examples/`) exercise live synthetic pixel
  comparisons and the real Sentinel seasonal workflow. Observed results
  and limitations are recorded in [the validation notes](docs/qualification.md).
- **Notebooks under `notebooks/`** are
  human-run live validations against a real EE session and the
  user-selected AOI. They provide evidence for "does this
  workflow run end-to-end against EE today?".
- Treat any claim of "validated" as scoped to whichever of these
  paths exercised it.

## Repository Structure

Paths relative to this package:

- `src/geecomposer/`: package source
- `tests/`: offline tests and synthetic fixtures
- `examples/`: runnable scripts
- `notebooks/`: optional live-validation notebooks
- `docs/rao.md`: scientific methods and usage
- `docs/qualification.md`: inherited fixes and qualification limits

Read [qualification status](docs/qualification.md) and
[offline verification](docs/verification.md) before engineering review.

## Future Roadmap

### Immediate next work

- finish live notebook validation for Sentinel-2 and Sentinel-1
- validate yearly export workflows
- harden examples and quickstart documentation
- prepare v0.1 release-facing materials

### Good future improvements

- a more polished README quickstart around notebook use
- optional helper patterns for repeated yearly exports if real usage proves they are needed
- stronger live-validation notes and troubleshooting guidance

### Explicitly deferred beyond v0.1

- live institutional GCS/Cloud Run qualification
- general monthly and seasonal grouping through the composition API
- Landsat support
- general-purpose scheduling and visualization layers
- multi-temporal Sentinel-1 speckle filtering, Refined Lee, Lee Sigma,
  Boxcar, or any user-facing menu of filter algorithms
- radiometric terrain flattening / slope correction
- additional border-noise correction beyond the EE ingestion default
- tide-aware filtering and SAR texture features
- a broad Sentinel-1 ARD framework

## Design Principles

- keep the API function-based
- keep Earth Engine visible
- keep dataset logic in dataset modules
- keep transforms separate from reducers
- keep export separate from composition
- prefer small, reviewable modules over clever abstraction

### Container and Cloud Run preparation

The package Dockerfile has shared `local` (offline CLI) and `hosted` (required
administrator policy) targets. Container execution remains deferred; these are
reviewable artifacts, not a qualified image. See [container evidence and deferred
gates](docs/qualification.md), [base/lock provenance](container/README.md),
and the [operator procedure](docs/cloud_run_operator.md). The operator
helper `python -m geecomposer.deployment` defaults to an offline plan; upload and
execute are explicit, separately authorized modes. No mode publishes or deploys.
