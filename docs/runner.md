# Version 1 recipe runner

The installed `geecomposer` command uses the same package science functions for
personal, institutional-local and hosted execution. The library/notebook APIs
retain their defaults, custom callables, interactive initialization and
unstarted Drive/GCS tasks; importing the package selects no CLI mode and never
authenticates. The original implementation was offline; later accepted January
regional exports and installed Linux checks are summarized in
[regional evidence](regional_release.md). Institutional execution remains
unqualified; see [qualification limits](qualification.md).

## Execution modes

The mode is chosen from arguments and the image before any credential file,
metadata server or network is touched, and never changes after an error:

| Mode | Selected by | Identity | Destination | Run records |
| --- | --- | --- | --- | --- |
| Personal (default) | no `--policy`, no `--hosted`, no bundled policy | explicit user EE saved-login file | Drive folder name | private local state directory |
| Institutional local | `--policy FILE` | approved attached service account | allowlisted GCS | private GCS control prefix |
| Hosted | bundled policy (always) or `--hosted` | approved attached service account | allowlisted GCS | private GCS control prefix |

Personal options (`--project`, `--credentials`, `--state-dir`) are rejected in
institutional and hosted modes before any policy, recipe or credential read.
Institutional policy admits only allowlisted GCS destinations, so a Drive
recipe fails validation there. Personal online commands require a Drive
destination; the personal CLI does not export to GCS (Python `export_to_gcs()`
remains available). There is no fallback between modes.

## Offline commands

From the package directory, after installation:

```text
geecomposer --help
geecomposer validate examples/recipes/sentinel2_ndvi.json
geecomposer run --recipe examples/recipes/yearly_ratio.json --dry-run
geecomposer --policy examples/recipes/policy.local.example.json validate examples/recipes/rao_gn.json
geecomposer validate examples/recipes/personal_rao_gn_drive.json
```

Validation reads at most 1 MiB from a regular UTF-8 JSON file. Neither validation
nor dry-run initializes EE, discovers credentials, contacts metadata or opens a
network connection. Supplying a local policy additionally checks its allowlists
and lower ceilings. Without policy these commands check the fixed C envelope;
that is not authorization for an online run.

Output is JSON with task count (or `null` when seasonal selection is unknown),
resource ceilings, a conservative pixel bound when computable offline, scene
count `null`, and pair/band/scene/pixel work bounds. Projected-grid bounds, scene
counts, available observations and actual permissions require bounded online
preflight. A dry-run is not a promise of data or successful execution. Hosted
input cannot be downloaded by an offline command: validate its local copy before
uploading it through the institution's approved tooling.

**Seasonal output warning:** `scene_outputs` still defaults to `true`. Validate
and dry-run emit `warnings` with code `seasonal_scene_output_limit`, two
`fixed_outputs`, and `allowed_scene_outputs = effective outputs ceiling - 2`
(normally eight at ceiling ten). Task count remains `null`. Seasonal evaluation
may be expensive before selection count is known. `scene_outputs: false` avoids
scene exports and leaves two fixed products; even those require capacity for two.
The runner never clips selected scenes or changes this setting automatically.

## Recipe shape

See the twelve small [example JSON files](../examples/recipes/): six GCS
recipes, three `personal_*_drive.json` recipes and three `coarse_*` recipes. Examples contain
illustrative resources, not production defaults. Every object rejects unknown
fields. All JSON keys must be unique; booleans are not numbers. NaN, infinities,
overflowing numbers, malformed dates, duplicate bands/years, invalid polygon
topology, open rings, arbitrary URLs/paths, expressions, code and asset IDs fail.

| Field | Contract |
| --- | --- |
| `version` | Integer `1`. |
| `workflow` | `composite`, `yearly`, `rao`, `seasonal_nirv`, `coarse` (see below). |
| `dataset` | `sentinel2`, `sentinel1` (dB), `sentinel1_float` (linear). |
| `aoi` | Inline 2D GeoJSON Polygon/MultiPolygon, WGS84 lon/lat. No Feature wrappers, vector-driver paths or references. Latitudes within -85..85; antimeridian-crossing rings are rejected. Holes must be valid. Edges use `geodesic=False`. |
| `grid` | Required except for `coarse`. `{crs, transform}`; six finite invertible affine coefficients. CRS is EPSG:4326, EPSG:3857, or EPSG:32601..32660/32701..32760. Other CRS definitions are rejected rather than interpreted. |
| `destination` | Exactly one shape. GCS: `{bucket, prefix}`, bare lowercase bucket from the administrator allowlist, relative slash-separated prefix; version 1 admits simple hyphenated bucket names, no dotted buckets. Drive (personal mode only): `{drive_folder}`, a folder name of 1-100 letters, digits, `_`, `-` or `.` starting with a letter or digit; no separators, IDs or URLs are interpreted. Mixed or unknown keys fail. |
| `bands` | 1..12 distinct actual Sentinel bands, not regex selectors. S2: B1..B9 including B8A, B11/B12; S1: VV/VH/HH/HV/angle. |
| `start`, `end` | Real `YYYY-MM-DD` dates, half-open interval. Required except for yearly. |
| `years` | Yearly only; 1..12 distinct integers 1..9998 (the output ceiling), replacing dates. |
| `reducer` | Optional existing median (default), mean, min, max, mosaic or count. |
| `transform` | Optional identity (default), ndvi, nirv, vh_vv_ratio. Indices need B4/B8; ratio requires linear VV/VH. NIRv uses S2's explicit 0.0001 reflectance factor. No caller expression strings. |
| `mask` | Optional `s2_cloud_score_plus`, only for S2 ordinary composites/Rao. |
| `preprocess` | Optional `gamma_map`, only for S1 float, with existing default 7-cell window. |
| `filters` | S1 only: instrumentMode IW/EW/SM, orbitPass ASCENDING/DESCENDING, distinct polarizations VV/VH/HH/HV. Existing IW default remains. |
| `rao` | Rao workflow only: required normalization raw/gn, optional window_size (default 3), decimal_places (default null; integer 0..9 or null). Strict complete windows remain the library default. |
| `seasonal` | Seasonal only: optional reference_start/reference_end (default output interval), window_size (3), decimal_places (2 or explicit null), scene_outputs (true). |
| `limits` | Optional integer decreases to administrator ceilings. No upward overrides. |

