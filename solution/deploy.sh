#!/usr/bin/env bash
# BeaconFare reference deployment: a blue/green release pipeline.
#
#   BEACONFARE_RELEASE unset  -> keep the live release, repair anything missing
#   = live release            -> same as above
#   = warm standby release    -> rollback: swap the listeners, start nothing
#   = anything else           -> stage in the standby color, self-test through
#                                the preview listener, then promote or reject
#
# Release state lives in infra/release.auto.tfvars.json, so it survives
# between runs and a standalone `terraform plan` agrees with the last run.
set -Eeuo pipefail

CONFIG_PATH="/workspace/config/config.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/infra"
STATE_FILE="${INFRA_DIR}/release.auto.tfvars.json"
MANIFEST_PATH="${SCRIPT_DIR}/manifest.json"
WORK="$(mktemp -d)"
LOCK_HELD=false
trap 'declare -F release_lock >/dev/null && release_lock; rm -rf "$WORK"' EXIT

log() { echo "[deploy] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

cfg() { jq -r "$1" "$CONFIG_PATH"; }
RESOURCE_PREFIX=$(cfg '.resource_prefix')
REGION=$(cfg '.region')
AWS_ENDPOINT_URL=$(cfg '.aws_endpoint_url')
DESIRED=$(cfg '.api_desired_count')
PROD_PORT=$(cfg '.production_listener_port')
PREVIEW_PORT=$(cfg '.preview_listener_port')
INITIAL=$(cfg '.initial_release')
ENDPOINT_HOST=$(printf '%s' "$AWS_ENDPOINT_URL" | sed -E 's#^[a-zA-Z]+://##; s#[:/].*$##')
PROD_URL="http://${ENDPOINT_HOST}:${PROD_PORT}"
PREVIEW_URL="http://${ENDPOINT_HOST}:${PREVIEW_PORT}"

export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION" AWS_PAGER=""
AWSCLI=(aws --endpoint-url "$AWS_ENDPOINT_URL" --region "$REGION")

# ---- release lock (contracts/release-lock.md) ----------------------------------
# Taken before any change; a live lease held by anyone else means exit 75
# having changed nothing. Released on every exit through the EXIT trap.
LOCK_TABLE="${RESOURCE_PREFIX}-release-lock"
LOCK_LEASE=$(cfg '.lock_lease_seconds')
HOLDER="deploy-$(date +%s)-$$-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
LOCK_KEY='{"lock_id":{"S":"release-controller"}}'

# An unknown requested version is an input error, not a controller action.
# Validate it before taking a lock or writing a submission-owned file so a
# typo cannot disturb routing, tasks, manifests, or another holder's lease.
REQUESTED_INPUT="${BEACONFARE_RELEASE:-}"
if [ -n "$REQUESTED_INPUT" ] \
   && ! jq -e --arg v "$REQUESTED_INPUT" '[.releases[].version] | index($v) != null' "$CONFIG_PATH" >/dev/null; then
  die "requested release '$REQUESTED_INPUT' is not in config.json releases"
fi

lock_table_exists() {
  "${AWSCLI[@]}" dynamodb describe-table --table-name "$LOCK_TABLE" >/dev/null 2>&1
}

acquire_lock() {
  local now expires
  now=$(date +%s); expires=$(( now + LOCK_LEASE ))
  if "${AWSCLI[@]}" dynamodb put-item --table-name "$LOCK_TABLE" \
       --item "{\"lock_id\":{\"S\":\"release-controller\"},\"holder\":{\"S\":\"$HOLDER\"},\"lease_expires_at\":{\"N\":\"$expires\"}}" \
       --condition-expression "attribute_not_exists(lock_id) OR lease_expires_at < :now" \
       --expression-attribute-values "{\":now\":{\"N\":\"$now\"}}" 2>"$WORK/lock.err"; then
    LOCK_HELD=true
    log "release lock acquired as $HOLDER (lease until $expires)"
    return 0
  fi
  if grep -q ConditionalCheckFailed "$WORK/lock.err"; then
    local current
    current=$("${AWSCLI[@]}" dynamodb get-item --table-name "$LOCK_TABLE" --key "$LOCK_KEY" \
              --consistent-read --output json 2>/dev/null | jq -c '.Item // {}' || echo '{}')
    log "release lock is held with a live lease: $current; refusing to act"
    exit 75
  fi
  die "could not take the release lock: $(head -c 400 "$WORK/lock.err")"
}

release_lock() {
  "$LOCK_HELD" || return 0
  LOCK_HELD=false
  if "${AWSCLI[@]}" dynamodb delete-item --table-name "$LOCK_TABLE" --key "$LOCK_KEY" \
       --condition-expression "holder = :h" \
       --expression-attribute-values "{\":h\":{\"S\":\"$HOLDER\"}}" >/dev/null 2>&1; then
    log "release lock released"
  else
    log "release lock was no longer ours; left it alone"
  fi
}

if lock_table_exists; then
  acquire_lock
else
  log "no lock table yet (first deployment): it is created by the first apply"
fi

# ---- inputs -------------------------------------------------------------------
jq '{region, aws_endpoint_url, resource_prefix, api_desired_count,
     production_listener_port, preview_listener_port, log_retention_days,
     initial_release, releases: [.releases[] | {version, image, image_id}]}' \
  "$CONFIG_PATH" > "${INFRA_DIR}/config.auto.tfvars.json"

