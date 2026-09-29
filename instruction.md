You need to build the release platform for **BeaconFare**, the freight quote
API behind a logistics marketplace. Your job is to provision, release, roll
back, repair and cleanly destroy it on ECS using Terraform or OpenTofu.

BeaconFare ships pricing releases every week. Last quarter a release with a
rounding regression went straight to customers and mispriced freight for an
afternoon. It passed every health check, because a pricing bug is not a
health problem. The team now wants a blue/green pipeline: every candidate is
deployed next to production, proven through a separate preview listener
before a single customer request reaches it, and only then switched in,
without dropping a request. The previous release stays warm so rolling back
is a listener change, not a redeploy. A candidate that fails its proof is
rejected automatically and never touches production.

The application is provided as one ready-to-run container image per release.
Each image serves the quote API and stores quotes in DynamoDB. The release
version and pricing build are fixed inside each image. I provide the exact
image references and their approved image IDs. Do not rewrite the application
or build replacement images.

## Workspace

The workspace contains Terraform, OpenTofu, the AWS provider, the AWS CLI,
Python and common diagnostic tools. It does not expose the Docker CLI or the
Docker socket. Perform every cloud operation through the provided AWS
endpoint.

The contracts are under `/workspace/contracts/`:

- `release-process.md` is the product contract. It defines the two colors,
  the production and preview listeners, how a release is requested, how a
  candidate is verified, and what promotion, rollback and rejection must do.
  **Read it first.**
- `runtime.md` defines the images, their environment variables, the
  self-test, and the endpoint behavior that affects deployment.
- `execution-guide.md` condenses the required controller phases, durable
  state, and the fastest useful diagnostics. It adds no requirements; use it
  to avoid rediscovering emulator behavior while implementing.
- `infrastructure.md` is the infrastructure index. Its linked files under
  `services/` define the network, load balancer, ECS, DynamoDB, IAM and
  logging requirements.
- `openapi.yaml` defines the HTTP API.
- `schemas/manifest.schema.json` defines the deployment manifest you must
  produce.

Other locations:

- Runtime configuration, the release list and approved image IDs:
  `/workspace/config/config.json`
- Your deliverable: `/workspace/submission/`
- While developing, you may write exploratory diagnostic output under
  `/workspace/evidence/`; it is writable in both agent and verifier
  environments. Submission scripts must not hardcode that absolute path for
  their own runtime logs. Put such logs under a directory resolved from the
  script's own location (for example, `"$SCRIPT_DIR/evidence"`) so they remain
  writable after Harbor copies the submission.

Configure the AWS provider, and every AWS CLI or SDK call you make, with
`aws_endpoint_url` and `region` from `/workspace/config/config.json`. Use the
static credentials already in the environment (`test` / `test`).

Read configuration values dynamically from `/workspace/config/config.json` at
execution time. Terraform or OpenTofu, lifecycle scripts, readiness checks and
manifest generation must not hard code values copied from the current
contents of that file. The resource prefix, task count and log retention are
generated fresh for every run, and so is the choice of which later release
carries a pricing regression.

During verification, your submission is not run where you wrote it. Harbor
hands it to the verifier read-only. The verifier copies the whole tree
somewhere writable and runs `deploy.sh` and `destroy.sh` from there,
repeatedly, in that same copy, with a different `BEACONFARE_RELEASE` each
time. Terraform state files and `config.auto.tfvars.json` are not handed over;
everything else you leave in `submission/` is, so the first verifier run
must not mistake leftovers from your own testing for a live deployment.
Resolve every submission-owned path, including state, manifests, temporary
files and persistent logs, **relative to your own script**. Hardcoded
`/workspace/submission/...` and `/workspace/evidence/...` paths break after
the copy or can target a volume your script does not own. The one path that
never moves is the runtime configuration: always read it at
`/workspace/config/config.json`.

Do not modify the contracts or the supplied images.

## What you must hand back

```text
/workspace/submission/
├── deploy.sh
├── destroy.sh
├── manifest.json     # deploy.sh writes this
└── infra/
    └── one or more *.tf files
```

- `deploy.sh` creates, releases, rolls back and repairs the deployment. It
  reads the requested release from `BEACONFARE_RELEASE` as described in
  `release-process.md`. It must work when no BeaconFare resources exist,
  repair managed resources deleted after a previous deployment, and finish
  only when the deployment is ready as defined in `services/ecs.md`. Rejecting
  a candidate that fails verification is a successful run. Each run has 720
  seconds and may produce at most 8 MiB of combined output.
- `destroy.sh` removes only the resources belonging to this deployment and
  must not modify pre-existing resources. It has 900 seconds and may produce
  at most 8 MiB of combined output.
- `manifest.json` is written by every successful `deploy.sh` run. It must
  conform to `contracts/schemas/manifest.schema.json` and contain the real
  identifiers returned by the AWS APIs and recorded in Terraform or OpenTofu
  state, plus the release outcome. Its maximum size is 1 MiB.
- `infra/` contains the Terraform or OpenTofu `.tf` files. `deploy.sh` must
  run Terraform or OpenTofu from this directory using local state. Declare
  every required cloud resource in the `.tf` files. Resources created only
  with the AWS CLI are not accepted.

Persist every dynamic value your configuration needs, including which color
is live and which release each color runs, into auto-loaded variable files in
`infra/`, written by `deploy.sh` before `init` and `apply`. Correctness is
checked by running `terraform plan -refresh=false` directly against `infra/`
without going through `deploy.sh`, after the last release.

## What "done" looks like

1. Production answers through the production listener from
   `api_desired_count` healthy tasks of one color in private subnets with no
   public IP; the preview listener forwards to the other color.
2. Requesting a correct new release deploys it into the standby color, proves
   it through the preview listener, and switches production to it without a
   single failed production request. The release that was live stays running
   as a warm standby.
3. Requesting the warm standby's release switches production back to exactly
   those standby tasks, starting nothing new, again without a failed request.
4. Requesting the release with the pricing regression ends with it rejected:
   no production request is ever answered by it, production is untouched and
   the candidate color is scaled to zero.
5. Quotes written before and during any release stay readable afterwards.
   Every release takes 20–35 seconds to warm up after it starts; a
   candidate is judged and switched in only once it is warm.
6. If listeners or task counts are changed outside `deploy.sh`, a run with
   no release requested puts them back to your recorded release state
   without failing a production request.
7. Rerunning `deploy.sh` with no release requested after managed resources
   were deleted restores them without disturbing production.
8. A standalone `terraform plan -refresh=false` against `infra/` shows
   nothing to create or delete.
9. `destroy.sh` removes everything this deployment owns and nothing else.

## Scoring

The score is weighted by category. A run passes only at 100.

| Category | Points |
|---|---:|
| Verified promotion and defective-release rejection | 32 |
| Instant rollback, drift and repair, stable state | 28 |
| Blue/green topology | 14 |
| Product traffic | 6 |
| Managed platform and isolation | 12 |
| Destruction | 8 |
| **Total** | **100** |

Letting a release that failed verification answer production traffic, or
deleting a resource this deployment does not own, caps the total regardless
of what else passes.
