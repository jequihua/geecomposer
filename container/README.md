# Container inputs and qualification boundary

This directory records public dependency/registry evidence, not a container
qualification receipt. Linux container qualification is pending. Docker use requires separate
authorization before any build/run/inspection.

`base-image.json` records the official Python 3.11.16 slim-trixie Linux/amd64
manifest. Its SHA-256 was checked against manifest bytes and the registry digest
header using anonymous public metadata access. No image layers or engine were
accessed. Both Dockerfile base references use that platform manifest digest.
The tag is descriptive; the digest selects the content. ARM64 is not qualified.

`runtime-linux.lock` was resolved from package pyproject dependencies with uv
0.12.18, targeting Python 3.11 and x86_64-unknown-linux-gnu. It is not a Windows
freeze. The 36 pinned runtime distributions have hashes. A separate three-package
build lock pins setuptools, wheel and packaging. The build uses base-image pip
(the base digest pins that tool); uv is venv-local maintenance tooling, not an
image runtime dependency. Hash lists emitted by uv can include other platform
artifacts; target-platform resolution and Linux wheel availability were checked
separately. Hashes establish artifact integrity, not suitability or licensing.

The runtime resolution includes EE 1.7.45 and google-auth 2.58.0, while the current
Windows regression environment uses EE 1.7.43 and google-auth 2.57.1. Linux import,
algorithm fixture/API compatibility and full offline regressions against the
resolved environment remain qualification gates. Never report Windows tests as
a test of the Linux lock.

## Repeatable update (metadata/dependency work only)

Use the repository venv. From the package root, POSIX uses `.venv/bin/python`;
PowerShell uses `.venv/Scripts/python.exe`:

```text
.venv/Scripts/python.exe -m pip --isolated install --index-url https://pypi.org/simple uv==0.12.18
.venv/Scripts/python.exe -m uv pip compile pyproject.toml --python-platform x86_64-unknown-linux-gnu --python-version 3.11.16 --no-python-downloads --generate-hashes --only-binary :all: --no-header --no-annotate --default-index https://pypi.org/simple --output-file container/runtime-linux.lock --no-cache
.venv/Scripts/python.exe -m uv pip compile container/build.in --python-platform x86_64-unknown-linux-gnu --python-version 3.11.16 --no-python-downloads --generate-hashes --only-binary :all: --no-header --no-annotate --default-index https://pypi.org/simple --output-file container/build-linux.lock --no-cache
```

For an intentional upgrade, add `--upgrade`, review all transitive changes and
repeat Linux qualification. For a repeat without upgrade, existing pins constrain
uv's selection. Do not run runtime pip at container startup.