# Release state without Terraform state is a leftover from another
# environment (state files are never handed over): start from scratch.
FIRST_DEPLOY=false
if [ ! -s "${INFRA_DIR}/terraform.tfstate" ] \
   || ! jq -e '(.resources // []) | length > 0' "${INFRA_DIR}/terraform.tfstate" >/dev/null 2>&1; then
  rm -f "$STATE_FILE"
fi
if [ ! -s "$STATE_FILE" ]; then
  FIRST_DEPLOY=true
  jq -n --arg r "$INITIAL" --argjson n "$DESIRED" \
    '{live_color: "blue", color_release: {blue: $r, green: $r}, color_count: {blue: $n, green: 0}}' > "$STATE_FILE"
fi

st() { jq -r "$1" "$STATE_FILE"; }
other() { [ "$1" = "blue" ] && echo green || echo blue; }

LIVE_COLOR=$(st '.live_color')
LIVE_VERSION=$(st ".color_release.${LIVE_COLOR}")
REQUESTED="$REQUESTED_INPUT"
[ -n "$REQUESTED" ] || REQUESTED="$LIVE_VERSION"
log "prefix=$RESOURCE_PREFIX live=$LIVE_COLOR/$LIVE_VERSION requested=$REQUESTED desired=$DESIRED"

# ---- helpers ----------------------------------------------------------------------
tf_apply() {
  log "terraform apply ($(jq -c . "$STATE_FILE"))"
  terraform -chdir="$INFRA_DIR" apply -input=false -auto-approve -compact-warnings >&2
}

# Capacity. Terraform creates each service with its recorded count and then
# ignores desired_count (see infra/compute.tf); this sets the count of every
# color to what the release state records, through UpdateService.
scale_colors() {
  local c want got svc
  for c in blue green; do
    want=$(jq -r --arg c "$c" '.color_count[$c]' "$STATE_FILE")
    svc=$(jq -r --arg c "$c" '.[$c]' <<<"$SERVICE_ARNS")
    got=$("${AWSCLI[@]}" ecs describe-services --cluster "$CLUSTER" --services "$svc" \
          --query 'services[0].desiredCount' --output text 2>/dev/null || echo "?")
    if [ "$got" != "$want" ]; then
      log "scaling $c: desiredCount $got -> $want"
      "${AWSCLI[@]}" ecs update-service --cluster "$CLUSTER" --service "$svc" \
        --desired-count "$want" >/dev/null || die "could not scale $c to $want"
      got=$("${AWSCLI[@]}" ecs describe-services --cluster "$CLUSTER" --services "$svc" \
            --query 'services[0].desiredCount' --output text 2>/dev/null || echo "?")
      [ "$got" = "$want" ] || die "$c reports desiredCount=$got after scaling to $want"
    fi
  done
}

apply() {
  tf_apply
  [ -n "${SERVICE_ARNS:-}" ] || return 0
  scale_colors
}

tf_out() { terraform -chdir="$INFRA_DIR" output -raw "$1"; }
tf_json() { terraform -chdir="$INFRA_DIR" output -json "$1"; }

EDGE_HOST=""
probe() {  # probe <url> <path> -> writes headers+body, echoes http code
  curl -s -m 8 -H "Host: ${EDGE_HOST}" -H 'Connection: close' \
    -D "$WORK/h" -o "$WORK/b" -w '%{http_code}' "$1$2" 2>/dev/null || echo 000
}
header() { tr -d '\r' < "$WORK/h" | awk -F': ' -v k="$(echo "$1" | tr 'A-Z' 'a-z')" 'tolower($1)==k {print $2}' | tail -n1; }

