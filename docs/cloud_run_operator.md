# Cloud Run operator procedure — prepared, not deployed

Institutional provisioning remains pending. No institutional identity, project, bucket, registry or grant is
verified by this kit. Preparation is available now; Docker, push, deployment,
upload and job execution each need their specified later authority. No command
below is a request for an agent to run it. Docker use requires separate authorization; Linux container qualification remains pending.

Use the [administrator request](institutional_admin_request.md) and
[runner permission/recovery contract](runner.md).
Runtime uses an attached, verified keyless service account. Build/deploy and
institutional invocation identities are separate. The upload helper uses the
operator's preconfigured institution-approved Storage client identity, while
execution uses the institution-approved gcloud identity. The helper never logs
in; it does not make a personal ADC account institutional. Administrators must
bind both tools to the approved operator identity before authorizing mutations.

## Offline preparation and target contract

The one package Dockerfile has `local` and `hosted` targets with the same wheel,
runtime locks and numeric UID/GID 65532. There is no port. Runtime package/policy
paths are root-owned and non-writable to that UID. No gcloud, test tooling,
analysis tooling, credentials or runtime pip installation belongs in the image.
Actual enforcement awaits the Linux gates below.

`local`: `--help`, `validate FILE`, and `run --recipe FILE --dry-run` are offline.
`hosted`: `--help`, generation-pinned `run`, `status`, `cancel`, `resume`;
validate/dry-run and caller policy paths remain rejected by the existing CLI.
Use the shared local target for offline equivalence; do not weaken hosted checks.
Off-cloud online local execution still lacks supported attached identity; do not
mount a home directory or keys to make it work.

Prepare an external private administrator policy, never commit it. Use the runner
schema with mode hosted, exact approved resources/prefixes, reviewed ceilings
and a release identifier in image_version. Local policies, wildcards and
illustrative placeholders fail preparation. After schema validation, preparation
rejects exact values `geecomposer-example`,
`worker@geecomposer-example.iam.gserviceaccount.com`,
`geecomposer-example-input`, `geecomposer-example-output`,
`geecomposer-example-control`, and `replace-with-reviewed-image-digest` in
project/account/release or resource bucket/prefix fields. It also rejects
`${NAME}`, `<NAME>`, `{{NAME}}` tokens (nonempty contents) and uppercase
`__NAME__` tokens (letters, digits and underscores, starting with a letter),
including tokens embedded in a value. It never expands expressions. Other
schema-valid names containing `test-`, `test_` or `example` are accepted;
syntactic acceptance does not establish institutional authority. The release identifier avoids the
circularity of embedding the final image digest in its own policy. Record the
resulting immutable image digest and canonical policy hash together at release.

From the package root with a prepared project venv. Policy preparation is
offline; replace the private path placeholders with administrator-supplied files.

Windows PowerShell:

```powershell
.venv/Scripts/python.exe -m geecomposer.deployment --mode policy --policy PRIVATE_POLICY --output PRIVATE_VALIDATED_POLICY
```

POSIX shell:

```sh
.venv/bin/python -m geecomposer.deployment --mode policy --policy PRIVATE_POLICY --output PRIVATE_VALIDATED_POLICY
```

Outputs are exclusive UTF-8 files; existing files are never overwritten. Do not
use PowerShell's legacy text redirection to create JSON. The general wheel never
contains policy. A hosted build later requires the validated file as a BuildKit
secret plus its hash as POLICY_SHA256. Policy is configuration, not credentials,
but still belongs in controlled image storage, outside caller mounts.

The repository job template `deploy/cloud_run/job.yaml` uses JSON syntax (valid
YAML) to avoid a YAML parser dependency. Parameter substitution is structural.
Supply both the project ID in policy and its administrator-confirmed project
number for the YAML namespace; the execution precheck confirms the number against
the job returned under that project ID. The rendered job uses the supplied immutable Artifact Registry image digest,
attached runtime identity, task count/parallelism 1, CPU 1, memory 1Gi, timeout
2100 seconds and maxRetries 0. Default args only show help; deploy never executes.
The application still caps work at 1800 seconds. No output override is introduced:
recipe destinations remain bounded by image policy.

