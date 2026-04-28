# geecomposer

`geecomposer` is a lightweight Python library for building Google Earth Engine
composites with a small, explicit API.

It is intentionally narrow. v0.1 focuses on:

- Sentinel-2, Sentinel-1 (dB), and Sentinel-1 float (linear units) image collections
- AOIs from local vector files, GeoJSON-like dicts, and Earth Engine geometry objects
- per-image transforms plus temporal reducers
- yearly grouping
- Drive export task creation

The main workflow is:

1. initialize Earth Engine
2. compose one image or a yearly set of images
3. optionally create export tasks
4. start and monitor those tasks in notebook or script code

`geecomposer` returns Earth Engine objects. It does not try to hide Earth
Engine behind a large abstraction layer.

## Recent Additions

Recent milestones added three important workflow improvements:

- `sentinel1_float` for linear-unit Sentinel-1 workflows
- opt-in mono-temporal Gamma MAP speckle filtering via
  `geecomposer.datasets.sentinel1_preprocessing.gamma_map`
- official notebooks and runnable example scripts for the current
  Sentinel-2, Sentinel-1 dB, and Sentinel-1 float + Gamma MAP paths

The deterministic test suite verifies call patterns and pipeline behavior.
Live Earth Engine validation still happens through the notebooks and must be
re-run locally when you want fresh evidence.

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
- build yearly composites with `compose_yearly()`
- create Drive export tasks with `export_to_drive()`

## What It Does Not Do

These are intentionally out of scope for v0.1:

- GCS export
- monthly or seasonal grouping
- task monitoring utilities
- visualization helpers
- a CLI
- local raster download pipelines
- multi-temporal Sentinel-1 speckle filtering, radiometric terrain
  flattening, additional border-noise correction, tide-aware filtering,
  or SAR texture features
  (mono-temporal Gamma MAP filtering for `sentinel1_float` is supported
  as an opt-in preprocess helper — see "Sentinel-1 Float With Gamma MAP
  Speckle Filtering" below)

## Public API

Top-level functions:

- `initialize(project: str | None = None, authenticate: bool = False)`
- `compose(...)`
- `compose_yearly(years, **compose_kwargs)`
- `export_to_drive(image, description, folder, region, scale, ...)`

## Installation

From the package workspace:

```powershell
cd 08_pkg
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

### 2. Compose A Sentinel-2 Image

```python
from geecomposer import compose
from geecomposer.transforms.indices import ndvi

img = compose(
    dataset="sentinel2",
    aoi="01_data/case_studies/rbmn.geojson",
    start="2024-01-01",
    end="2024-12-31",
    mask="s2_cloud_score_plus",
    transform=ndvi(),
    reducer="max",
)
```

### 3. Compose A Sentinel-1 Image

```python
img = compose(
    dataset="sentinel1",
    aoi="01_data/case_studies/rbmn.geojson",
    start="2024-01-01",
    end="2024-12-31",
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
    aoi="01_data/case_studies/rbmn.geojson",
    start="2024-01-01",
    end="2024-12-31",
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
    aoi="01_data/case_studies/rbmn.geojson",
    start="2024-01-01",
    end="2024-12-31",
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
    aoi="01_data/case_studies/rbmn.geojson",
    start="2024-01-01",
    end="2024-12-31",
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
    aoi="01_data/case_studies/rbmn.geojson",
    start="2024-01-01",
    end="2024-12-31",
    mask="s2_cloud_score_plus",
    transform=ndvi(),
    reducer="count",
)
```

For `sentinel1` and `sentinel1_float`, the same reducer gives a count of
contributing acquisitions rather than a cloud-free count.

### 4. Compose Yearly Images

```python
from geecomposer import compose_yearly
from geecomposer.transforms.indices import ndvi

yearly = compose_yearly(
    years=[2023, 2024, 2025],
    dataset="sentinel2",
    aoi="01_data/case_studies/rbmn.geojson",
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
    description="rbmn_s2_ndvi_max_2025",
    folder="geecomposer-dev",
    region="01_data/case_studies/rbmn.geojson",
    scale=10,
)

task.start()
print(task.status())
```

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

It does not implement credential management systems, service-account workflows,
or secret storage.

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

`export_to_drive()` creates a task but does not start it.

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
  - `rbmn_s1_vv_median_2025_smoke`
  - `rbmn_s1_vv_median_2025_smoke_v2`

### One image per export task

`export_to_drive()` exports one `ee.Image` at a time.

If you use `compose_yearly()`, you get:

- `dict[int, ee.Image]`

To export one raster per year, loop over that dictionary and create one task
per year.

## Current Project Status

The package is now implemented and unit-tested for the intended v0.1 scope:

- all four public API functions exist
- both required dataset presets exist (`sentinel1`, `sentinel1_float`,
  `sentinel2`)
- yearly grouping exists
- Drive export exists
- opt-in mono-temporal Gamma MAP speckle filtering is available for
  `sentinel1_float` workflows
- official notebooks and runnable example scripts now exist for the core
  workflows
- live notebook validation has begun, but re-execute the notebooks locally
  before treating their outputs as fresh evidence

Still deferred:

- GCS export
- monthly/seasonal grouping
- multi-temporal speckle filtering, radiometric terrain flattening,
  additional border-noise correction, tide-aware filtering, SAR texture
  features
- broader release polish

## Where To Start

If you have not used the package before, in order:

1. Read this README to the end of "Public API".
2. Open the **official end-to-end smoke notebook**:
   `02_analysis/notebooks/milestones/005_live_end_to_end_smoke.ipynb`.
   It runs Sentinel-2, Sentinel-1 dB, yearly grouping, and a gated
   Drive export against the case-study AOI at
   `01_data/case_studies/rbmn.geojson`.
3. For the linear-unit Sentinel-1 + Gamma MAP path, open the dedicated
   notebook:
   `02_analysis/notebooks/milestones/006_sentinel1_float_gamma_map_smoke.ipynb`.
4. For copy-paste scripts rather than notebooks, see
   `08_pkg/examples/`:
   - `sentinel2_red_median.py`
   - `sentinel2_ndvi_max.py`
   - `sentinel1_vv_median.py`
   - `sentinel1_ratio_yearly.py` — yearly VH/VV ratio on the float
     preset
   - `sentinel1_float_gamma_map.py` — Gamma MAP + VH/VV ratio

All examples and notebooks default `START_EXPORT = False` so re-running
them never silently launches an Earth Engine batch task.

### What is validated by what

- **Deterministic tests** (`08_pkg/tests/`, run via `pytest`) verify
  call patterns: which Earth Engine methods are invoked in which order
  with which arguments. They do not verify pixel-level numerical
  correctness against real EE outputs.
- **Notebooks under `02_analysis/notebooks/milestones/`** are
  human-run live validations against a real EE session and the
  case-study AOI. They are the source of truth for "does this
  workflow run end-to-end against EE today?".
- Treat any claim of "validated" as scoped to whichever of these two
  paths exercised it.

## Repository Structure

If you are picking the project up later, the most important places are:

- `08_pkg/src/geecomposer/`: package source
- `08_pkg/tests/`: unit tests
- `08_pkg/examples/`: runnable example scripts
- `02_analysis/notebooks/milestones/`: official live-validation
  notebooks (the durable smoke path)
- `05_governance/`: decisions, risks, review history
- `geecomposer_v0.1_spec.md`: primary product contract

## Recommended Reading Order For Future Work

If you need to resume the project later, start here:

1. `geecomposer_v0.1_spec.md`
2. `CLAUDE.md`
3. `08_pkg/architecture_contract.md`
4. `08_pkg/public_api_contract.md`
5. `08_pkg/current_status.md`
6. `08_pkg/testing_strategy.md`
7. `08_pkg/development_backlog.md`
8. `05_governance/decision_log.md`
9. `05_governance/risks.md`
10. `05_governance/review_log.md`
11. `02_analysis/findings.md`
12. the latest notebooks in `02_analysis/notebooks/`

## Future Roadmap

### Immediate next work

- finish live notebook validation for Sentinel-2 and Sentinel-1
- validate yearly export workflows
- harden examples and quickstart documentation
- prepare v0.1 release-facing materials

### Good future improvements

- a more polished README quickstart around notebook use
- example scripts under `08_pkg/examples/`
- optional helper patterns for repeated yearly exports if real usage proves they are needed
- stronger live-validation notes and troubleshooting guidance

### Explicitly deferred beyond v0.1

- GCS export
- monthly and seasonal grouping
- Landsat support
- task monitoring utilities
- CLI or visualization layers
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