Ordinary Rao is computed on the named temporal composite. GN fits once over the
explicit AOI and grid, and persists its model and fingerprint. Seasonal NIRv
uses the existing per-observation public workflow: WorldCover v200, SCL 4/5,
strict support, exact 0.90 quantile with strict `>`, and default normalization
and rounding. It exports mean and observation-count rasters, optionally every
selected scene raster. All scene summaries/site estimate/calibration identities
are stored privately. More than 12 requested exports fails before submission;
there is no first-N selection. The seasonal example explicitly requests only
mean/count exports. Use an appropriate aligned 20 m grid for the combined
protocol; the runner never silently changes the requested grid.

Scene IDs are snapshotted before graph construction. An extra sentinel entry in
the bounded metadata query detects catalogue growth after the count check; it
causes rejection. The adapter builds an explicit ImageCollection snapshot and
calls the existing compose/compose_yearly/rao_q/fit_normalization/seasonal APIs.
`compose(collection=...)` now also documents ImageCollection snapshots and marks
their metadata `explicit_snapshot`. No scientific algorithms or defaults changed.
Resume reuses saved source IDs and checks the rebuilt native config/provenance
fingerprints. It fails closed on changed calibration or graph configuration.

## Coarse-first recipes (`workflow: "coarse"`)

`"workflow": "coarse"` runs the coarse-first products of `geecomposer.coarse`
through the normal recipe and CLI path, one product per run:

- Sentinel-1 linear power;
- Sentinel-2 reflectance;
- coarse GN Rao.

Three monthly 2025 examples on a 1 km grid are provided:
`coarse_s1_monthly_2025.json`, `coarse_s2_monthly_2025.json` and
`coarse_rao_monthly_2025.json`. The S1 and S2 examples use an illustrative
national-span outline plus a Pacific island piece; it is not an official
boundary. The Rao example uses a separate illustrative polygon. None is the
private measured RBMN geometry, and the 2025 monthly recipes are capability
examples, not twelve observed live months. For all six presets, support sizes,
operation orders and annual-memory limits, see [coarse methods](coarse.md).

**Top-level fields.**

| Field | Contract |
| --- | --- |
| `dataset` | `sentinel2` (bands a unique subset of B2/B3/B4/B8, `mask: "s2_cloud_score_plus"` required) or `sentinel1_float` (VV/VH; `filters` with instrumentMode IW, an explicit `orbitPass`, polarizations VV and VH; no Gamma MAP). |
| `start`, `end` | The run interval, half-open. |
| `grid` | Optional. If present it must equal the resolved coarse grid, for example `{"crs": "MX-AEA", "transform": [1000, 0, 0, 0, -1000, 0]}`; anything else is a contradictory-grid error. A coarser export scale never replaces per-observation aggregation. |
| `reducer`, `transform`, `preprocess`, `rao`, `seasonal`, `years` | Rejected; the `coarse` section owns these choices. |

**The `coarse` section.** Every key is validated and unknown keys are
rejected.

