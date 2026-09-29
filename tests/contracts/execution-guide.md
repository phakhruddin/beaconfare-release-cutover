# Execution Guide

This guide is non-normative. The linked service contracts remain the source
of truth. It collects their highest-leverage implementation facts so you can
spend time building the release controller rather than probing the emulator.

## Keep one durable release record

Persist these values in an auto-loaded `*.auto.tfvars.json` file under
`submission/infra/`:

- the live color;
- the release assigned to each color; and
- the desired task count for each color.

Treat that file as desired release state. Do not infer the live color from a
listener: listeners and service counts can drift. If Terraform state is absent
on the first verifier run, discard any copied release record and initialize a
fresh one; the verifier deliberately does not copy Terraform state from your
workspace.

## Use a small phase machine

A reliable `deploy.sh` can follow these phases on every invocation:

1. Validate the requested version before changing cloud resources.
2. Write configuration and recorded release state as auto-loaded tfvars, then
   run `terraform init` and `terraform apply`.
3. Restore both ECS desired counts with `aws ecs update-service`; the endpoint
   intentionally ignores Terraform updates to an existing service's
   `desired_count`.
4. Wait until recorded production is ready. If the recorded standby count is
   nonzero, wait for preview too. This repairs listener and capacity drift
   before interpreting the request.
5. Classify the request as unchanged, rollback, or candidate using the
   recorded state and counts in `release-process.md`.
6. For a candidate, assign its release-specific task definition to standby,
   scale it up, apply, and explicitly read the preview listener's default
   action back until it names the standby target group. Only then wait for
   preview readiness. Poll self-test through preview until every distinct task
   header has passed. Retry
   `503 warming_up`; any completed non-passing verdict rejects the candidate.
7. Promote or roll back by changing the recorded live color and applying the
   listener routing. Reject by recording standby count zero and scaling it
   down. Finally re-check readiness and write the manifest from real outputs.

Terraform must declare every managed resource, but AWS CLI reads and
`update-service --desired-count` are expected controller operations here.

## Model releases without in-place image mutation

The endpoint cannot read task-definition container JSON back faithfully, so
the contract requires a narrow `ignore_changes` for that field. Consequently,
changing an image in an existing task-definition resource will not deploy a
new release. A straightforward model is one task definition per
`color × release`, with each color service selecting the recorded release's
definition.

The local release images do not require ECR permissions. Keep the execution
role to the two CloudWatch Logs actions in `services/iam.md`; attaching AWS's
standard managed ECS execution policy adds forbidden ECR actions. Keep the
application's four table actions on the separate task role.

## Focused diagnostics

All commands use values from `/workspace/config/config.json`; do not copy its
current random values into source files.

Keep deploy-time logs inside the copied submission using a directory derived
from `BASH_SOURCE[0]`. `/workspace/evidence` is available for interactive
development evidence, but it is not a submission-owned path and must not be
hardcoded into `deploy.sh` or `destroy.sh`.

```bash
CFG=/workspace/config/config.json
ENDPOINT=$(jq -r .aws_endpoint_url "$CFG")
REGION=$(jq -r .region "$CFG")
AWS=(aws --endpoint-url "$ENDPOINT" --region "$REGION")

# Confirm Terraform configuration and local state agree after a deploy.
terraform -chdir=/workspace/submission/infra plan -refresh=false -detailed-exitcode

# Inspect service counts/task definitions without a broad resource dump.
"${AWS[@]}" ecs describe-services --cluster CLUSTER_ARN \
  --services BLUE_SERVICE_ARN GREEN_SERVICE_ARN \
  --query 'services[].{name:serviceName,desired:desiredCount,running:runningCount,taskDefinition:taskDefinition}'

# Inspect actual target health.
"${AWS[@]}" elbv2 describe-target-health --target-group-arn TARGET_GROUP_ARN \
  --query 'TargetHealthDescriptions[].TargetHealth.State'
```

During HTTP probes, send the manifest's `edge.host_header`, retain response
headers, and use `Connection: close`. The version, color, and task headers are
the quickest way to distinguish routing errors from readiness errors.
If candidate tasks are running and healthy but preview polling still times
out, inspect `describe-listeners` first: that combination normally means the
preview default action was never converged onto the standby target group.

## Budget your test loop

Each new task needs 20–35 seconds to warm. Prefer one end-to-end test after
syntax and `terraform validate` checks instead of repeatedly destroying and
recreating the stack. Keep polling output concise: combined output from one
script invocation is capped at 8 MiB.
