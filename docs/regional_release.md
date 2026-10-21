# Regional release evidence — 21 October 2026

The bounded regional demonstration was accepted after independent reviews 253
and 255 (development decision D187). It establishes the following four January
2025 products, not institutional operation or national feasibility. Private
geometry, task identities and raw evidence are intentionally not published.

| Product and measured area | Resolution | Raster width × height | Accepted observations |
| --- | --- | --- | --- |
| Sentinel-1 linear VV/VH and dB, full RBMN | 1000 m | 41 × 94 | Six bands; 1,586 valid cells; count, coverage and power-to-dB checks |
| Sentinel-1 linear VV/VH and dB, full RBMN | 500 m | 81 × 187 | Six bands; 5,869 valid cells; power-to-dB maximum difference about 7.11e-15 |
| Single-band NDVI, pinned RBMN northeast quarter | 100 m | 141 × 209 | 13,144 valid and 16,325 nodata cells; structural checks |
| GN Rao and four reflectance features, same pinned quarter | 250 m | 57 × 84 | Eight bands; 1,845 valid Rao and 2,943 nodata cells; 2,157 interior support checks |

All four files matched their prepared CRS, affine grid, extent and band schema.
Every band was float64 with finite `-9999` nodata; GDAL masks agreed with nodata,
and no NaN/Inf was found. S1 dB was checked against `10 * log10(power)` after
aggregation, not an average of dB. S1 uses no Gamma MAP in this coarse workflow.
This is evidence for these rasters and the corrected export boundary, not every
possible geometry or all six presets. Earlier border-NaN failures remain
historical; describing that defect as still unresolved in these results is stale.

NDVI was selected to one output band in the measured campaign. Its underlying
four-band shared validity and spatial-first temporal median were preserved.
The public S2 recipe still exports reflectance, NDVI, count and coverage; it has
no single-band output-selection option. The measured one-band file cannot
independently establish red/NIR arithmetic, observation counts or coverage.

Rao was checked from exported feature bands and one frozen January-only model:
32 complete interior samples agreed to a maximum absolute difference of about
2.08e-17, and 32 incomplete-support samples were correctly masked. The audit
excludes the outer file border; it is sampled, not every-pixel validation or
independent upstream-imagery reconstruction. The separate M004 Rao numerical
comparison remains deferred and nonblocking for this regional release.

D112 accepts only float32-rounded `valid_count / 9` coverage in its exact
precision class (historical maximum error 2.6490953208835322e-08). It does not
waive incorrect counts, material ratio errors, masks, support or Rao agreement.
D179 separately governs native-mask weighting and final coverage admission:
finite positive W and `W >= threshold or abs(W - threshold) <= 1e-12`. Raw W/S
and values remain unchanged. These two policies must not be conflated.

## Capability and operational limits

The [coarse guide](coarse.md) covers 100 m, 250 m, 500 m, 1 km, 5 km and 10 km
presets, named periods and twelve monthly outputs with one shared model for Rao.
Twelve months are supported capability, not twelve observed live successes.
Public examples use illustrative geometry; none reproduces the private measured
RBMN polygon. A dry-run checks syntax and planning, not catalogue or cloud access.

Coarse-first means spatial mean per acquisition, then equal-observation temporal
statistics; NDVI follows the reflectance composite and Rao follows its temporal
median. Changing the order changes the product. A 3 × 3 coarse Rao window spans
750 m at 250 m resolution and roughly 3 km at 1 km resolution; it is a different
scale from fine-grid Rao. See [methods and recipe syntax](coarse.md).

Three annual S2/NDVI attempts ended in provider out-of-memory failures on
21 October 2026, including the one-band attempt. Small output files do not bound
fine-input reads, temporal sort memory or backend work. The successful January
NDVI/Rao requests do not resolve annual processing. There is no automatic
coarsening, estimator change, filter substitution or tested tile prescription.
Annual optimization/qualification and national execution remain deferred;
national execution is indefinitely deferred and is not a rollout prerequisite.

The four successful tasks reported approximately 1624.903, 611.917, 262.271 and
176.582 EECU-seconds respectively (known sum 2675.672). Failed-task usage and
project headroom are incomplete/unknown. These observations are not a cost
estimate, full campaign total or affordability promise for another area/month.

## Qualification and next release

Accepted installed Linux evidence includes SDK 1.7.45, 37 distributions,
38 matching package-module hashes in both measured local/builder images,
1,750 tests with zero skips (including FIFO), seven export-boundary checks and
25 matching CLI invocations per image. Review 255 closed the response-shape
assessor defect with a byte-identical complete 154-comparison replay. Historical
failed controls/builds were retained, not relabelled successful.

D186 retains a human-waived inspection gap for four intermediate host/qualification
source versions from continuations 248/249. Final archives do not recover or
independently verify those missing originals. This caveat remains part of the
accepted regional evidence; it is not a missing-raster or general science waiver.

The next hosted image uses the committed public source and real administrator
policy and needs its affected content, identity-rejection and policy checks.
Institutional keyless execution and GCS delivery remain unverified. Follow the
[operator release chain](cloud_run_operator.md) and
[qualification boundaries](qualification.md); publication, deployment and one
fresh bounded monthly GCS smoke are later, separately authorized actions.