| Key | Contract |
| --- | --- |
| `resolution` | Required: 100, 250, 500, 1000, 5000 or 10000 m. |
| `crs` | `"MX-AEA"` (default; the fixed national Albers grid with a 10 km origin) or a local northern UTM zone `"EPSG:326zz"`. It resolves to exactly the WKT, affine and origin of `coarse.grid`. |
| `spatial` | `"mean"`, the only spatial statistic. |
| `coverage_min` | In (0, 1]; default 0.5. Applied once, after the last spatial stage. |
| `temporal` | A unique subset of mean, median, sd_pop, min, max and range; default `["median"]`. Equal weight per observation, after the spatial mean. |
| `index` | `"ndvi"` (S2), computed from the mean or median composite. |
| `db` | Boolean (S1). Adds dB bands for mean, median, min and max. |
| `periods` | `"monthly"` (the run must be one calendar year), a list of up to 12 `{label, start, end}` inside the run, or absent (one period, the whole run). |
| `tiles` | `{columns, rows, cores}`: the aligned export window split into `columns × rows` integer cores (each 1..12), optionally only the listed `[row, col]` cores. Default one core. |
| `product` | `"composite"` (default) or `"rao"`. |
| `rao` | Rao only. Either `{"model": {...}}`, an inline frozen four-feature `RaoNormalization` whose band order the recipe's `bands` must follow, or `{"fit": {start, end}}`, one reference median fit inside the run. |
| `snapshot` | Optional. An inline completed snapshot, as stored in a previous run's private record, so that runs exporting different cores share the same sources. Its member images count against the effective `scenes` ceiling. |

**Limits.**

- **Final bands.** At most 32. Rao always exports eight: value, valid count,
  coverage, `obs__t_count` and the four features.
- **Output ceiling.** Declared periods × selected cores must fit the effective
  `outputs` ceiling (12, or lower by policy or recipe). This is checked before
  any credential or remote access; an empty period cannot make an oversized
  declaration admissible.
- **Scene ceiling.** Every snapshot fits the effective `scenes` ceiling
  (1,000, or lower by policy or recipe), counting its member images. This
  covers fresh reads, an inline `snapshot` and a persisted snapshot on resume.
  An oversized inline snapshot fails validation; an oversized persisted one
  stops the resume (`conflict`) before calibration or tasks. Nothing is
  truncated.
- **Window ceiling.** Coarse Rao always uses its fixed 3 × 3 neighbourhood, so
  its effective `window` ceiling must be at least 3. A lower recipe or policy
  ceiling fails validation for both the fit and the supplied-model paths.
  Composites are unaffected.
- **Records made before these checks.** A run recorded before the scene and
  window ceilings were enforced can still be inspected and stopped. That covers
  an inline `snapshot` above its `scenes` ceiling, or coarse Rao with a
  `window` ceiling below 3.
  - `status` reads the trusted record.
  - `cancel` persists a stop request. After the worker's lease, it confirms
    and cancels only the recorded operations.
  - Its work stays inadmissible: `resume`, `run`, `validate` and
    `run --dry-run` refuse it, and nothing is prepared or submitted.
  - Every other record check still applies: policy, content and source
    binding, recipe and snapshot structure, reserved prefix, task markers and
    operation confirmation.
- **More than 12 outputs.** A national 500 m monthly product with several
  cores needs an explicit replan: separate runs for different `cores`, sharing
  one inline `snapshot` and `model`. Nothing is batched automatically and no
  coverage is reduced silently.

**Execution order.**

1. **Metadata.** The run reads Earth Engine metadata once, through the bounded
   runner transport and within the run deadline:
   - the AOI bounds in the grid;
   - candidate rows (C1) over the run interval and coarse-cell union;
   - completion rows (C2) by acquisition key, without dates;
   - for Sentinel-2, Cloud Score+ rows for the members.

   For every contributing band it captures the native CRS, affine and
   `dimensions`. Any response pixel `origin` is folded into the affine.
   Missing or contradictory metadata, overflow of the 1000-row read, more than
   600 keys or 400 acquisitions in a period, and unsupported native grids all
   fail clearly. Nothing is truncated or fetched again later.
2. **Persistence.** The validated snapshot (members, projections, dimensions,
   scope and fingerprint) and any model are persisted in the private run record
   before any export intent. Resume rebuilds and revalidates them without new
   metadata reads or a refit, and a changed record refuses before submission.
3. **Calibration.** A reference fit runs once, before the period loop. It must
   fit the 1,000,000-cell calibration ceiling, which is separate from the
   10,000,000-pixel output ceiling. Its intent is recorded first: a fit that was
   interrupted before its model was persisted fails the run (`uncertain`)
   instead of refitting. Start a new run to fit again.
4. **Science.** Each period is built once on the full coarse-cell union with
   the shared snapshot and model.
5. **Tasks.** Each selected core gets one unstarted task, using the existing
   -9999/double encoding and exact core rectangle. The raw AOI polygon never
   truncates coarse cells.
   - A period without acquisitions is recorded as `empty` and gets no task.
   - If every period is empty, the run is `no_data`.

**Dry-run.** `validate` and `run --dry-run` are credential-free. The report
labels each item:

- **Known:** periods, task names and their upper bound, cores, grid, stage
  chain with first and nested contributor bounds, final bands and units,
  coverage, and Rao's 3X support.
- **Conservative bounds:** the national window, and the maxPixels values,
  which hold only under the per-source domain conditions.
- **Unknown:** scene counts, empty periods, EECU-seconds and runtime.

Output pixels are not compute cost, and twelve tasks are not promised to finish
within the deadline.

**Task telemetry.** Each status poll can update a task's
`{"attempt", "batch_eecu_usage_seconds"}`:

- The attempt is tracked separately from the measurement. A newer attempt
  replaces the current value. If it carries no valid EECU value, its usage is
  `null` (unknown), never the earlier attempt's value or zero.
- An older attempt never replaces it, nor does a response without a valid
  attempt number once one is known. This holds after the state is reloaded.
- Polls of the same attempt replace a valid value and are never summed. A
  poll without a valid value keeps that attempt's last valid one.
- Valid values are finite and nonnegative, including zero.
- Telemetry never affects task identity, success or limits. It is shown per
  task only when present.

This dry-run does not measure project-level EECU, waiting time or Drive/GCS cost.
The regional campaign's partial task telemetry is observational and incomplete;
see [measured values and limits](regional_release.md).

## Policy, identity and hosted packaging

An administrator policy has exactly: `version`, `mode`, `project`,
`service_account`, `outputs`, `inputs`, `control`, `limits`, `poll_seconds`,
`image_version`. Location entries are `{bucket, prefix}`. Output and control
buckets must differ under the current validator because mutable control state
needs conditional replacement while rasters should have no delete permission.
Input/control may share an approved bucket at separate prefixes. An all-in-one
bucket layout is not admitted even when administrators approve separate prefixes;
it needs a separately reviewed validator correction or a distinct control bucket.
Prefixes alone are not IAM isolation. Prefix matches
include a slash boundary, never a mere textual prefix. No permissive policy is
installed by default.

The local operator supplies `--policy policy.json` before the command. The
example policy is illustrative and uses `mode: local`. Institutional local online
execution requires running on a platform with the approved attached identity.
Federation/impersonation, downloaded keys and personal refresh credentials are
not accepted in institutional modes; unsupported online identity fails clearly
rather than falling back to a personal account. Personal use is the separate
default mode described below.

The hosted build must place a reviewed `runtime_policy.json` beside `geecomposer/cli.py` in a
read-only image layer, set its `mode` to `hosted`, and record the reviewed image
version/digest. The hosted Docker target validates an explicit administrator policy input and
adds that exact file after installing the general wheel; the wheel excludes it. It is deliberately absent here. Its presence makes hosted mode
mandatory: arguments cannot switch to local mode, supply another policy path,
change project/account, or read a local recipe. `--hosted` also requires that
bundle; it cannot manufacture a hosted policy. Do not make its path an environment
variable, caller-writable mount, symlink into caller storage or job override.
Protect the image, command, Python import paths, package installation and policy
from modification. This CLI does not sandbox arbitrary code launched by someone
with container/deployment override authority. Deployers must restrict that authority
and use dedicated jobs/accounts for separate cohorts, as designed.

Production authentication bypasses ADC discovery entirely. It contacts only
the fixed metadata IP with Metadata-Flavor verification, no ambient proxy or
redirect, checks the actual attached account against policy, then obtains the
token from that same attached-account endpoint. Refresh repeats principal
verification. An asserted email, discovery string, generic compute credential,
OAuth refresh credential or service-account key object is not accepted as proof.
Credential-file, metadata-endpoint, storage-emulator and universe overrides are
rejected. Verified identity precedes EE initialization or creation/use of a
storage client. EE and GCS transports restrict origins, disable ambient proxies
and redirects, and use finite timeouts. No EE login or persistent credentials are
written. Unsupported federation/impersonation must remain rejected until both
source chain and target have an approved validator.

Hosted execution takes an approved, generation-pinned object:

```text
geecomposer run --recipe gs://APPROVED-INPUT/recipes/example.json --recipe-generation GENERATION --run-id RUN_ID
geecomposer status --run-id RUN_ID
geecomposer cancel --run-id RUN_ID
geecomposer resume --run-id RUN_ID
```

Replace placeholders with institution-provided values; this documentation does
not authorize invocation. Download metadata must fit the byte ceiling before
allocation; the download also has an explicit byte range, raw bytes and generation
precondition. The canonical recipe, source generation, complete effective policy,
package version and image setting participate in the run binding. Local paths
are not recorded; local source identity is a content hash. Hosted AOIs are inline
bounded JSON, not arbitrary vector files. External upload/invocation tooling and
deployment require explicit authorization; no upload/publish helper runs implicitly.

## Personal mode

```text
geecomposer --project PROJECT [--credentials FILE] [--state-dir DIR] run --recipe RECIPE.json --run-id RUN_ID
geecomposer --project PROJECT [--state-dir DIR] status --run-id RUN_ID
geecomposer --project PROJECT [--credentials FILE] [--state-dir DIR] resume --run-id RUN_ID
geecomposer --project PROJECT [--credentials FILE] [--state-dir DIR] cancel --run-id RUN_ID
```

**Project.** `--project` is required for every online personal command and is
bound into the run; a different project makes status/resume/cancel fail as a
conflict. Offline `validate` and `--dry-run` accept but never use personal
options.