PowerShell preparation (values below come from the approved private handoff;
variables are not literal resource placeholders in a production policy):

```powershell
$python = '.venv/Scripts/python.exe'
$common = @('--policy', $Policy, '--job-template', 'deploy/cloud_run/job.yaml',
  '--image', $ImageDigest, '--project-number', $ProjectNumber, '--region', $Region, '--job', $Job,
  '--run-id', $RunId, '--recipe', $Recipe, '--uri', $InputUri)
& $python -m geecomposer.deployment @common --output $Plan
& $python -m geecomposer.deployment @common --mode render --output $JobYaml
```

POSIX shell preparation:

```sh
python=.venv/bin/python
set -- --policy "$Policy" --job-template deploy/cloud_run/job.yaml \
  --image "$ImageDigest" --project-number "$ProjectNumber" --region "$Region" --job "$Job" --run-id "$RunId" \
  --recipe "$Recipe" --uri "$InputUri"
"$python" -m geecomposer.deployment "$@" --output "$Plan"
"$python" -m geecomposer.deployment "$@" --mode render --output "$JobYaml"
```

Review private outputs; a plan has no side effects and is not permission to run.
The recipe URI must be a new object within an approved input prefix. Use a
run-specific name; never reuse an unresolved input upload. The plan omits the
full recipe/AOI. Actual policies/recipes/plans/receipts stay outside tracked trees.

## Separate cloud stages, only after exact authorization

1. **Registry push:** after Linux qualification, a build identity publishes the
   reviewed hosted image to the approved Artifact Registry. Record the remote
   digest, policy hash, source revision, base digest, locks and notices. Never
   deploy a moving tag or mistake a local image ID for a registry digest.
2. **Deploy:** first complete the mandatory digest/label verification below.
   The deploy identity reviews the same unchanged JobYaml and runs
   `gcloud run jobs replace JOB_YAML --project PROJECT --region REGION`. This
   does not execute. The deploy identity needs narrow Run deployment authority,
   service-account-use on the runtime identity and approved image access; it
   does not need runtime EE or raster privileges. No helper mode pushes/deploys.
3. **Upload only:** using the same shell argument array from preparation, add
   `--mode upload --output RECEIPT`. It validates before an SDK upload with
   if_generation_match=0, timeout 10 and retry=None. The receipt binds the
   returned generation, URI, canonical recipe hash and policy hash. An uncertain
   acknowledgement stops; inspect that original object rather than blindly
   repeating or replacing it. Only input object create is needed for new uploads;
   inspection/read access is a separate institutional decision.
4. **Execute only:** add `--mode execute --receipt RECEIPT` to the same arguments.
   It first describes the job and verifies image, policy-hash annotation,
   attached account and budgets, then invokes one execution with exact runner
   args through a temporary JSON flags file and shell=False. It sets no env,
   task-count or timeout overrides. Calls have 30/60-second timeouts and no
   helper retry. gcloud's own internal transport behavior is outside this helper;
   timeout/nonzero means unconfirmed, never proof of no execution. Inspect job
   executions and the original run ID before retry. Success means submitted,
   not completed. Never run execute-on-deploy.

