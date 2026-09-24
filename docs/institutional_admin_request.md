# Institutional administrator request

Prepared for the owner to send; no message has been sent. Design date 2026-09-09.

## Copyable request

We are developing geecomposer, a Python/Earth Engine workflow that creates
Sentinel composites and normalized Rao Q rasters. Please provide an
institution-owned Google Cloud project with Earth Engine access, a dedicated
keyless runtime service account, a Cloud Run Job and Artifact Registry location,
and approved pre-existing Cloud Storage output and control/input buckets.

The workload must not depend on my personal Google credentials, personal
project, OAuth refresh token or a downloaded service-account JSON key. Please
attach the runtime service account to Cloud Run. Grant institution-managed
groups or federated identities permission to invoke the job; grant a separate
maintainer/CI identity deployment access. I need the project ID, region, runtime
service-account email, registry repository, existing bucket names and permitted
prefixes, access-group details and the approved qualification budget. Please
confirm Earth Engine registration/use classification, billing, quotas, retention,
logging and any VPC Service Controls, CMEK or location restrictions.

A separate build/deploy identity and a dedicated control bucket keep operational
permissions separate from raster access. If unattended work must run outside
Google Cloud, please provide Workload Identity Federation for an institution-owned
CI/compute identity. Please approve a small qualification run before production
use; agree resource/cost limits before any live qualification.

## Identity model