**Credentials.** Log in on the host with Earth Engine's own flow
(`earthengine authenticate` or `ee.Authenticate()`); geecomposer adds no OAuth
flow, profile manager or token store. `--credentials` names that saved-login
file; without it the installed EE client's documented location
(`~/.config/earthengine/credentials`) is used. In Docker, mount exactly that
file read-only and always pass `--credentials`; never mount a home directory or
bake a credential into an image. The file is read once (at most 64 KiB, strict
JSON) and may contain only keys the EE saved-login flow writes
(`refresh_token`, `scopes`, `client_id`, `client_secret`, `redirect_uri`,
`project`). Service-account keys, gcloud ADC files, external-account files and
anything else fail with exit 3. The refresh token becomes an explicit
`google.oauth2` user credential with EE's fixed token endpoint and the saved
(or EE default) client and scopes; the file's `project` is ignored in favor of
`--project`. There is no ADC, metadata-server or key-file fallback, no
interactive login during `run`, and no credential write. Each invocation resets
EE and initializes it with that object and project, so no identity is inherited
from a previous run. Earth Engine calls and token refreshes go through the
runner's bounded, proxy- and redirect-free transport, limited to
`earthengine.googleapis.com` and the fixed `oauth2.googleapis.com` token host.
Tokens and raw errors are never printed or stored. `status` reads only the
local record and does not open credentials.

**State directory.** `--state-dir` (default `~/.geecomposer/runs`, created with
owner-only permissions where the OS supports them) holds `RUN_ID.json` records
and `RUN_ID.lock` files. It must not contain the credential file or lie inside
the credential file's directory. Each record is at most 1 MiB and holds the
canonical recipe, source hash, binding, project, reserved file-name base,
provenance (scene IDs, GN calibration), task request IDs, markers,
configuration fingerprints and operation names, never credential material.
Every read and write takes a non-blocking OS file lock (released by the OS if
the process dies; bounded wait of about five seconds, then `conflict`),
re-checks the record generation and replaces the record atomically from a
temporary file in the same directory. Reservation requires that no record
exists. A file that is not a valid geecomposer record for that run ID is
reported `uncertain` and never overwritten. A failed write reports `uncertain`
and leaves the prior record. Protect the directory as private data and keep it
while a run may need recovery. Locking on network or container-shared
filesystems has not been qualified.

**Execution and recovery.** Personal runs use the same state machine, ceilings
and deadlines as institutional runs (sections below), without bucket preflight
or GCS inventory: reserve, snapshot sources, persist the plan, persist each
request ID, marker, fingerprint and a `submitting` intent **before**
`exportImage`, then persist the operation name. After a crash, wait for the
120-second lease and `resume`; an intent whose operation cannot be found by its
exact marker is reported `uncertain` and is never resubmitted. `cancel` on a
live run records a stop request (`cancelling`); on an expired run it cancels
discovered EE operations. Ctrl-C/SIGTERM request the same cancellation. Earth
Engine tasks outlive the CLI or container: exiting does not stop them, and
`remote_pending: true` means work may still be running. Reaching the
1,800-second application deadline finalizes the run as `deadline` and requests
cancellation of unfinished exports, as in institutional mode. Resume fails
closed if the project, recipe, run binding, prepared configuration or
scientific provenance changed.

**Drive outputs.** Each product becomes one native unstarted Drive task built by
`export_to_drive()` with the recipe's exact CRS/affine grid and `-9999`
GeoTIFF nodata metadata. Before the task is built, after all compositing,
transformation and Rao computation, the runner selects the AOI (`Image.clip`)
and then encodes the export explicitly: every band's masked cells, including
cells outside the AOI, become `-9999` (`Image.unmask(-9999,
sameFootprint=False)`) and every band is converted to double (so Rao
value/count/coverage share one type). Valid pixels, including zero and
fractional-mask pixels, are unchanged; a non-finite value at a valid pixel is
not replaced, so it remains detectable. The native constructor then applies its
usual reprojection and clip, here to the export window: the grid-aligned pixel
rectangle enclosing the AOI in the grid's own pixel coordinates. The single
bounds query asks Earth Engine for the AOI's box in the full affine grid, so
shear and rotation are included. That clip bounds the otherwise
unbounded fill without masking any window cell. GCS runner tasks use the same
boundary. The public
`export_to_drive()`/`export_to_gcs()` helpers themselves stay metadata-only and
never unmask. Files are named
`RUN_ID-RANDOM_PRODUCT` in the named folder; Earth Engine may add `-row-col`
tile suffixes. The folder is a name: Earth Engine uses an existing folder of
that name anywhere in your Drive (the most recently modified if several exist)
or creates one; separators are literal. Choose a distinctive name. Output JSON
reports `mode`, `project`, `destination`, each task's `name`, `state`,
`request_id`, `task_id` (from the operation name, when known) and
`file_name_prefix`, plus `remote_pending` and `raster_verification: "pending"`.
It contains no invented Drive file IDs or URLs. `succeeded` means every Earth
Engine export task reported COMPLETED; no Drive listing, download, checksum or
pixel check is performed and no scope is requested beyond the saved login's.
Inspect the GeoTIFFs (grid, dtype, nodata, masks, values) before relying on
them.

