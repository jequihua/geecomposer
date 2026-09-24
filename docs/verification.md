# Offline engineer verification

Run from the package root containing pyproject.toml. In the development checkout
this is the package subdirectory; in the public checkout it is the repository
root. Use an existing Python 3.11+ environment with the package dependencies and
`dev` extra installed. The entry point never installs dependencies or accesses
an index. Initial environment setup requires an approved package source.

```powershell
.venv/Scripts/python.exe tools/verify_offline.py
```

```sh
.venv/bin/python tools/verify_offline.py
```

An absolute path to another prepared venv interpreter also works. The gate uses
that same interpreter for every child. It redirects home/config into disposable
external scratch, clears credential variables, disables pytest plugin autoload,
blocks sockets/DNS and credential discovery, checks source origins, runs isolated
Ruff E4/E7/E9/F checks, builds and installs a wheel without indexes/dependency
installation, then checks wheel imports and the installed CLI's help/validate/
dry-run paths for all six recipes. This is a regression guard, not an OS sandbox.
It fails on unexpected test skips or missing public Job/example assets.

Two development-only modules are intentionally omitted: test_live_diagnostics.py
(the private experiment diagnostic harness) and test_verification_gate.py
(the development repository gate). Package tests including deployment and the
unstarted GCS example run here without missing-asset skips. The public verifier
also excludes these two modules when run from the canonical development package.
Raw validation/remediation receipts and development governance are not shipped.

Live examples require explicitly selected authorized inputs and credentials.
The offline gate does not run notebooks, live exports, Docker or cloud commands.
See [qualification limits](qualification.md) and [operator procedure](cloud_run_operator.md).
