# geecomposer

`geecomposer` is a lightweight Python library for building Google Earth Engine
composites with a small, explicit API.

It is intentionally narrow. v0.1 focuses on:

- Sentinel-2 and Sentinel-1 image collections
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

## What The Package Can Do

- initialize Earth Engine with a thin helper: `initialize()`
- normalize AOIs from:
  - `ee.Geometry`, `ee.Feature`, `ee.FeatureCollection`
  - GeoJSON-like dicts
  - local vector files such as `.geojson`, `.shp`, and `.gpkg`
- compose Sentinel-2 collections with Cloud Score+ masking
- compose Sentinel-1 collections with explicit radar filters
- apply built-in or custom per-image transforms
- reduce across time with:
  - `median`
  - `mean`
  - `min`
  - `max`
  - `mosaic`
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
- advanced Sentinel-1 preprocessing such as speckle filtering or terrain correction

## Public API

Top-level functions:

- `initialize(project: str | None = None, authenticate: bool = False)`
- `compose(...)`
- `compose_yearly(years, **compose_kwargs)`
- `export_to_drive(image, description, folder, region, scale, ...)`

## Installation

From the package workspace:

```powershell
cd geecomposer
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