**Qualification.** Offline checks cover fake credential/transport/task boundaries,
temporary state and native graphs. Later bounded regional Drive and installed
Linux results are accepted; see [regional evidence](regional_release.md).
Those campaign results do not qualify every personal recipe or the host
credential-mount pattern. Institutional identity and GCS delivery remain unverified.

## Runtime Earth Engine permissions

The attached runtime principal needs `serviceusage.services.use` on the enabled,
registered EE consumer project, plus the following EE permissions. Project
membership or successful initialization is not proof of these grants.

| Runner activity | Exact permission |
| --- | --- |
| Synchronous scene/count/grid/calibration computation | `earthengine.computations.create` |
| Create batch image export | `earthengine.exports.create` |
| Discover an operation by its marker | `earthengine.operations.list` |
| Read actual operation status | `earthengine.operations.get` |
| Request asynchronous cancellation | `earthengine.operations.update` |

[ComputeValue](https://developers.google.com/earth-engine/reference/rest/v1/projects.value/compute)
and [image export](https://developers.google.com/earth-engine/reference/rest/v1/projects.image/export)
specify their permissions. Google's [audit-method mapping](https://developers.google.com/earth-engine/guides/audit_logging#googlelongrunningoperations)
explicitly maps List/Get/CancelOperation to list/get/update respectively; there
is no invented `earthengine.operations.cancel` grant. Cancellation does not delete
an operation or prove it stopped.

[Resource Writer](https://docs.cloud.google.com/iam/docs/roles-permissions/earthengine)
includes these permissions and broader asset/configuration powers; it is an
administrator-reviewed fallback, not an automatic grant. Viewer lacks export
creation and operation update. A custom role must retain the API/source read and
configuration access needed by the approved workflows; administrators must
qualify it against [EE service-usage requirements](https://developers.google.com/earth-engine/guides/access_control).
No Owner/Editor role is proposed. Runtime EE permissions do not grant Storage
access. Launchers need job get/run-with-overrides (and input create for upload),
not runtime EE powers. Deployers/builders are separate identities. The
[administrator request](institutional_admin_request.md) has the
matching inventory; institutional approval remains pending. These documents do not modify IAM.

## Runtime Storage permissions

The institutional permission review must account for these actual
runner calls. This is a permission inventory, not a role grant or IAM change.

| Resource / call | Required permission |
| --- | --- |
| Output and control bucket metadata preflight (`buckets.get`) | `storage.buckets.get` on both buckets. |
| Generation-pinned input metadata and bytes (`objects.get`) | `storage.objects.get` on approved inputs. No input listing or writes. |
| Exact-run output listing, including size, generation and checksum metadata | `storage.objects.list` on the output bucket; this implementation uses listing metadata, not a separate object GET. |
| New raster objects delivered by EE | `storage.objects.create` on approved outputs. No output overwrite/delete authority is requested. |
| Private control metadata and bytes | `storage.objects.get` on control records. |
| Initial control reservation and generation-conditional replacement | `storage.objects.create`, plus `storage.objects.delete` for replacement of the same object name, scoped to control storage. No explicit delete request is made. |

These mappings follow Google's [JSON-method permissions](https://docs.cloud.google.com/storage/docs/access-control/iam-json).
Conditional replacement still needs overwrite authority; a generation condition
does not waive it. See [objects.insert](https://docs.cloud.google.com/storage/docs/json_api/v1/objects/insert).
Output and control buckets are separate so control replacement authority does
not confer output deletion. `storage.objects.update` is not a substitute for
content replacement. No bucket creation or IAM mutation permission is used.

The library `export_to_gcs` helper builds an unstarted EE task without Storage
metadata preflight; the runner adds bucket reads and inventory verification.
Object-only access that suffices for a library export is therefore insufficient
for runner preflight. Readable metadata does not prove export-write access.
Administrators must independently qualify the approved runtime principal, EE project,
output delivery and control replacement under the institution's policy; no live
permission check or IAM change is authorized by this document.

## Resource and request budgets

Hard ceilings are 1 MiB recipe; 10,000 vertices; 12 input dimensions; odd windows
through 7; 12 outputs (D126/A17; records created under the former 10-output
ceiling keep it); 10 million pixels/output; 1,000 source scenes across the
run; 1 million calibration-grid pixels; 2 active exports; 1,800 seconds application
time; 3 transient status retries. Policy and recipe can lower these ceilings.
Policy chooses polling in 15..60 seconds. The application reserves its last 60
seconds for cancellation/finalization, with an injectable clock in tests.
Source counts, projected enclosing grid rectangles and calibration extents are
checked before submission, without sampling, grid coarsening or silent truncation.

Metadata, preflight, graph evaluations, export, status and storage calls have at
most 10-second request timeouts and share the application budget. Native EE
automatic retries are disabled. Only transient status failures retry (three
retries, bounded 15/30/60-second exponential backoff plus deterministic per-ID
jitter, capped at 60); unknown responses and permission failures do not.

EE and token requests ask for uncompressed responses only. The runner's EE
transport replaces any caller `Accept-Encoding` header, in any letter case, with
`identity` on the outgoing request; it does not modify the caller's header
dictionary or other headers, and it contains no decompressor.

A response is accepted with no `Content-Encoding`, or with exactly one whose
trimmed, case-insensitive value is `identity`. Any other coding (gzip, deflate,
unknown, empty, compound or duplicated) is closed unread and refused as
`uncertain` ("unsupported EE response encoding"), without retry. HTTP 401/403
still map to `auth`, and 429/5xx keep their transient handling, even when their
bodies are compressed. Byte limits and deadline checks are unchanged.

EE initialization reports `auth` only for:

- an explicit runner permission status;
- a google-auth refresh error that carries a structured, non-retryable token
  endpoint refusal code: `invalid_grant`, `invalid_client` or
  `unauthorized_client` (RFC 6749 §5.2). Exception text is never inspected.

A token reply without an access token (for example HTTP 200 `{}`), or one with
a retryable, unknown, ill-typed or absent code, reports `uncertain`.

Undecodable, malformed, transient or otherwise unclassified initialization
responses report `uncertain` ("EE initialization response unavailable"), and
deadlines stay `deadline`. No response body or exception text is shown.

Storage
operations use `retry=None`, and no ambiguous conditional write is blindly
retried. Deadline checks run inside the EE HTTP transport, including the seasonal
helper's synchronous evaluations. GCS pagination checks deadlines on every
request. Response bodies and inventory pages are bounded. Socket timeouts and
application checks are not an OS hard-kill guarantee; deployment still needs the planned
35-minute platform timeout. No host process is killed by the runner.

## Persistence, recovery and cancellation

1. Validate inputs and policy; verify attached identity. Inspect existing bucket
   metadata. HTTP 404 is reported as absent according to that API, 401/403 as
   inaccessible with existence unknown, other failures as unknown. Never create
   buckets or change IAM. A readable bucket does not prove export-write access.
2. Reserve `control-prefix/RUN_ID.json` with `if_generation_match=0`. The bounded
   private record contains canonical recipe, source/binding, project/principal,
   versions and an output prefix `user-prefix/RUN_ID-random-UUID/`. Reusing an ID
   with `run` returns conflict, including after success. Use explicit `resume`.
3. Build bounded native unstarted configs and save the source/provenance plan.
   Obtain each explicit `ee.data.newTaskId()` request ID. Derive description
   `gc-` plus SHA-256 of canonical `[request_id, run_binding, product_name]`:
   67 characters, below the supported 100-character limit, with no raw private
   fields. Persist immutable ID, marker and the final marked config fingerprint
   before submission. Resume reconstructs that identical config. No private
   Task fields are touched; configs go to `ee.data.exportImage`.
4. CAS each product from prepared to submitting **before** export. Save the
   returned operation name. A request UUID is not an operation ID. If the name
   was lost, enumerate the configured project's operations, matching the exact
   `metadata.description` marker. Use at most five pages of 100 operations,
   1 MiB/page and 10 seconds/request within the existing deadline; no automatic
   discovery retry or unbounded `ee.data.listOperations`. Complete enumeration
   must yield exactly one match. Save that name with CAS before status/cancel
   uses it, retaining the previous owner while reconciling before takeover.
   Zero/ambiguous matches, malformed records, unavailable listing, repeated
   tokens or incomplete enumeration leave uncertainty. Identity/permission
   failures propagate auth (exit 3), with unresolved remote work still pending;
   absence never proves
   non-submission. Never allocate a replacement ID or resend that intent.
5. A 120-second ownership lease and generation-match writes fence overlapping
   workers. The owner renews during work; only one CAS takeover wins. A stale
   worker cannot persist a new intent. An already persisted intent stays
   unresolved until reconciled, even after lease expiration. No claim of
   distributed exactly-once execution is made.
6. After a crash, wait for the lease to expire, then explicitly resume. Resume
   reconciles original submitted intents before takeover. Prepared IDs are provably
   unsubmitted under the intent protocol; they retain their IDs/config hashes.
   A crash just after intent but before the HTTP send can therefore remain
   uncertain. Do not erase the reservation, manually reset submitting to prepared,
   or run a new ID as a retry while the original export remains unresolved.
7. `cancel` on an owned run CAS-writes a stop request and returns cancelling.
   The owner observes it at its next fenced write/heartbeat, cancels pending
   exports and retains partial objects. SIGINT/SIGTERM request the same cleanup
   through normal control flow. If the owner crashed, invoke cancel again after
   lease expiration. Resume of a stopped/expired run only finalizes/cancels; it
   never extends the original work deadline. Cleanup has its own at-most-60-second
   window within that invocation's overall budget.

Cancellation is asynchronous. `remote_pending: true` and uncertain cancellation
mean EE work may survive the worker. Inspect the private task's `marker`, original `request_id`, product name and
saved operation name. An authorized operator can use the configured project's
[operations listing](https://developers.google.com/earth-engine/reference/rest/v1/projects.operations/list)
and compare [metadata.description](https://developers.google.com/earth-engine/reference/rest/Shared.Types/OperationMetadata)
for exact equality with that marker, then inspect/cancel the full returned
`projects/PROJECT/operations/OPERATION` name. Do not query the request UUID as an
operation or match only a product name. Retain service evidence privately.
After visibility catches up, explicit resume/cancel can rediscover within the
same bounds. Eventual visibility, retention and a project with more than 500
listed operations can prevent automatic recovery; missing evidence cannot
justify a replacement run. Older manifests without a marker fail uncertain;
do not synthesize one, edit intent state, or erase the reservation. Obtain
institution/operator evidence and an authorized recovery decision.
`status` reads the persisted snapshot and does not silently resume or refresh all
EE statuses. Explicit resume performs reconciliation. State conflicts or lost
write acknowledgements stop the current worker immediately; they are not a
reason to overwrite another generation. Failed/cancelled rasters are never deleted.

## Completion and private evidence

Success requires every planned task to report COMPLETED and a verified exact-run
object inventory. Every required product must have a nonempty `.tif` or EE
`image-row-column.tif` shards in its own product directory. Reject unexpected or
duplicate object names, zero sizes, missing generations, absent/invalid available
CRC32C/MD5 metadata, incomplete product inventory and inventories above 512 objects.
GCS listing is fully consumed within its bounds; there is no first-page success.
This verifies service-reported checksums and inventory, not downloaded raster
bytes or scientific pixel values. Completed EE status plus object listing cannot
prove continued retention against later external deletion; production qualification
still needs the prescribed exported-raster audit.

The private control record is also the manifest, at most 1 MiB. It retains method
parameters via canonical recipe, grid, export window, float64 dtype/nodata -9999, source scene
IDs, GN models/fingerprints, seasonal coverage/summary data, tasks/operations,
bounded status categories and actual object names/generations/sizes/checksums.
Oversized provenance fails before export rather than truncating evidence. Every
export explicitly converts bands to double and uses the computation affine grid;
missing observations are never unmasked before science. No raster bytes enter
the worker. Protect control storage as private data. Shared CLI JSON contains only
run/status, task names/states, destination/manifest and verified successful output
links. It never includes full recipes/AOIs, tokens or raw external exception text.

| Status | Exit | Meaning |
| --- | --- | --- |
| validated, dry_run, succeeded | 0 | Offline validation/estimate, or terminal success: verified GCS inventory (institutional) or completed EE Drive tasks with `raster_verification: pending` (personal). |
| validation | 2 | Input/schema/resource/argument failure. |
| auth | 3 | Identity, credential file, allowlist, permission or required-bucket access failure. |
| no_data | 4 | Empty scene collection or unusable calibration/season. |
| conflict | 5 | Reserved ID, active ownership, changed binding/configuration or failed CAS. |
| uncertain | 6 | Unknown submission/state outcome, unreconciled cancellation, unavailable or undecodable response (including during EE initialization) or refused response encoding. |
| deadline | 7 | Original work deadline reached; inspect remote_pending and original task IDs. |
| cancelled | 8 | Cancellation finalized; submitted tasks are known terminal, other tasks unsubmitted. |
| export | 9 | EE terminal export failure; partial evidence retained and siblings cancelled best effort. |
| running, cancelling | 10 | Persisted nonterminal run or pending stop request. |
| artifact | 11 | Missing/invalid output inventory or private manifest/provenance budget failure. |

## Primary API references

- [Native GCS export](https://developers.google.com/earth-engine/apidocs/export-image-tocloudstorage)
  and [export grids](https://developers.google.com/earth-engine/guides/exporting_images).
- [Export request IDs and operation response](https://developers.google.com/earth-engine/reference/rest/v1/projects.image/export).
- [Storage generation preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions).
- [Attached metadata service](https://docs.cloud.google.com/compute/docs/metadata/overview),
  [job service identity](https://docs.cloud.google.com/run/docs/configuring/jobs/service-identity)
  and [EE service accounts](https://developers.google.com/earth-engine/guides/service_account).

The installed EE newTaskId/exportImage/Task.start/status and google-auth metadata
refresh implementations were also inspected. Live identity, IAM, real EE/GCS
timing/reconciliation and Cloud Run remain unqualified. Accepted regional rasters
and installed Linux observations have the specific limits in
[regional evidence](regional_release.md).

## Pending live snapshot equivalence

Offline dependency tests establish graph ancestry, not server-side equality.
Separate authorization is required for a small live
comparison of catalog-loaded and explicit-snapshot paths. That comparison must
check identical `system:index` identities and properties, Cloud Score+ linkage
by that index and resulting masks, and seasonal grouping by acquisition time,
spacecraft and orbit (including same-observation tiles). Compare selected groups,
thresholds and output pixels on the same bounded grid and dates. No live
snapshot-equivalence claim is made here; scientific defaults are unchanged.