healthy_targets() {
  local n
  n=$("${AWSCLI[@]}" elbv2 describe-target-health --target-group-arn "$1" \
      --query "length(TargetHealthDescriptions[?TargetHealth.State=='healthy'])" --output text 2>/dev/null || echo 0)
  [ "$n" = "None" ] && n=0
  echo "${n:-0}"
}

running_tasks() {
  "${AWSCLI[@]}" ecs describe-services --cluster "$CLUSTER" --services "$1" \
    --query 'services[0].runningCount' --output text 2>/dev/null || echo 0
}

# wait_serving <url> <color> <version> <seconds>: the color's target group has
# DESIRED healthy targets, its service runs DESIRED tasks, and 3*DESIRED
# consecutive /health/ready answers on <url> are 200 from <version> in
# <color>. A task still warming up answers 503, so this also waits out warm-up.
wait_serving() {
  local url="$1" color="$2" version="$3" deadline=$(( $(date +%s) + $4 )) streak=0 need=$(( DESIRED * 3 ))
  local tg; tg=$(jq -r --arg c "$color" '.[$c]' <<<"$TG_ARNS")
  local svc; svc=$(jq -r --arg c "$color" '.[$c]' <<<"$SERVICE_ARNS")
  while true; do
    local healthy running code v c
    healthy=$(healthy_targets "$tg"); running=$(running_tasks "$svc")
    if [ "$healthy" -ge "$DESIRED" ] && [ "$running" = "$DESIRED" ]; then
      code=$(probe "$url" /health/ready); v=$(header X-BeaconFare-Version); c=$(header X-BeaconFare-Color)
      if [ "$code" = "200" ] && [ "$v" = "$version" ] && [ "$c" = "$color" ]; then
        streak=$(( streak + 1 ))
        [ "$streak" -ge "$need" ] && { log "$color serves $version on $url ($healthy healthy)"; return 0; }
        continue
      fi
    fi
    streak=0
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "timeout: $color/$version on $url healthy=$healthy running=$running last=${code:-}/${v:-}/${c:-}"
      "${AWSCLI[@]}" ecs describe-services --cluster "$CLUSTER" --services "$svc" \
        --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount,taskDefinition:taskDefinition,deployments:deployments,events:events[:5]}' \
        --output json >&2 2>/dev/null || true
      "${AWSCLI[@]}" elbv2 describe-target-health --target-group-arn "$tg" --output json >&2 2>/dev/null || true
      diagnose_color "$svc"
      return 1
    fi
    sleep 3
  done
}

# Why a color is not running: stopped tasks and what their containers logged.
diagnose_color() {
  local svc="$1" name stopped td family
  name=$("${AWSCLI[@]}" ecs describe-services --cluster "$CLUSTER" --services "$svc" \
         --query 'services[0].serviceName' --output text 2>/dev/null || true)
  stopped=$("${AWSCLI[@]}" ecs list-tasks --cluster "$CLUSTER" --service-name "$name" --desired-status STOPPED \
            --query 'taskArns[:5]' --output text 2>/dev/null || true)
  if [ -n "$stopped" ] && [ "$stopped" != "None" ]; then
    # shellcheck disable=SC2086
    "${AWSCLI[@]}" ecs describe-tasks --cluster "$CLUSTER" --tasks $stopped \
      --query 'tasks[].{task:taskArn,last:lastStatus,stoppedReason:stoppedReason,containers:containers[].{exit:exitCode,reason:reason}}' \
      --output json >&2 2>/dev/null || true
  else
    log "no stopped tasks recorded for $name"
  fi
  td=$("${AWSCLI[@]}" ecs describe-services --cluster "$CLUSTER" --services "$svc" \
       --query 'services[0].taskDefinition' --output text 2>/dev/null || true)
  family=$(printf '%s' "$td" | sed -E 's#.*/##; s#:[0-9]+$##')
  log "last container output in /ecs/$family:"
  "${AWSCLI[@]}" logs filter-log-events --log-group-name "/ecs/$family" --limit 30 \
    --query 'events[].message' --output text >&2 2>/dev/null || log "(no log events)"
}

