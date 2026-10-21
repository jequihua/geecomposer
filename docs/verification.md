# Offline engineer verification

For public release verification, run from the committed standalone FRONT root
containing pyproject.toml. The same verifier can run in the canonical DEV package
subdirectory during development. Use an existing Python 3.11+ environment with the package dependencies and
`dev` extra installed. The entry point never installs dependencies or accesses
an index. Initial environment setup requires an approved package source.

```powershell
.venv/Scripts/python.exe tools/verify_offline.py
```

```sh
.venv/bin/python tools/verify_offline.py
```

Prefer this gate to a plain pytest invocation when reviewing a synchronized
checkout. Editable installation can leave `src/geecomposer.egg-info`, and ordinary
pytest can leave caches under managed directories. Git may ignore them, but a
repeat sync can still propose deleting them. Review the exact sync plan and
reconcile each unexpected path before applying; never use a routine overwrite
flag or broad cleanup to conceal drift.

An absolute path to another prepared venv interpreter also works. The gate uses
that same interpreter for every child. It redirects home/config into disposable
external scratch, clears credential variables, disables pytest plugin autoload,
blocks sockets/DNS and credential discovery, checks source origins, runs isolated
Ruff E4/E7/E9/F checks, builds and installs a wheel without indexes/dependency
installation, then checks wheel imports and the installed CLI's help/validate/
dry-run paths for all nine recipes. This is a regression guard, not an OS sandbox.
It fails on test failures, missing public Job/example assets and every test or
collection skip, with one exception decided from pytest reports: on Windows,
which has no POSIX FIFOs, `tests/test_personal_cli.py::test_posix_fifo_records_never_block`
must be collected and skipped by its own mark with reason `POSIX FIFOs are
unsupported on this platform`. The gate then prints that permitted skip and
continues through lint, wheel and CLI checks. That run is not FIFO evidence; the
real regression still needs a POSIX/Linux run. Elsewhere the gate allows zero
skips, and that test must execute and pass.

Two development-only modules are intentionally omitted: test_live_diagnostics.py
(the private experiment diagnostic harness) and test_verification_gate.py
(the development repository gate). Package tests including deployment and the
unstarted GCS example run here without missing-asset skips. The public verifier
also excludes these two modules when run from the canonical development package.
Raw validation/remediation receipts and development governance are not shipped.

Live examples require explicitly selected authorized inputs and credentials.
The offline gate does not run notebooks, live exports, Docker or cloud commands.
Accepted historical Linux tests (1,750 passes, no skips, including FIFO) and four
January regional rasters are summarized in [regional release](regional_release.md).
A current Windows gate does not re-execute those Linux or server measurements,
and cannot qualify a newly built hosted policy/image or institutional IAM.
See [qualification limits](qualification.md) and [operator procedure](cloud_run_operator.md).