For a maintained Python patch update, inspect the
[official Python image](https://hub.docker.com/_/python) and its public
`https://hub.docker.com/v2/repositories/library/python/tags/TAG` metadata. Select
linux/amd64, retrieve its manifest from the public registry, verify both the
content hash and Docker-Content-Digest header, and update both FROM lines and the
provenance record together. Never copy an unverified example digest. After any
base/lock change, rerun all qualification gates; this document does not authorize
Docker or publication. [uv platform resolution](https://docs.astral.sh/uv/pip/compile/#cross-platform-resolution)
is metadata resolution, not Linux execution.

Wheel availability can be checked into disposable external scratch using
`pip download --no-deps --require-hashes --only-binary=:all: --platform
manylinux_2_28_x86_64 --platform manylinux2014_x86_64 --python-version 311
--implementation cp --abi cp311 -r runtime-linux.lock --dest SCRATCH` (with the
package-relative lock path). `--no-deps` here avoids Windows pip evaluating
Windows-only dependency markers; uv performed the Linux dependency resolution.
The actual Linux image install must resolve/check the complete hash-locked set.

## Contents and license notices

The context is **the package root only**, never the development repository.
The deny-by-default `.dockerignore` admits Python package source, package metadata,
README/LICENSE and two locks. It excludes tests, notebooks, receipts, examples,
credentials, dotenv files, virtual environments and runtime policy. A general
wheel excludes JSON package data even if a runtime_policy.json was accidentally
left beside cli.py. The hosted stage adds validated policy from a required
BuildKit secret outside context and checks its canonical hash against
POLICY_SHA256, which also invalidates the policy layer cache on change.

The MIT project license is copied to `/usr/share/doc/geecomposer/LICENSE` and
wheel dist-info. Dependency dist-info/licenses and bundled binary license files
remain installed; do not strip them to shrink an image. Release qualification
must inventory all notices and bundled GDAL/PROJ/GEOS/NumPy libraries and inspect
actual image content. No licensing audit or image secret scan has run here.

## Reviewed installed test-tree pruning — M003-S09, 2026-09-25

The r2 continuation found executable dependency tests in the runtime despite
passing 759 Linux package tests. `runtime_content.py` now removes only the
following installed trees in the **builder venv, before runtime COPY**. It runs
after hash-locked installation and `pip check`, with `/opt/venv/bin/python`.
The helper itself is explicitly admitted by `.dockerignore`, copied to `/build`,
and never copied into a runtime target. No dependency, base or lock pin changes.
This is an offline implementation; the changed images have not been built.

The contract binds the exact raw `runtime-linux.lock` SHA-256
`84c29e6bcc93eaf8f76a0fd87458173ddfacb25a9a756540baaff35c7dbf45d0` and all 36
installed versions plus geecomposer 0.1.0. A changed lock/version, missing tree,
unexpected file in place of a directory, empty tree, missing retained file,
symlink/junction/reparse path or special file fails before deletion. This is
not a glob-based test remover. Review this inventory when updating the lock.

| Locked distribution | Approved installed trees | Reason / retention |
| --- | --- | --- |
| numpy 2.4.6 | `numpy/_core/tests`, `numpy/_pyinstaller/tests`, `numpy/distutils/tests`, `numpy/f2py/tests`, `numpy/fft/tests`, `numpy/lib/tests`, `numpy/linalg/tests`, `numpy/ma/tests`, `numpy/matrixlib/tests`, `numpy/polynomial/tests`, `numpy/random/tests`, `numpy/testing/tests`, `numpy/tests`, `numpy/typing/tests` | Upstream executable suites and their fixtures; preserve `numpy.testing` and its `_private` support code, all other runtime modules/native libraries/data. |
| pandas 3.0.6 | `pandas/tests` | Upstream suite/assets; preserve pandas runtime and `_testing` support APIs. |
| geopandas 1.1.4 | `geopandas/tests`, `geopandas/io/tests`, `geopandas/tools/tests` | Upstream suites; preserve IO/tools implementations and `geopandas.testing`. |
| shapely 2.1.2 | `shapely/tests` | Upstream suite/assets; preserve geometry implementations, support APIs and GEOS libraries. |
| pyogrio 0.13.0 | `pyogrio/tests` | Upstream suite/assets; preserve IO runtime, bundled GDAL libraries and data. |
| earthengine-api 1.7.45 | executable suite/assets under `ee/tests`, except the two JSON files below | Remove executable upstream tests and their bytecode; preserve `ee/apitestcase.py` and required qualification fixtures. |
| certifi 2026.7.22 | `certifi/tests` | Inspection of `test_certify.py` confirmed a unittest suite checking CA data/type-file presence; preserve `cacert.pem`, `core.py`, `py.typed` and all runtime files. |

Only these 22 trees are traversed for deletion. Bytecode inside them is removed
with the suites; no global bytecode purge occurs. Broad candidates such as
`cryptography/hazmat/bindings/_rust/test_support.pyi` remain unchanged. The
installed Linux candidate inventory supplied the exact tree list. Host inspection
used metadata-record paths only; host dependencies were never pruned.

### Intentional qualification support and notice retention

`ee/apitestcase.GetAlgorithms` reads `ee/tests/algorithms.json`; the public
`tests/conftest.py::real_ee` calls it to initialize offline API definitions.
Keep that exact JSON file at its original location. Also keep
`ee/tests/cloud_api_discovery_document.json`: the preserved apitestcase helper's
`UsingCloudApi` / `_GenerateCloudApiResource` API reads it. No public package test
currently calls that second API, but removing its data would break a preserved
support API. No other file or subdirectory remains under `ee/tests`.
The inspected host helper is EE 1.7.43; the prior exact 1.7.45 Linux inventory
contains both fixture paths and its public suite passed. Fresh locked-image
qualification must confirm these helper/fixture relationships after pruning.

The content contract is therefore **no executable bundled dependency suites**,
with explicit exceptions for these two data fixtures and legitimate support APIs.
The earlier reports are not rewritten to hide their failures. A filename that
contains `test` alone is neither a deletion authorization nor a content failure.

Before any deletion the helper inventories notice/license/licence/copyright/
copying/authors paths inside approved trees. It writes byte-identical copies to
`/opt/venv/pruned-notices/DISTRIBUTION/ORIGINAL_RELATIVE_PATH`, preserving original
attribution and SHA-256 in `inventory.json`. This directory is outside
site-packages and travels with the cleaned venv. Any copy failure stops before
pruning. The observed Linux inventory had no named notice files inside the
selected trees; synthetic tests still cover nested notice retention. All dist-info
licenses, project MIT, runtime/native library notices and geospatial data outside
those trees stay in place. No claim of a complete legal audit follows from names.

The general wheel and upstream wheel artifacts remain untouched. Installed
dist-info metadata and RECORD stay unchanged, so RECORD intentionally still lists
removed test paths: **the pruned installed distribution is not a complete wheel
RECORD installation**. `inventory.json` records the lock, versions, exact trees,
retained fixture/support hashes and relocated notices. It is a build receipt,
not a cryptographic attestation against a malicious image producer.

### Future qualification

The separate development evidence checker `runtime_content_check.py` loads this
reviewed contract plus its lock through explicit read-only mounts. It checks
versions, absence of known suites, the exact EE fixture exception, retained
support hashes, preserved notices and actual imported package origins. It does
not scan every filesystem path or exported layer and does not replace those
gates. Keep it/test tools outside production targets.

Fresh immutable images must be built only after review, canonical commit and
authorized projection into FRONT. Re-run the entire installed Linux public suite,
offline CLI/graph paths, hosted policy/cache checks, non-root permissions and
actual final filesystem/all exported layers/notices against those new IDs.
Account for installers inherited from upstream base layers separately. None of
the old successful runtime checks qualify the changed image; M001-S01 and
M003-S03-QUAL-F1 remain incomplete.

## Round 2 installed-content rule and pending Linux assumptions — 2026-09-25

The round-1 observations above remain historical. The installed-content pass/fail
rule now includes `runtime_content.check()`'s read-only residual scan of **all
site-packages**, independently of the 22 pruning roots and distribution RECORDs.
It rejects every unreviewed relative path with a `test` or `tests` component,
or a `test_*` / `*_test` Python module basename. Matching is case-insensitive;
exceptions below are exact case-sensitive relative file paths. Python source,
stubs and windowed modules (`.py`, `.pyi`, `.pyw`), bytecode (`.pyc`, `.pyo`,
including cache/optimization tags) and extension modules (`.so`, `.pyd`, including
ABI/platform tags) are covered. Non-Python filenames containing `test_` alone
are not automatically classified as modules.

The exact existing file exceptions are:

- `ee/tests/algorithms.json`
- `ee/tests/cloud_api_discovery_document.json`
- `ee/apitestcase.py`
- `numpy/testing/__init__.py`
- `numpy/testing/_private/utils.py`
- `cryptography/hazmat/bindings/_rust/test_support.pyi`
- `certifi/cacert.pem`
- `certifi/core.py`
- `certifi/py.typed`

Required directory ancestors of these files may exist, but this does not exempt
siblings or subtrees: e.g. `ee/tests/extra.json` and a compiled sibling of the
cryptography stub fail. Retained-file presence and hashes still must pass the
existing check. Empty test/test directories fail too, unless they are required
ancestors; an empty `ee/tests` still fails the retained-fixture check. A path not
matching the rule (e.g. the `numpy.testing` API directory) is not a blanket
allowlist: a new `numpy/testing/test_extra.py` fails.

Diagnostics sort all candidates by relative POSIX path without reading their
contents or reporting host-root paths. The scan rejects links, Windows reparse
points, special files and unreadable traversal entries; unsafe directories are
never descended into. It never deletes candidates or adds exceptions. Rejection
means review is required, not that every match is dispensable. An unrelated
unlisted suite remains untouched by prune but now prevents its final check from
passing. All scans in this coding round used only tiny test-created directories;
no scan or cleanup ran against the host venv.

The separate qualification checker calls this strengthened check without a
bypass and is unchanged. The original broad `continuation_runtime_inventory.py`
remains immutable historical evidence, **not** the acceptance command for the
retained fixtures. A successful residual scan supplements independent full
filesystem/all-layer and notice inspection; it cannot detect arbitrarily named
test content or establish complete secret/license coverage by itself.

Fresh locked-Linux requalification must explicitly establish all of the following;
none is an observed post-prune image success or authority for Docker now:

1. Each of the 22 listed trees actually exists with the expected layout in the
   installed locked Linux wheels before pruning. Do not ignore missing trees or
   add newly discovered candidates to the removal list automatically.
2. In EE **1.7.45**, inspect and exercise apitestcase's fixture reads/imports after
   `ee/tests/__init__.py` is removed. Confirm GetAlgorithms and UsingCloudApi /
   _GenerateCloudApiResource work with the two retained JSON files and do not
   require imports from the removed `ee.tests` suite. Host 1.7.43 observations
   do not prove this; retain network/credential guards and evaluate no pixels.
3. Run `python -m pip --python /opt/venv check` in the builder **after** pruning,
   using the builder's existing pinned pip, then verify real installed runtime
   imports/origins and the full Linux public regression suite. The frozen
   Dockerfile currently runs pip check before pruning; that recorded result is
   not the post-prune check. This pending check is separate qualification work,
   not a Dockerfile change in round 2.
4. Inspect `/opt/venv/pruned-notices` in the final filesystem and application
   layers: inventory/archive presence even if no notices were relocated,
   attribution to distribution/original path, every preserved byte hash, root
   ownership and non-writability for UID 65532 with writable root as well as
   read-only root. Include inventory.json, directories and any notice files.
   Retain and inspect the other dependency/native-library notices independently.

These checks accompany fresh CLI, hosted policy, permissions, image equivalence
and all-layer checks on new immutable IDs; previous image successes are history.
