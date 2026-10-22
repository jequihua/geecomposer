# Coarse-first composites (`geecomposer.coarse`)

`geecomposer.coarse` builds coarse-first products as lazy Earth Engine graphs.
Each acquisition is spatially averaged onto a coarse grid first, then summarized
over time:

- **Sentinel-1:** linear-power VV/VH.
- **Sentinel-2:** reflectance composites (B2, B3, B4, B8).
- **Coarse GN Rao:** strict 3x3 Rao on the coarse Sentinel-2 temporal median.

The functions never evaluate, fetch metadata, authenticate, fit a model or start
an export. The recipe runner performs those explicit steps. The scientific rules
are the adopted M005-S01 contract (A1-A19). Four bounded January 2025 products
have accepted live raster evidence; read [regional release evidence](regional_release.md)
for measured scope, chronology and limits.

## National MX-AEA grid: what is implemented, admitted and measured

The national grid is MX-AEA, an Albers equal-area grid on WGS 84 with a 10 km
origin, at 100, 250, 500, 1000, 5000 and 10000 m. These are three separate
things:

1. **Offline capability (implemented).** `composite`, `composite_periods`
   (including coarse Rao) and the local `fine_first_reference` build lazy
   graphs without remote evaluation. EE initialization/algorithm definitions
   must already be available when building valid EE graphs; the pure admission
   checks reject missing/invalid metadata before that point.
2. **Conditional mathematical admission (D129/D130).** The reviewed geometric
   bound admits a native 10 m → MX-AEA first stage with `maxPixels` 225
   (10 → 100 m) or 81 (10 → 50 m, the 250 m chain). Nested stages keep 144/49,
   and every stage uses `bestEffort=False`. The bound holds only for:
   - **native grids:** EPSG:32611–32616 with exactly `[10, 0, c, 0, −10, f]`;
   - **first-stage cells:** every point of every first-stage target cell must
     lie in latitude 13.5–34.0° and longitude −119.5 to −85.5°, and within 5° of
     longitude of the source zone's central meridian.

   The package checks these conditions per source and per whole cell, curved
   edges included, using exact Albers polar geometry. It never relies on cell
   centres or sampled points alone.
3. **Measured server behaviour (bounded).** The accepted regional campaign
   exercised corrected finite/coverage/median controls and four January exports;
   see [the results and limitations](regional_release.md). The earlier FC01–FC14
   national qualification checklist is not established in full by those runs.
   Remaining generalization questions include:
   - EE's contributor counting and memory at 225;
   - its UTM/Albers arithmetic;
   - whether it confines the clipped reductions;
   - overlap fractions;
   - MX-AEA WKT acceptance;
   - actual catalogue projections, especially Sentinel-1.

   An offline graph is not evidence that a Mexico-wide run fits or succeeds.

**How a national request is assembled.** Each member image (granule) gets its
own MX-AEA rectangle:

- **The rectangle.** It is the coarse-cell-aligned part of the requested cells
  that its native footprint can reach.
- **The check.** That rectangle plus a 1 km halo must satisfy the conditions
  above for the member's zone.
- **The reduction.** It is confined to the rectangle: the input is clipped to
  the rectangle plus 200 m, and the first-stage output is kept only on the
  rectangle's whole cells.

The rest of the science is unchanged:

- granules of one acquisition, even across zones, give one observation per
  cell, choosing the maximum W with ties to the smaller id;
- full-cell denominators;
- the final-only coverage threshold;
- equal-weight temporal statistics;
- the `coveringGrid` cell union of the whole AOI, including islands.

The applied rectangles are recorded in the `coarse:national` property.

**What is refused, before any Earth Engine object is built:**

| Code | When |
|---|---|
| `national_domain` | The AOI, or an admitted rectangle plus halo, leaves the latitude/longitude domain. |
| `zone_offset` | A member's rectangle plus halo is more than 5° from its zone meridian. |
| `source_extent` | A member's native footprint leaves the band where its cover is justified. |
| `source_crs`, `source_pixel`, `source_affine`, `source_orientation` | The native grid is not admitted. |
| `missing_metadata` | No snapshot, or a member has no native dimensions. |