An application service account belongs to the institution's project and survives
staff changes. Cloud Run obtains short-lived credentials through its attached
identity. The Python worker verifies the attached principal through the fixed metadata
endpoint, obtains its short-lived token there and explicitly passes those
verified credentials plus the institutional project to `ee.Initialize`. It
bypasses ADC discovery and rejects personal/key credentials. The operator upload
helper is separate: its preconfigured institutional identity must be approved;
it does not log in. Current EE guidance warns against explicitly restricting
scopes in Cloud Run GCS export tasks. [EE service-account/ADC guidance](https://developers.google.com/earth-engine/guides/service_account).

The launcher is separately auditable: institutional SSO/group membership can
identify the person who requested a job, while EE and GCS operations use the
runtime account. If the owner wants no personal-account login even for launching,
use institution-managed SSO or institution-owned CI; a keyless workload still
needs an authorized identity, never an anonymous shared credential.

Do not set `GOOGLE_APPLICATION_CREDENTIALS` to a downloaded key in Cloud Run.
Attach a user-managed account and grant deployment's service-account-user
permission narrowly to it. [Job service identity](https://docs.cloud.google.com/run/docs/configuring/jobs/service-identity).
Off-cloud unattended CI should exchange its own institutional OIDC identity
through a restricted provider (repository/workflow/environment conditions), not
borrow a developer's refresh token.
[Workload Identity Federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation).

## Proposed permission matrix

Admins must validate these permissions against their policies and current APIs;
the live smoke verifies the effective principal. Do not grant project Owner or
Editor to the runtime. Do not confuse it with Google's service-agent identities.

| Principal | Scope | Proposed access |
|---|---|---|
| Runtime account | EE consumer project | `roles/serviceusage.serviceUsageConsumer`; EE computation/export and operation read/update permissions. Prefer a custom role for `earthengine.computations.create`, `earthengine.exports.create`, `earthengine.operations.get`, `earthengine.operations.list`, `earthengine.operations.update`, required source read/config permissions. `roles/earthengine.writer` is the documented broader fallback for export, if approved. |
| Runtime account | Approved output bucket | `storage.objects.create`, `storage.objects.get`, `storage.objects.list`; `storage.buckets.get` for explicit preflight. Object Creator + Object Viewer plus narrow bucket-metadata access is one option. No output delete permission. |
| Runtime account | Dedicated control bucket | Read/create and conditional replacement of run state; replacements require `storage.objects.delete`; also `storage.buckets.get` for preflight. Confine that permission to control storage. No bucket administration. |
| Runtime account | Recipe/AOI input objects | Object read only, within approved input storage; validate generation and size. |
| Institutional launch group | Specific Cloud Run Job | `roles/run.invoker` for fixed configuration; `roles/run.jobsExecutorWithOverrides` when recipe arguments are overridden. Also `run.jobs.get` for helper binding verification; add execution/log visibility as needed. |
| Result readers | Output bucket/cohort | Object read; no automatic public sharing. Bucket owner grants access independently of job invocation. |
| Deploy identity | Specific job and runtime account | Cloud Run Developer, Service Account User on runtime account; repository image read as required. Broader create permissions only for initial provisioning. |
| Build identity | Specific Artifact Registry repository | Image write; source read and approved build permissions. No EE or output-bucket write access. |
| Admins | Project/org/buckets | Enable APIs, register EE, assign IAM, billing/quota and policy configuration; not runtime permissions. |

EE Viewer includes computation/read operations but **does not include
`earthengine.exports.create`** in the current permission reference. A successful
initialization alone therefore does not prove export authorization.
[EE roles and exact permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/earthengine),
[EE project consumption access](https://developers.google.com/earth-engine/guides/access_control).

GCS Object Creator cannot replace existing objects; replacement needs delete
permission as well. Prefixes are names, not automatically access-control
boundaries. Use separate buckets for strict isolation or administrator-tested
conditional/managed-folder permissions; `objects.list` needs special attention
and cannot be assumed to reveal only a requested prefix.
[Storage roles](https://docs.cloud.google.com/storage/docs/access-control/iam-roles),
[Storage permissions](https://docs.cloud.google.com/storage/docs/access-control/iam-permissions).

Job override execution requires additional permission beyond ordinary invocation.
Limit it to trusted users of that job/cohort; separate accounts/jobs or an
authenticated broker are necessary for conflicting per-user data entitlements.
[Jobs execution](https://docs.cloud.google.com/run/docs/execute/jobs),
[Jobs executor roles](https://docs.cloud.google.com/iam/docs/roles-permissions/run).

## Provisioning sequence and acceptance

1. Choose institution-owned project, billing owner, approved region and support
   group. Confirm registered EE access and commercial/noncommercial eligibility;
   do not infer eligibility just from institutional status.
2. Enable Earth Engine, Cloud Run, Cloud Storage and Artifact Registry APIs;
   enable Cloud Build only if selected. IAM Credentials/STS APIs are needed
   when the chosen federation/impersonation arrangement requires them.
3. Create runtime and build/deploy service accounts or authorize maintainers to
   do so with narrowly scoped permissions. Keep service-account key creation
   disabled. Apply the selected matrix and test source access.
4. Supply output and control/input buckets already created. Confirm cross-project
   grants when bucket and EE project differ. Resolve CMEK, retention lock,
   requester-pays, VPC-SC and egress constraints before the pilot.
5. Select the reviewed container digest and deploy the one-task job with attached
   runtime account. Use separate development/production resources and least
   privilege; operator changes have an auditable owner.
6. Set daily EE quotas and project-wide batch limits; add Cloud billing alerts,
   Cloud Run execution limits and retention. Billing alerts do not stop spending.
   EE daily EECU limits are approximate safeguards, not precise cash caps.
   [EE quotas](https://developers.google.com/earth-engine/guides/usage).
7. Approve the bounded smoke and verify its outputs/audit principal. Test forbidden
   bucket/prefix, replay of same run, cancellation and absence of user tokens.
   Give the operations team the manifest/recovery procedure before wider access.

Noncommercial EE has tier/quota requirements that may evolve; administrators
should confirm the applicable tier and account setup. Other Google Cloud services
can still generate charges even when EE use is noncommercial.
[Current noncommercial tier guidance](https://developers.google.com/earth-engine/guides/noncommercial_tiers).

No admin answer is needed to continue offline package development. Institutional approval gates cloud deployment and live acceptance.

## Runtime operation permissions

The exact EE computation/export permissions are `earthengine.computations.create`
and `earthengine.exports.create`. Lost-acknowledgement discovery needs
`earthengine.operations.list`; status needs `earthengine.operations.get`;
asynchronous cancellation needs `earthengine.operations.update`. Google's
[audit-method table](https://developers.google.com/earth-engine/guides/audit_logging#googlelongrunningoperations)
maps those operation methods explicitly. The runner does not delete EE operations.
These are additional to source/config access and `serviceusage.services.use`,
and separate from all Storage and Cloud Run invocation permissions.

The output verification path obtains metadata through `storage.objects.list`;
`storage.objects.get` on output is useful for the approved downstream raster
audit, but is not a separate runner inventory request. Input/control reads need
`storage.objects.get`. Control replacements require create plus delete even with
generation preconditions. Output delete remains excluded. See the complete
[runner inventory](runner.md).

No institutional resources or permissions have been verified. Institutional approval remains pending. These are preparation artifacts only;
Linux/container qualification is pending, and publication/deployment/execution need exact
human authority after the institutional handoff. See
[the operator procedure](cloud_run_operator.md).