wait_idle() {  # wait_idle <color>: the color runs no tasks
  local svc deadline=$(( $(date +%s) + 180 ))
  svc=$(jq -r --arg c "$1" '.[$c]' <<<"$SERVICE_ARNS")
  until [ "$(running_tasks "$svc")" = "0" ]; do
    [ "$(date +%s)" -ge "$deadline" ] && die "$1 still runs tasks after scale-in"
    sleep 3
  done
  log "$1 is idle"
}

# selftest <color> <version>: every candidate task passes, through preview.
# 503 warming_up is not a verdict: that task is asked again later.
selftest() {
  local color="$1" version="$2" deadline=$(( $(date +%s) + 180 )) seen=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local code task v body
    code=$(probe "$PREVIEW_URL" /release/selftest); task=$(header X-BeaconFare-Task); v=$(header X-BeaconFare-Version)
    body=$(cat "$WORK/b" 2>/dev/null || true)
    if [ "$v" != "$version" ]; then sleep 1; continue; fi
    if [ "$code" = "503" ] && jq -e '.code == "warming_up"' <<<"$body" >/dev/null 2>&1; then
      sleep 2; continue
    fi
    if [ "$code" != "200" ] || ! jq -e '.passed == true' <<<"$body" >/dev/null 2>&1; then
      log "selftest FAILED on $task ($code): ${body:0:400}"
      return 1
    fi
    case " $seen " in *" $task "*) ;; *) seen="$seen $task" ;; esac
    if [ "$(wc -w <<<"$seen")" -ge "$DESIRED" ]; then
      log "selftest passed on every task:$seen"
      return 0
    fi
  done
  log "selftest could not reach $DESIRED distinct warm $version tasks (saw:$seen)"
  return 1
}

set_state() { jq "$1" "$STATE_FILE" > "$WORK/state" && mv "$WORK/state" "$STATE_FILE"; }

# ---- 1. converge on the recorded state (first deploy and repair) --------------------
terraform -chdir="$INFRA_DIR" init -input=false >&2
apply

EDGE_HOST=$(tf_out edge_dns_name)
CLUSTER=$(tf_out cluster_arn)
TG_ARNS=$(tf_json target_group_arns)
SERVICE_ARNS=$(tf_json service_arns)
"$LOCK_HELD" || acquire_lock
scale_colors

STANDBY_COLOR=$(other "$LIVE_COLOR")
STANDBY_VERSION=$(st ".color_release.${STANDBY_COLOR}")
STANDBY_COUNT=$(st ".color_count.${STANDBY_COLOR}")

wait_serving "$PROD_URL" "$LIVE_COLOR" "$LIVE_VERSION" 300 || die "live color $LIVE_COLOR is not serving $LIVE_VERSION"
if [ "$STANDBY_COUNT" -gt 0 ]; then
  wait_serving "$PREVIEW_URL" "$STANDBY_COLOR" "$STANDBY_VERSION" 180 || log "standby $STANDBY_COLOR is not serving $STANDBY_VERSION"
fi

OUTCOME=unchanged
$FIRST_DEPLOY && OUTCOME=initial

# ---- 2. act on the request --------------------------------------------------------------
if [ "$REQUESTED" != "$LIVE_VERSION" ]; then
  standby_svc=$(jq -r --arg c "$STANDBY_COLOR" '.[$c]' <<<"$SERVICE_ARNS")
  standby_tg=$(jq -r --arg c "$STANDBY_COLOR" '.[$c]' <<<"$TG_ARNS")
  if [ "$REQUESTED" = "$STANDBY_VERSION" ] && [ "$STANDBY_COUNT" = "$DESIRED" ] \
     && [ "$(running_tasks "$standby_svc")" = "$DESIRED" ] && [ "$(healthy_targets "$standby_tg")" -ge "$DESIRED" ]; then
    log "rollback: $STANDBY_COLOR already runs $REQUESTED warm; swapping listeners"
    set_state ".live_color = \"$STANDBY_COLOR\""
    apply
    OUTCOME=rolled_back
  else
    log "candidate $REQUESTED -> $STANDBY_COLOR (production stays on $LIVE_COLOR/$LIVE_VERSION)"
    set_state ".color_release.${STANDBY_COLOR} = \"$REQUESTED\" | .color_count.${STANDBY_COLOR} = $DESIRED"
    apply
    if wait_serving "$PREVIEW_URL" "$STANDBY_COLOR" "$REQUESTED" 360 && selftest "$STANDBY_COLOR" "$REQUESTED"; then
      log "promoting $REQUESTED"
      set_state ".live_color = \"$STANDBY_COLOR\""
      apply
      OUTCOME=promoted
    else
      log "rejecting $REQUESTED; scaling $STANDBY_COLOR to zero"
      set_state ".color_count.${STANDBY_COLOR} = 0"
      apply
      wait_idle "$STANDBY_COLOR"
      OUTCOME=rejected
    fi
  fi