A refused member is never dropped silently. A member whose footprint cannot
reach any requested cell is excluded by geometry, and the exclusion is
recorded. The fine-first benchmark stays local: its whole AOI must fit the one
declared fine grid's zone.

Local studies on one northern UTM zone grid remain supported, with their exact
interval-count bounds:

- 144–169 contributors for 10 → 100 m;
- 49–64 for 10 → 50 m;
- nested 144/49.

A local result is not evidence of national activation.

## Inputs you must supply

Source identity is never inferred from an opaque collection. You pass a
`coarse.Snapshot` built by `coarse.build_snapshot(...)` from two **completed**
reads:

- **Candidate rows (C1):** the product filters and the run interval
  (`coarse.candidate_query`).
- **Completion rows (C2):** the same scope, restricted to the candidate
  acquisition keys, with **no date filter** (`coarse.completion_query`).

Running and storing these reads is the API caller's job; the implemented recipe
runner already performs and persists them. Each row has these fields:

- `id`
- `time_ms` (from `system:time_start`)
- the acquisition key:
  - Sentinel-2: `SPACECRAFT_NAME` and `DATATAKE_IDENTIFIER`;
  - Sentinel-1: `platform_number` and `missionDataTakeID`;
- `projections`: the `{crs, transform}` of every contributing band. For
  Sentinel-2 this includes `cs_cdf`.
  - **National MX-AEA requests** also need each band's native
    `dimensions: [width, height]` (Earth Engine band metadata). The bands of a
    row must agree, and every member must have them, or none.
  - The snapshot keeps them as `extents`. These are validated like the other
    fields and included in the fingerprint.
  - They are caller-supplied metadata from the completed reads, not
    authenticated evidence. A national request without them fails
    (`missing_metadata`).

How the snapshot is built and checked:

- **Grouping.** Whole acquisitions are grouped and assigned to a period by
  their earliest member time. A December acquisition keeps its January members;
  one that began before the run is excluded.
- **Row agreement.** Every candidate id appears once in C1 and once in C2. Its
  normalized time, acquisition key and contributing-band projections must be
  identical in both reads. Other metadata and extra bands are ignored.
  Duplicates or contradictions fail as `uncertain`.
- **Ceilings.** `limit` may only be a stricter integer in 1..1000, the
  default being 1000. At most `limit + 1` rows of each read are consumed, and
  an overflow fails (`limit`).
  - More than 600 candidate acquisition keys fail before any pre-run
    exclusion.
  - More than 400 acquisitions in one period fail when it is built.
  - `candidate_query` and `completion_query` accept the same `limit` and keep
    the `limit + 1` sentinel.
- **Validation everywhere.** `Snapshot` validates and canonicalizes itself on
  every construction path: `build_snapshot`, direct
  `coarse.Snapshot(dataset, start, end, scope, groups, excluded, projections)`
  and `dataclasses.replace`. It checks:
  - unique acquisition keys, with included and excluded keys disjoint;
  - at most 600 keys and 1000 members;
  - sorted unique member ids, each in exactly one acquisition;
  - integer group times inside the run;
  - exactly one projection entry per member, with the same contributing bands
    on one finite native grid.

  Contents are copied into sorted tuples, so later changes to the caller's
  lists cannot change an accepted snapshot. Failures are `snapshot_invalid` or
  `limit`.
- **Binding.** The snapshot is tied to its scope: dataset, AOI, grid, bands,
  mask and filters. Consumers also check that its members carry exactly the
  requested contributing bands. It cannot be reused for another request.

## Science in one paragraph

These rules come from contract §6-§8:

- **Finite values first.** Non-finite source values are replaced before any
  multiplication.
