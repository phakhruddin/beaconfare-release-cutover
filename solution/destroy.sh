#!/usr/bin/env bash
# Removes only what this deployment owns: everything in Terraform state.
# Pre-existing resources that merely share the prefix are never touched,
# which is why nothing here deletes by name pattern.
set -Eeuo pipefail

CONFIG_PATH="/workspace/config/config.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/infra"

log() { echo "[destroy] $*" >&2; }

REGION=$(jq -r '.region' "$CONFIG_PATH")
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION" AWS_PAGER=""

if [ ! -f "${INFRA_DIR}/terraform.tfstate" ]; then
  log "no state present, nothing owned by this deployment"
  exit 0
fi

if [ ! -f "${INFRA_DIR}/config.auto.tfvars.json" ]; then
  jq '{region, aws_endpoint_url, resource_prefix, api_desired_count,
       production_listener_port, preview_listener_port, log_retention_days,
       initial_release, releases: [.releases[] | {version, image, image_id}]}' \
    "$CONFIG_PATH" > "${INFRA_DIR}/config.auto.tfvars.json"
fi

terraform -chdir="$INFRA_DIR" init -input=false >&2 || {
  log "terraform init failed (a provider plugin may not have started in time); retrying once"
  sleep 10
  terraform -chdir="$INFRA_DIR" init -input=false >&2
}

# The emulator reports an ECS service as deleted before its tasks have fully
# stopped.  Its log-group deletion then waits for those writers indefinitely.
# Tear down only the services and task definitions that Terraform state owns,
# wait a bounded interval for that deployment's cluster to drain, and let the
# ordinary full destroy remove every remaining managed resource.
AWS_ENDPOINT_URL=$(jq -r '.aws_endpoint_url' "$CONFIG_PATH")
AWSCLI=(aws --endpoint-url "$AWS_ENDPOINT_URL" --region "$REGION")
CLUSTER=$(terraform -chdir="$INFRA_DIR" output -raw cluster_arn)

log "stopping managed ECS services before log cleanup"
terraform -chdir="$INFRA_DIR" destroy -input=false -auto-approve \
  -target=aws_ecs_service.color >&2

for attempt in $(seq 1 60); do
  running=$("${AWSCLI[@]}" ecs list-tasks --cluster "$CLUSTER" --desired-status RUNNING \
    --query 'taskArns' --output text)
  if [ -z "$running" ] || [ "$running" = "None" ]; then
    break
  fi
  if [ "$attempt" -eq 60 ]; then
    log "managed ECS tasks did not stop within 120 seconds"
    exit 1
  fi
  sleep 2
done

log "deregistering managed ECS task definitions before log cleanup"
terraform -chdir="$INFRA_DIR" destroy -input=false -auto-approve \
  -target=aws_ecs_task_definition.api >&2

log "terraform destroy"
if ! terraform -chdir="$INFRA_DIR" destroy -input=false -auto-approve >&2; then
  log "first destroy attempt failed, retrying once"
  sleep 10
  terraform -chdir="$INFRA_DIR" destroy -input=false -auto-approve >&2
fi
rm -f "${INFRA_DIR}/release.auto.tfvars.json"
log "destroy complete"
