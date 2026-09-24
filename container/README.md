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