For PowerShell, append flags to `& $python -m geecomposer.deployment @common ...`;
for POSIX append to `"$python" -m geecomposer.deployment "$@" ...`. This avoids
hand-escaping recipe JSON. Local file paths can contain spaces/metacharacters;
remote identifiers are validated and never interpreted as shell fragments.
[gcloud JSON flags files](https://docs.cloud.google.com/sdk/gcloud/reference/topic/flags-file)
carry list values without comma/shell escaping. `--output` must name a fresh
private file; if persistence fails after a remote mutation, inspect remote state.

The pre-execution check is a point-in-time check, not a transaction with job
updates. Freeze deployment changes while launching; only authorized deployers
may modify that job. The policy hash annotation is a reviewed binding to the
qualified image, not independent proof of image contents. The helper must not
be treated as a multi-user security broker.

## Mandatory digest/label verification before deployment

After an authorized push and before every deploy (including rollback), inspect
**the exact immutable registry digest in the rendered Job**. Plan/render and
execute do not inspect the image or its label. The Job annotation alone is an
operator claim, not proof. Stop if the pull/inspection fails, digest identity is
unverified, or any label/hash is missing, malformed or unequal. Never substitute
a tag or a local image ID for the registry digest.

For later authorized use only, save this bounded checker as `VerifyBinding` in
external private storage. It uses the existing package interpreter, canonical
validated hosted policy, rendered JSON Job, and Docker's image-config inspection.
`Inspection` and `BindingReceipt` also name fresh private files outside tracked
trees. Set `ImageDigest` to the approved pushed digest, `Policy` to the validated
policy and `JobYaml` to the reviewed rendered Job. Retain the push record's digest
and policy hash, then retain this deploy receipt alongside it; reconcile both
before deployment. Never edit the Job, policy or digest between verification and
deploy; repeat verification if any changes.

```python
import json
import re
import sys
from pathlib import Path
from geecomposer.deployment import hosted_policy
from geecomposer.recipe import canonical, digest, read_json

policy_path, job_path, inspection_path, image, receipt_path = sys.argv[1:]
if not re.fullmatch(
    r"[a-z][a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+/"
    r"[a-z0-9_/-]+@sha256:[0-9a-f]{64}", image
):
    raise SystemExit("STOP: immutable Artifact Registry digest required")
policy_hash = digest(hosted_policy(read_json(policy_path)))
job = read_json(job_path)
inspected = json.loads(Path(inspection_path).read_text(encoding="utf-8"))
if not isinstance(inspected, list) or len(inspected) != 1:
    raise SystemExit("STOP: exactly one inspected image required")
config = inspected[0]
job_image = job["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"]
label = (config.get("Config", {}).get("Labels") or {}).get("org.geecomposer.policy-sha256")
annotation = job["metadata"]["annotations"].get("geecomposer-policy-sha256")
if image != job_image or image not in (config.get("RepoDigests") or []):
    raise SystemExit("STOP: inspected registry digest does not match Job")
if not isinstance(label, str) or not re.fullmatch(r"[0-9a-f]{64}", label):
    raise SystemExit("STOP: missing or malformed image policy label")
if label != policy_hash or annotation != policy_hash:
    raise SystemExit("STOP: image, validated policy and Job hashes differ")
record = {
    "image_digest": image, "policy_sha256": policy_hash,
    "image_policy_label": label, "job_policy_annotation": annotation,
    "rendered_job_sha256": digest(job),
}
with Path(receipt_path).open("xb") as output:
    output.write(canonical(record) + b"\n")
```

Any checker exception or nonzero exit is a deployment stop, including malformed
inspection/Job JSON. PowerShell (the explicit exit checks also work in Windows
PowerShell; use UTF-8 without a BOM for inspection JSON):

```powershell
& docker pull --platform linux/amd64 $ImageDigest
if ($LASTEXITCODE -ne 0) { throw 'STOP: digest pull failed' }
$inspectionJson = & docker image inspect $ImageDigest
if ($LASTEXITCODE -ne 0) { throw 'STOP: digest inspection failed' }
if (Test-Path -LiteralPath $Inspection) { throw 'STOP: inspection file exists' }
[System.IO.File]::WriteAllText($Inspection, ($inspectionJson -join "`n"),
  [System.Text.UTF8Encoding]::new($false))
