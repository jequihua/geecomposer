# Qualification status and limits

As of 21 October 2026, bounded regional coarse products and installed Linux
behavior have accepted evidence. Institutional Cloud Run/GCS use remains
unqualified. See the self-contained [regional release record](regional_release.md)
for measured products, numerical limits and retained historical failures.

Accepted regional evidence covers January 2025 S1 at 1000/500 m over RBMN and
one-band NDVI100 m/GN Rao250 m on its pinned northeast quarter. All four rasters
have the expected grids, float64/-9999 encoding, consistent masks and no NaN/Inf.
S1 power-to-dB and sampled feature-based Rao checks passed. NDVI lacks independent
red/NIR/count/coverage audit; Rao excludes the outer file border and does not
reconstruct upstream imagery or complete the deferred M004 comparison.

Installed local/builder Linux qualification recorded SDK 1.7.45, 37 distributions,
38 matching modules, 1,750 tests with no skips including FIFO, seven S19 export
checks and 25 matching CLI invocations per image, plus recorded content, notice,
permission and lock checks. This supersedes the early blanket statement that
Linux had never been qualified. It qualifies those pinned artifacts, not a new
hosted image with a site policy. ARM64 and institutional permissions are unproven.
The development Windows environment is CPython 3.11.9 with EE 1.7.43 and
Google-auth 2.57.1; the Linux locks use Python 3.11.16, EE 1.7.45 and
Google-auth 2.58.0. Windows results alone do not test Linux locks.

Historical qualification failures remain evidence. Four intermediate 248/249
host/qualification source originals were unavailable and explicitly waived by
the human under D186. Later final archives do not recover or independently verify
those implementations. D187 accepted the regional envelope with that exact gap.
The reported review-255 metadata-checker separator correction has no distinct
saved execution record; separately recorded tests remain supported.

Earlier synthetic Rao comparisons used a 9 × 9 gradient at 20 m: raw/scalar
normalized and missing-data cases differed by at most 1.39e-17; a multivariate
case by at most 2.50e-16. These are fixture-specific results. Earlier Sentinel
seasonal audits used WorldCover 2020 v100, while the current seasonal default is
2021 v200. They do not validate that changed mask. Empty notebook outputs are
not current live evidence. Regional coarse acceptance is a separate scope.

Offline tests exercise call patterns, numerical oracles, native EE graphs,
recipe validation, identity rejection, recovery and deployment planning. They do
not establish effective permissions or generic server pixel correctness. Run
[offline verification](verification.md) before release review.

Future builds use the committed standalone FRONT root, its existing Dockerfile,
base and locks. Bind DEV/FRONT commits, projection manifest/provenance, locks,
base digest, canonical hosted policy hash, release identifier and immutable
registry digest. Reuse applicable accepted science/Linux evidence, then qualify
the changed hosted policy/image and installed behavior. Retain notices and
failed builds. No Docker or cloud action is performed by these instructions.

A fresh bounded keyless monthly Sentinel-to-GCS smoke must establish attached
runtime identity, terminal EE success, durable state and actual output delivery.
That operational pilot does not close broader M004-S01 science/snapshot/recovery
qualification or M004-S02 final handoff. Annual memory optimization, national
execution and independent M004 Rao remain deferred and nonblocking. Current
limits and future owners are in the [operator procedure](cloud_run_operator.md).