fi

# ---- 3. readiness after the action ----------------------------------------------------
LIVE_COLOR=$(st '.live_color'); LIVE_VERSION=$(st ".color_release.${LIVE_COLOR}")
STANDBY_COLOR=$(other "$LIVE_COLOR"); STANDBY_COUNT=$(st ".color_count.${STANDBY_COLOR}")
STANDBY_VERSION=$(st ".color_release.${STANDBY_COLOR}")
wait_serving "$PROD_URL" "$LIVE_COLOR" "$LIVE_VERSION" 240 || die "production is not serving $LIVE_VERSION from $LIVE_COLOR"
if [ "$STANDBY_COUNT" -gt 0 ]; then
  wait_serving "$PREVIEW_URL" "$STANDBY_COLOR" "$STANDBY_VERSION" 240 || die "preview is not serving $STANDBY_VERSION from $STANDBY_COLOR"
  STANDBY_REPORTED="$STANDBY_VERSION"
else
  STANDBY_REPORTED=""
fi

# ---- 4. manifest ----------------------------------------------------------------------
jq -n \
  --arg deployment "$RESOURCE_PREFIX" \
  --arg vpc_id "$(tf_out vpc_id)" \
  --argjson public_subnet_ids "$(tf_json public_subnet_ids)" \
  --argjson private_subnet_ids "$(tf_json private_subnet_ids)" \
  --arg lb_arn "$(tf_out edge_arn)" \
  --arg dns_name "$EDGE_HOST" \
  --arg prod_arn "$(tf_out production_listener_arn)" \
  --arg prod_url "$PROD_URL" \
  --arg preview_arn "$(tf_out preview_listener_arn)" \
  --arg preview_url "$PREVIEW_URL" \
  --arg cluster_arn "$CLUSTER" \
  --argjson services "$SERVICE_ARNS" \
  --argjson target_groups "$TG_ARNS" \
  --arg table "$(tf_out quotes_table_name)" \
  --arg table_arn "$(tf_out quotes_table_arn)" \
  --arg lock_table "$(tf_out lock_table_name)" \
  --arg lock_table_arn "$(tf_out lock_table_arn)" \
  --arg exec_role "$(tf_out execution_role_arn)" \
  --arg task_role "$(tf_out task_role_arn)" \
  --argjson log_groups "$(tf_json log_groups)" \
  --arg live_version "$LIVE_VERSION" --arg live_color "$LIVE_COLOR" \
  --arg standby_version "$STANDBY_REPORTED" --arg standby_color "$STANDBY_COLOR" \
  --arg requested "$REQUESTED" --arg outcome "$OUTCOME" \
  '{
    deployment: $deployment,
    network: {vpc_id: $vpc_id, public_subnet_ids: $public_subnet_ids, private_subnet_ids: $private_subnet_ids},
    edge: {load_balancer_arn: $lb_arn, dns_name: $dns_name, host_header: $dns_name,
           production_listener_arn: $prod_arn, production_url: $prod_url,
           preview_listener_arn: $preview_arn, preview_url: $preview_url},
    compute: {cluster_arn: $cluster_arn, services: $services, target_groups: $target_groups},
    data: {quotes_table: {name: $table, arn: $table_arn},
           lock_table: {name: $lock_table, arn: $lock_table_arn}},
    roles: {execution_role_arn: $exec_role, task_role_arn: $task_role},
    logs: {groups: $log_groups},
    release: {live_version: $live_version, live_color: $live_color,
              standby_version: (if $standby_version == "" then null else $standby_version end),
              standby_color: $standby_color,
              last_request: {version: $requested, outcome: $outcome}}
  }' > "$MANIFEST_PATH"

log "deploy complete: outcome=$OUTCOME live=$LIVE_COLOR/$LIVE_VERSION standby=$STANDBY_COLOR/${STANDBY_REPORTED:-idle}"