- **Sums and weights.** Sums S and weights W ride on a constant base with
  mask 1. Fractional validity counts once, as a value, and W is built in
  double. In the synthetic server checks of M005-S05 correction247 (constant
  images given `updateMask`, then clipped and mosaicked), a requested 1/2
  mask read back as 128/255 and `where` gave its output the value's mask.
  The stacked S/W value therefore keeps only presence (mask > 0) as its
  mask, so reducer inputs have mask 1 and `reduceResolution` never applies
  the fraction a second time. Those checks do not establish how every
  asset stores masks. An exact fractional weight needs a numeric quality
  value with binary presence.
- **One threshold.** The coverage threshold (default 0.5) applies only after
  the last stage. Valid zeros stay valid; missing support is never written as
  0. Final admission (the observation and the fine-first reference) is
  `finite(W) and W > 0 and (W >= threshold or |W - threshold| <= 1e-12)`:
  an exact half cell can arrive a few units in the last place below 0.5
  after the staged double means. The fixed tolerance is not configurable and
  only decides admission; W, S and values are reported unchanged.
- **One granule per cell.** For each acquisition, each cell takes the granule
  with the largest W; ties go to the smaller id.
- **Temporal statistics.** Mean, exact median and population SD are
  equally weighted per observation. The median uses an explicit sort, not a
  histogram, and the SD uses two passes. Min, max, range, `obs__t_count` and
  `cov__t_mean` complete the set. Each observation is cast to double once
  before any collection is built: collection sums, min/max and the sorted
  median need one band type, and inferred types otherwise follow the values.
- **NDVI** is computed from the median or mean reflectance composites.
- **Sentinel-1** is linear power without Gamma MAP. dB is optional and applies
  only to mean, median, min and max.

## Example (national, monthly)

```python
from geecomposer import coarse, grouping

grid = coarse.grid(1000)  # MX-AEA; any of 100, 250, 500, 1000, 5000, 10000
snapshot = coarse.build_snapshot(dataset="sentinel2", aoi=aoi, grid=grid,
                                 start="2025-01-01", end="2026-01-01",
                                 select=["B2", "B3", "B4", "B8"], mask="s2_cloud_score_plus",
                                 candidate_rows=c1_rows, completion_rows=c2_rows)
months = coarse.composite_periods(grouping.monthly_periods(2025), snapshot=snapshot,
                                  dataset="sentinel2", aoi=aoi, grid=grid,
                                  select=["B2", "B3", "B4", "B8"], mask="s2_cloud_score_plus",
                                  temporal=("median",), index="ndvi")
```

`composite_periods` returns twelve images keyed `2025-01` … `2025-12`. Empty
months keep the band schema with masked statistics and `obs__t_count = 0`.

- **Rows.** `c1_rows`/`c2_rows` must carry band `dimensions` for a national
  grid.
- **Other datasets.** The same call works with `dataset="sentinel1_float"`
  (linear VV/VH, explicit `orbitPass`, no Gamma MAP).
- **Local studies.** A UTM `RaoGrid` uses the same call; national-only
  metadata is then optional.

For coarse Rao, fit one model **separately** with the existing
`geecomposer.rao.fit_normalization`. The default reference is the year's coarse
median over `coarse.cell_region(aoi, grid)`. Then pass the frozen model:

```python
rao_months = coarse.composite_periods(
    grouping.monthly_periods(2025), model=model, snapshot=snapshot,
    dataset="sentinel2", aoi=aoi, grid=grid,
    select=["B2", "B3", "B4", "B8"], mask="s2_cloud_score_plus",
    temporal=("median",),
)
```

The model must hold **exactly** the four features
`B2/B3/B4/B8__s_mean__t_median`, once each, in any declared order. A subset, a
duplicate, an extra band or another suffix fails (`model_bands`). The model's
order and fingerprint are kept, and the request's `select` must follow that
order. Period functions never refit. Each Rao output has eight bands:
`coarse_rao_gn`, `coarse_rao_valid_count`, `coarse_rao_coverage`,
`obs__t_count` and the four features. Its physical support is 3X x 3X.

`coarse.fine_first_reference` and `coarse.reference_rao` are **benchmark-only**.
The reference differs from the coarse product in three ways:

- it takes the lowest-ID granule per fine pixel;
- it resamples Sentinel-1 to the fine grid by nearest neighbour;
- it applies the operations in the opposite order.