& $python $VerifyBinding $Policy $JobYaml $Inspection $ImageDigest $BindingReceipt
if ($LASTEXITCODE -ne 0) { throw 'STOP: binding verification failed' }
```

POSIX (run as a script; any failed step exits before deployment):

```sh
set -eu
set -C
docker pull --platform linux/amd64 "$ImageDigest"
docker image inspect "$ImageDigest" > "$Inspection"
"$python" "$VerifyBinding" "$Policy" "$JobYaml" "$Inspection" "$ImageDigest" "$BindingReceipt"
```

These checks rely on the authorized Docker daemon/registry client verifying
content addressed by the pulled digest and reporting its config faithfully.
The config label is a build assertion: the reviewed Dockerfile's policy-hash
check and controlled build/push provenance remain necessary. A malicious builder
can label different content dishonestly. Protect build identity, registry access,
private receipts and deployment edits; this procedure is not registry attestation
or Linux qualification. The helper's later describe check binds the live Job to
the verified digest/annotation but cannot replace this pre-deploy image check.

## Cohort isolation, observation and recovery

An invoker using argument overrides needs `run.jobs.runWithOverrides` as well as
execution permission, plus `run.jobs.get` for the helper's check. The
`roles/run.jobsExecutorWithOverrides` role is a candidate for administrator
review, not a grant here. The same authority can bypass the helper and override
other execution settings; only trusted cohort operators may hold it. Protect
job update, image write, command/env overrides and Python import paths. Keep
conflicting data entitlements in separate jobs/accounts/buckets, or obtain a
separately authorized broker; policy alone is not a sandbox for arbitrary code.
Prefix allowlists do not make Storage listing prefix-private. Result readers
receive separate approved object-read access, never public sharing by default.

Use the helper with `--action status|cancel|resume`, the original run ID, policy,
image, job and region, omitting recipe/URI/receipt. Default plan stays offline;
explicit execute invokes that runner action. `status` is persisted state; resume
reconciles original operation markers before taking ownership. Do not make a
new run ID to retry uncertain work. Wait for the 120-second lease after a crash.
Cancel on a live owner records a stop request; after owner failure invoke cancel
again after lease expiry. Never use a platform retry to recover a run.

Platform execution cancellation does not prove EE cancellation. Inspect private
manifest `remote_pending`, each saved operation and its exact `gc-...` discovery
marker. List the configured project's operations and match metadata.description
exactly; inspect/cancel that full operation name and verify terminal state. The
runner allows at most 5 x 100 listing records, 1 MiB/page and bounded deadlines;
visibility/retention/large project lists can require authorized operator evidence.
Request UUIDs are not operation IDs. Do not erase state or reset intent to prepared.
Auth exit 3 now preserves discovery principal/permission failures; uncertainty
remains exit 6. No raw tokens, recipe/AOI or private exceptions belong in shared logs.

Success requires terminal EE success and nonempty exact-run objects with recorded
generations, shard names and available checksums. Operators verify the private
manifest and approved downloaded-raster audit, not only a Cloud Run exit code.
Keep partial outputs; lifecycle deletion, control retention/versioning, access
logs and eventual disposal require an administrator-approved schedule. Retain
control evidence long enough for EE operation recovery and object audits.

One task does not serialize different job executions. Pilot operators serialize
launches and admins impose EE quotas/concurrency limits and retention; billing
alerts are not hard cost caps. The proposed institutional smoke budget remains unapproved.
Rollback means redeploying a previously qualified digest with its matching
policy/release record, never a mutable tag. Existing runs bind old policy/image
settings: recover them with the matching prior job/image; do not rewrite manifests
to force resume across versions. Rollback must not automatically execute a job.

Primary references: [Jobs execution](https://docs.cloud.google.com/run/docs/execute/jobs),
[job YAML](https://docs.cloud.google.com/run/docs/reference/yaml/v1),
[container contract](https://docs.cloud.google.com/run/docs/container-contract),
[job service identity](https://docs.cloud.google.com/run/docs/configuring/jobs/service-identity).
See [container qualification](qualification.md)
for the deferred Linux gates and observed offline evidence.