Its differences from the coarse product are measured, not expected to vanish.

A reference period with no acquisition keeps the same schema: the
`<band>__t_<stat>__s_mean` bands in order, then `cov__s_mean`, on the target
grid with the benchmark properties. Its statistics are masked, and
`cov__s_mean` is 0 on the target cell union and masked outside it. No
observation is invented. `reference_rao` applies the same four-feature rule and
maps the reference features into the model's order by name.

## Recipes and CLI

The same products run through ordinary recipes with `"workflow": "coarse"`.
See the coarse section of [the runner guide](runner.md), with its three monthly
2025 example recipes. A recipe run:

- reads the C1/C2 metadata, including native `dimensions`, once per run;
- persists the snapshot and any frozen model privately and replays them on
  resume;
- builds every period with the same calls shown above;
- exports one unstarted task per populated period and tile core, within the
  twelve-output ceiling.

`validate` and `run --dry-run` stay credential-free and label scene counts,
EECU and runtime as unknown. Notebook callers can use the Python API directly
with the same snapshot and model.

National execution is indefinitely deferred and requires new bounded authority.
Regional acceptance does not establish annual/national completion, cost or
catalogue coverage. The shipped geometries are public illustrations, not the
private measured RBMN geometry. The one-band campaign NDVI selection is not a
public recipe option.

## Resolution and operation order

| Preset | Spatial chain in metres | Fixed 3 × 3 Rao support width |
| --- | --- | --- |
| 100 m | 10 → 100 | 300 m |
| 250 m | 10 → 50 → 250 | 750 m |
| 500 m | 10 → 100 → 500 | 1.5 km |
| 1 km | 10 → 100 → 1000 | 3 km |
| 5 km | 10 → 100 → 1000 → 5000 | 15 km |
| 10 km | 10 → 100 → 1000 → 10000 | 30 km |

All presets use spatial mean first. Temporal summaries are `mean`, exact `median`,
`sd_pop` (population SD), `min`, `max` and `range`; `obs__t_count` and
`cov__t_mean` report observation support. Reflectance is unitless after the
0.0001 scale; S1 is linear power, with optional dB for supported statistics.
Shared validity uses contributing bands together; zero is valid, absent support
is not. Coarse S1 requires an explicit orbit pass and adds no terrain flattening
or speckle filter. These limits affect interpretation of backscatter differences.

The products differ by order: (1) spatial mean per acquisition → temporal
summary; (2) for coarse Rao, spatial mean → temporal median → strict 3 × 3 GN Rao;
(3) the fine-first benchmark summarizes time on the fine grid before spatial
aggregation, with reference Rao applied to those reference features. The
benchmark's different granule selection and S1 resampling also matter; the
comparison does not isolate temporal ordering alone. None is a silent fallback.

API periods are `(label, start, end)` tuples, for example
`(("jan", "2025-01-01", "2025-02-01"),)`; `grouping.monthly_periods(2025)`
returns twelve ordered half-open UTC months. Recipes use objects instead:
`"periods": [{"label": "jan", "start": "2025-01-01", "end": "2025-02-01"}]`
inside `coarse`, or `"periods": "monthly"` for a calendar-year run. Periods must
be unique, ordered and non-overlapping, at most twelve. One shared snapshot and,
for Rao, one frozen model apply across periods/cores. The annual reference fit
in the illustrative Rao recipe is a choice, not proof of annual feasibility;
the accepted regional Rao fit used January only.

Three annual S2/NDVI attempts failed with provider OOM, including one-band output.
A smaller final grid/file does not eliminate native input work or exact temporal
sort memory. No untested tile settings, alternative filtering, approximation or
automatic fallback are prescribed. Twelve monthly outputs are implemented
capability, not twelve successful live exports. Annual optimization, national
execution and the independent M004 Rao comparison remain deferred/nonblocking.
The 1000-read, 600-key and 400-per-period limits remain; no national result or
cost promise follows. A fresh hosted image and institutional operational smoke
are still separate gates in [the operator guide](cloud_run_operator.md).
