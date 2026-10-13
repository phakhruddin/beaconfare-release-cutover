#!/usr/bin/env bash
# BeaconFare reference deployment: a blue/green release pipeline.
#
#   BEACONFARE_RELEASE unset  -> keep the live release, repair anything missing
#   = live release            -> same as above
#   = warm standby release    -> rollback: swap the listeners, start nothing
#   = anything else           -> stage in the standby color, self-test through
#                                the preview listener, then promote or reject
#
# Release state and the current-task verification evidence live in the cloud
# release record.  infra/release.auto.tfvars.json is only the local cache
# Terraform auto-loads so a standalone `terraform plan` agrees with the
# last run.
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

# Named crash points (contracts/crash-recovery.md): stop like a killed
# process. The lock stays held, the record and manifest are not written and
# nothing is undone; only our private temp directory is removed.
FAULT_POINT="${BEACONFARE_FAULT_POINT:-}"
crash_at() {
  [ "$FAULT_POINT" = "$1" ] || return 0
  log "crash point '$1' reached: exiting 137 without cleanup"
  trap 'rm -rf "$WORK"' EXIT
  exit 137
}

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
  log "refused: requested release '$REQUESTED_INPUT' is not in config.json releases"
  exit 64
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

# Once Terraform state exists, the lock table is a managed resource and the
# controller already knows it must exist.  Avoid a separate DescribeTable
# round-trip before the conditional lock write: refusal of an operator-edited
# record has a strict 30-second, no-change deadline.
if [ -s "${INFRA_DIR}/terraform.tfstate" ] \
   && jq -e '(.resources // []) | length > 0' "${INFRA_DIR}/terraform.tfstate" >/dev/null 2>&1; then
  acquire_lock
elif lock_table_exists; then
  acquire_lock
else
  log "no lock table yet (first deployment): it is created by the first apply"
fi

# ---- the release record (contracts/release-record.md) -----------------------------
# Read under the lock and validated before anything is written: a record an
# operator edited into an impossible state is refused with 65, changing nothing.
RECORD_KEY='{"lock_id":{"S":"release-record"}}'
RELEASES_JSON=$(jq -c '[.releases[].version]' "$CONFIG_PATH")
RECORD=""
if "$LOCK_HELD" && [ -s "${INFRA_DIR}/terraform.tfstate" ]; then
  RECORD=$("${AWSCLI[@]}" dynamodb get-item --table-name "$LOCK_TABLE" --key "$RECORD_KEY" \
             --consistent-read --output json 2>/dev/null | jq -c '.Item // empty' || true)
fi

record_valid() {  # stdin: the record item in DynamoDB JSON
  jq -e --argjson n "$DESIRED" --argjson rel "$RELEASES_JSON" '
    def num: if type == "object" and has("N") then (.N | tonumber? // null) else null end;
    def str: if type == "object" and has("S") and (.S | type) == "string" then .S else null end;
    . as $rec
    | ($rec.live_color | str) as $live
    | ($rec.generation | num) as $g
    | (($rec.verified.L // null) | if type == "array" then map(str) else null end) as $ver
    | ($live == "blue" or $live == "green")
      and $g != null and $g >= 1 and ($g | floor) == $g
      and $ver != null and ($ver | length) > 0 and all($ver[]; . != null)
      and all(("blue", "green");
            . as $c | ($rec[$c + "_count"] | num) as $k | ($rec[$c + "_release"] | str) as $r
            | ($k == 0 or $k == $n) and ($k == 0 or ($r != null and any($rel[]; . == $r))))
      and (($rec[$live + "_count"] | num) == $n)
      and (($rec[$live + "_release"] | str) as $lr | any($ver[]; . == $lr))
  ' >/dev/null 2>&1
}

if [ -n "$RECORD" ] && ! record_valid <<<"$RECORD"; then
  log "refused: the release record is not a state this controller can deploy: $RECORD"
  exit 65
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

# The cloud release record (contracts/release-record.md) is the source of
# truth; the local file is only a cache that may be missing or stale (the
# verifier can hand us a fresh copy with only Terraform state kept).
PREV_GEN=0
VERIFIED=$(jq -cn --arg r "$INITIAL" '[$r]')
# Keep this before restoring the record cache. The marker is written only by a
# successful controller run in this working copy. The verifier's fresh-worker
# handoff preserves Terraform state but not this private marker, even when the
# submitted source contains a stale release cache. Such a worker must rebuild
# enough current-task evidence for its intact warm standby to remain an
# immediate rollback target.
CACHE_MARKER="${INFRA_DIR}/.beaconfare-record-cache"
FRESH_RECORD_WORKER=false
if [ -n "$RECORD" ] && [ -s "${INFRA_DIR}/terraform.tfstate" ] && [ ! -f "$CACHE_MARKER" ]; then
  FRESH_RECORD_WORKER=true
fi
# A proof is deliberately attached to a *color's current tasks*, rather than
# merely to a release name.  It lets a later controller distinguish an intact
# warm standby from one an operator restarted or re-created.  An empty proof
# is safe: it means the release may run behind preview but must be proven
# again before it can be promoted.
PROOFS='{"blue":{"release":"","tasks":[]},"green":{"release":"","tasks":[]}}'
if [ -n "$RECORD" ] && [ -s "${INFRA_DIR}/terraform.tfstate" ]; then
  # An idle color's release is not checked (it may name anything); any known
  # release will do for its task definition.
  jq --argjson rel "$RELEASES_JSON" --arg init "$INITIAL" '
      def pick($r): if any($rel[]; . == $r) then $r else $init end;
      {live_color: .live_color.S,
       color_release: {blue: pick(.blue_release.S // ""), green: pick(.green_release.S // "")},
       color_count: {blue: (.blue_count.N | tonumber), green: (.green_count.N | tonumber)}}' \
    <<<"$RECORD" > "$STATE_FILE"
  PREV_GEN=$(jq -r '.generation.N' <<<"$RECORD")
  VERIFIED=$(jq -c '[.verified.L[].S] | unique' <<<"$RECORD")
  PROOFS=$(jq -c '
      def proof($c): {
        release: (.[$c + "_proof_release"].S // ""),
        tasks: [(.[$c + "_proof_tasks"].L // [])[] | select(type == "object" and (.S | type == "string")) | .S]
      };
      {blue: proof("blue"), green: proof("green")}' <<<"$RECORD")
  log "release state restored from the release record (generation $PREV_GEN, verified $VERIFIED)"
fi
is_verified() { jq -e --arg r "$1" 'any(.[]; . == $r)' <<<"$VERIFIED" >/dev/null; }
set_proof() {  # set_proof <color> <release> <JSON array of task identities>
  PROOFS=$(jq -c --arg c "$1" --arg r "$2" --argjson tasks "$3" \
    '.[$c] = {release: $r, tasks: ($tasks | unique | sort)}' <<<"$PROOFS")
}
clear_proof() {  # clear_proof <color>
  PROOFS=$(jq -c --arg c "$1" '.[$c] = {release: "", tasks: []}' <<<"$PROOFS")
}

if [ ! -s "$STATE_FILE" ]; then
  FIRST_DEPLOY=true
  jq -n --arg r "$INITIAL" --argjson n "$DESIRED" \
    '{live_color: "blue", color_release: {blue: $r, green: $r}, color_count: {blue: $n, green: 0}}' > "$STATE_FILE"
fi

START_STATE=$(jq -cS . "$STATE_FILE")

st() { jq -r "$1" "$STATE_FILE"; }
other() { [ "$1" = "blue" ] && echo green || echo blue; }
set_state() { jq "$1" "$STATE_FILE" > "$WORK/state" && mv "$WORK/state" "$STATE_FILE"; }

LIVE_COLOR=$(st '.live_color')
LIVE_VERSION=$(st ".color_release.${LIVE_COLOR}")
REQUESTED="$REQUESTED_INPUT"
[ -n "$REQUESTED" ] || REQUESTED="$LIVE_VERSION"
STANDBY_COLOR=$(other "$LIVE_COLOR")
STANDBY_VERSION=$(st ".color_release.${STANDBY_COLOR}")
STANDBY_COUNT=$(st ".color_count.${STANDBY_COLOR}")

# A release that is not the already-warm standby cannot take the rollback
# path. Put its candidate state into the first convergence so we do not first
# apply an identical no-op state and then apply candidate state again. The
# first apply still restores the recorded live side before verification.
CANDIDATE_PRECONVERGED=false
if ! "$FIRST_DEPLOY" && [ "$REQUESTED" != "$LIVE_VERSION" ] \
   && { [ "$REQUESTED" != "$STANDBY_VERSION" ] || [ "$STANDBY_COUNT" != "$DESIRED" ]; }; then
  set_state ".color_release.${STANDBY_COLOR} = \"$REQUESTED\" | .color_count.${STANDBY_COLOR} = $DESIRED"
  CANDIDATE_PRECONVERGED=true
  log "candidate $REQUESTED is included in the first recorded-state convergence"
fi
log "prefix=$RESOURCE_PREFIX live=$LIVE_COLOR/$LIVE_VERSION requested=$REQUESTED desired=$DESIRED"

# ---- helpers ----------------------------------------------------------------------
# tf <args>: run Terraform in infra/, retrying only documented provider
# start-up failures on a loaded host (runtime.md). They occur before Terraform
# can plan or change resources, so the retry starts from the same state.
retryable_provider_startup_failure() {
  # Terraform can wrap the timeout across colored diagnostic lines. Require
  # each exact fragment and the surrounding schema/instantiate-provider form,
  # so an unrelated timeout is not retried as a provider start-up failure.
  { grep -Fq 'Failed to load plugin schemas' "$WORK/tf.out" \
      && grep -Fq 'failed to instantiate provider' "$WORK/tf.out" \
      && grep -Fq 'timeout while' "$WORK/tf.out" \
      && grep -Fq 'waiting for plugin to start' "$WORK/tf.out"; } \
    || { grep -Fq 'Failed to load plugin schemas' "$WORK/tf.out" \
         && grep -Fq 'GetProviderSchema request was cancelled' "$WORK/tf.out"; }
}

tf() {
  local attempt rc
  for attempt in 1 2 3; do
    set +e
    terraform -chdir="$INFRA_DIR" "$@" 2>&1 | tee "$WORK/tf.out" >&2
    rc=${PIPESTATUS[0]}
    set -e
    [ "$rc" = 0 ] && return 0
    retryable_provider_startup_failure || return "$rc"
    [ "$attempt" -lt 3 ] || break
    log "terraform $1: provider start-up was cancelled (attempt $attempt); retrying"
    sleep $(( attempt * 10 ))
  done
  return "$rc"
}

tf_apply() {
  log "terraform apply ($(jq -c . "$STATE_FILE"))"
  tf apply -input=false -auto-approve -compact-warnings || die "terraform apply failed"
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

# The endpoint records a running service's new task definition but keeps
# its running tasks on the old one (runtime.md). A color that ran tasks and
# moved to another task definition is cycled through zero, so the tasks it
# runs afterwards are the release the state assigns it. The live color is
# never cycled.
color_snapshot() {  # color_snapshot <service arns json> -> {color: {td, running, status}}
  local arns="$1" c svc row out='{}'
  for c in blue green; do
    svc=$(jq -r --arg c "$c" '.[$c] // empty' <<<"$arns" 2>/dev/null || true)
    [ -n "$svc" ] || continue
    row=$("${AWSCLI[@]}" ecs describe-services --cluster "$CLUSTER" --services "$svc" \
          --query 'services[0].{td:taskDefinition,running:runningCount,status:status}' --output json 2>/dev/null || true)
    jq -e 'type == "object"' >/dev/null 2>&1 <<<"$row" || row=null
    out=$(jq -c --arg c "$c" --argjson r "$row" '. + {($c): $r}' <<<"$out")
  done
  echo "$out"
}

refresh_stale() {  # refresh_stale <snapshot taken before the apply>
  local before="$1" live c svc want was_td was_run td
  live=$(st '.live_color')
  for c in blue green; do
    want=$(st ".color_count.${c}")
    [ "$want" -gt 0 ] || continue
    was_td=$(jq -r --arg c "$c" '.[$c] | select(. != null and .status != "INACTIVE") | .td // empty' <<<"$before")
    was_run=$(jq -r --arg c "$c" '.[$c].running // 0' <<<"$before")
    [ -n "$was_td" ] && [ "$was_run" -gt 0 ] || continue
    svc=$(jq -r --arg c "$c" '.[$c]' <<<"$SERVICE_ARNS")
    td=$("${AWSCLI[@]}" ecs describe-services --cluster "$CLUSTER" --services "$svc" \
         --query 'services[0].taskDefinition' --output text 2>/dev/null || true)
    [ -n "$td" ] && [ "$td" != "$was_td" ] || continue
    if [ "$c" = "$live" ]; then
      log "WARNING: live $c moved to $td with tasks running; live tasks are never cycled"
      continue
    fi
    log "$c still runs tasks of ${was_td##*/}; cycling it through zero onto ${td##*/}"
    "${AWSCLI[@]}" ecs update-service --cluster "$CLUSTER" --service "$svc" --desired-count 0 >/dev/null \
      || die "could not scale $c to 0"
    wait_idle "$c"
    "${AWSCLI[@]}" ecs update-service --cluster "$CLUSTER" --service "$svc" --desired-count "$want" >/dev/null \
      || die "could not scale $c to $want"
  done
}

apply() {
  local before='{}'
  [ -n "${SERVICE_ARNS:-}" ] && before=$(color_snapshot "$SERVICE_ARNS")
  tf_apply
  [ -n "${SERVICE_ARNS:-}" ] || return 0
  scale_colors
  refresh_stale "$before"
}

# Terraform has already committed outputs to its local state after a successful
# apply. Reading that one file avoids launching a provider process for every
# manifest field, which can otherwise stall after an otherwise-complete deploy
# on a loaded host. The verifier's fresh-worker contract deliberately keeps
# this state file, so it is the same source `terraform output` would read.
TF_OUTPUTS='{}'
load_tf_outputs() {
  if [ -s "${INFRA_DIR}/terraform.tfstate" ]; then
    TF_OUTPUTS=$(jq -ce '.outputs // {}' "${INFRA_DIR}/terraform.tfstate") \
      || die "could not read Terraform outputs from local state"
  else
    TF_OUTPUTS='{}'
  fi
}
tf_out() { jq -er --arg name "$1" '.[$name].value' <<<"$TF_OUTPUTS"; }
tf_json() { jq -ce --arg name "$1" '.[$name].value' <<<"$TF_OUTPUTS"; }

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

# observe_tasks <url> <color> <version>:
# collects one bounded fresh-connection witness from a ready endpoint. Up to
# six samples per expected task gives the listener ample opportunity to expose
# every task without turning proof reconciliation into a poll/sleep loop. A
# shortfall is safe: it makes the proof ineligible for rollback.
observe_tasks() {
  local url="$1" color="$2" version="$3" attempts=$(( DESIRED * 6 )) attempt
  local -a seen=()
  for (( attempt = 0; attempt < attempts; attempt++ )); do
    local code task v c body present=false
    code=$(probe "$url" /release); task=$(header X-BeaconFare-Task)
    v=$(header X-BeaconFare-Version); c=$(header X-BeaconFare-Color)
    body=$(cat "$WORK/b" 2>/dev/null || true)
    if [ "$code" = "200" ] && [ -n "$task" ] && [ "$v" = "$version" ] && [ "$c" = "$color" ] \
       && jq -e --arg t "$task" --arg v "$version" --arg c "$color" \
            '.task == $t and .version == $v and .color == $c' <<<"$body" >/dev/null 2>&1; then
      for present in "${seen[@]:-}"; do [ "$present" = "$task" ] && break; done
      [ "$present" = "$task" ] || seen+=("$task")
      if [ "${#seen[@]}" -eq "$DESIRED" ]; then
        printf '%s\n' "${seen[@]}" | jq -R . | jq -cs 'unique | sort'
        return 0
      fi
    fi
  done
  return 1
}

# proof_matches <url> <color> <version>: a proof is current only when its
# release and every recorded task identity equal the independently observed
# warm task set.  A stale proof is never a rollback authorization.
proof_matches() {
  local url="$1" color="$2" version="$3" actual expected
  expected=$(jq -c --arg c "$color" '.[$c]' <<<"$PROOFS")
  jq -e --arg v "$version" --argjson n "$DESIRED" \
    '.release == $v and (.tasks | type == "array" and length == $n and unique == .)' \
    >/dev/null 2>&1 <<<"$expected" || return 1
  actual=$(observe_tasks "$url" "$color" "$version") || return 1
  [ "$actual" = "$(jq -c '.tasks | unique | sort' <<<"$expected")" ]
}

# selftest <url> <color> <version>: every candidate task passes through the
# supplied listener.  503 warming_up is not a verdict.  On success it leaves
# SELFTEST_TASKS as the exact set that passed, suitable for durable evidence.
SELFTEST_TASKS='[]'
selftest() {
  local url="$1" color="$2" version="$3" deadline=$(( $(date +%s) + 180 ))
  local -a seen=()
  SELFTEST_TASKS='[]'
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local code task v c body present=false
    code=$(probe "$url" /release/selftest); task=$(header X-BeaconFare-Task)
    v=$(header X-BeaconFare-Version); c=$(header X-BeaconFare-Color)
    body=$(cat "$WORK/b" 2>/dev/null || true)
    if [ "$v" != "$version" ] || [ "$c" != "$color" ] || [ -z "$task" ]; then sleep 1; continue; fi
    if [ "$code" = "503" ] && jq -e '.code == "warming_up"' <<<"$body" >/dev/null 2>&1; then
      sleep 2; continue
    fi
    if [ "$code" != "200" ] || ! jq -e --arg t "$task" --arg v "$version" --arg c "$color" \
         '.passed == true and .task == $t and .version == $v and .color == $c' <<<"$body" >/dev/null 2>&1; then
      log "selftest FAILED on $task ($code): ${body:0:400}"
      return 1
    fi
    for present in "${seen[@]:-}"; do [ "$present" = "$task" ] && break; done
    [ "$present" = "$task" ] || seen+=("$task")
    if [ "${#seen[@]}" -eq "$DESIRED" ]; then
      SELFTEST_TASKS=$(printf '%s\n' "${seen[@]}" | jq -R . | jq -cs 'unique | sort')
      log "selftest passed on every task: $(jq -r '.[]' <<<"$SELFTEST_TASKS" | tr '\n' ' ')"
      return 0
    fi
  done
  log "selftest could not reach $DESIRED distinct warm $version tasks (saw:${seen[*]:-})"
  return 1
}

# ---- 1. converge on the recorded state (first deploy and repair) --------------------
tf init -input=false || die "terraform init failed"
load_tf_outputs
# What each color ran before this run's first apply (empty on a first deploy).
PRE_SNAPSHOT='{}'
PRE_ARNS=$(tf_json service_arns 2>/dev/null || true)
if jq -e 'type == "object"' >/dev/null 2>&1 <<<"$PRE_ARNS"; then
  CLUSTER=$(tf_out cluster_arn 2>/dev/null || true)
  [ -n "$CLUSTER" ] && PRE_SNAPSHOT=$(color_snapshot "$PRE_ARNS")
fi
apply
load_tf_outputs

EDGE_HOST=$(tf_out edge_dns_name)
CLUSTER=$(tf_out cluster_arn)
TG_ARNS=$(tf_json target_group_arns)
SERVICE_ARNS=$(tf_json service_arns)
"$LOCK_HELD" || acquire_lock
scale_colors
refresh_stale "$PRE_SNAPSHOT"

STANDBY_COLOR=$(other "$LIVE_COLOR")
STANDBY_VERSION=$(st ".color_release.${STANDBY_COLOR}")
STANDBY_COUNT=$(st ".color_count.${STANDBY_COLOR}")

wait_serving "$PROD_URL" "$LIVE_COLOR" "$LIVE_VERSION" 300 || die "live color $LIVE_COLOR is not serving $LIVE_VERSION"
if [ "$STANDBY_COUNT" -gt 0 ]; then
  wait_serving "$PREVIEW_URL" "$STANDBY_COLOR" "$STANDBY_VERSION" 180 || log "standby $STANDBY_COLOR is not serving $STANDBY_VERSION"
fi

# Reconcile the live proof before making a release decision. On first
# deployment or after an external live-task restart it is safe to rebuild it
# through production because that release is already the recorded, verified
# live release. A standby proof is observed once: on a no-release run to clear
# stale evidence, or immediately before its rollback decision. This avoids a
# duplicate listener pass that could make proof reconciliation consume the
# release budget without changing any decision.
if ! proof_matches "$PROD_URL" "$LIVE_COLOR" "$LIVE_VERSION"; then
  log "live $LIVE_COLOR has no current-task proof; rebuilding it through production"
  selftest "$PROD_URL" "$LIVE_COLOR" "$LIVE_VERSION" || die "recorded live $LIVE_COLOR/$LIVE_VERSION did not self-test"
  set_proof "$LIVE_COLOR" "$LIVE_VERSION" "$SELFTEST_TASKS"
fi
if [ "$REQUESTED" = "$LIVE_VERSION" ] && [ "$STANDBY_COUNT" -gt 0 ]; then
  if ! proof_matches "$PREVIEW_URL" "$STANDBY_COLOR" "$STANDBY_VERSION"; then
    if "$FRESH_RECORD_WORKER"; then
      # No local cache survived, but the cloud record says this is an intact
      # warm standby. Prove these existing preview tasks independently rather
      # than treating cache loss as a task restart. Ordinary reconciliation
      # below still clears evidence after an operator restart.
      log "fresh worker is rebuilding proof for warm $STANDBY_COLOR/$STANDBY_VERSION"
      if selftest "$PREVIEW_URL" "$STANDBY_COLOR" "$STANDBY_VERSION"; then
        set_proof "$STANDBY_COLOR" "$STANDBY_VERSION" "$SELFTEST_TASKS"
      else
        log "fresh worker could not prove warm $STANDBY_COLOR; it is not a rollback target"
        clear_proof "$STANDBY_COLOR"
      fi
    else
      log "standby $STANDBY_COLOR proof is absent or stale; it is not a rollback target"
      clear_proof "$STANDBY_COLOR"
    fi
  fi
elif [ "$STANDBY_COUNT" -eq 0 ]; then
  clear_proof "$STANDBY_COLOR"
fi

OUTCOME=unchanged
$FIRST_DEPLOY && OUTCOME=initial

# ---- 2. act on the request --------------------------------------------------------------
if [ "$REQUESTED" != "$LIVE_VERSION" ]; then
  standby_svc=$(jq -r --arg c "$STANDBY_COLOR" '.[$c]' <<<"$SERVICE_ARNS")
  standby_tg=$(jq -r --arg c "$STANDBY_COLOR" '.[$c]' <<<"$TG_ARNS")
  if [ "$REQUESTED" = "$STANDBY_VERSION" ] && [ "$STANDBY_COUNT" = "$DESIRED" ] && is_verified "$REQUESTED" \
     && [ "$(running_tasks "$standby_svc")" = "$DESIRED" ] && [ "$(healthy_targets "$standby_tg")" -ge "$DESIRED" ]; then
    if proof_matches "$PREVIEW_URL" "$STANDBY_COLOR" "$REQUESTED"; then
      log "rollback: $STANDBY_COLOR has current evidence for $REQUESTED; swapping listeners"
      set_state ".live_color = \"$STANDBY_COLOR\""
      apply
      crash_at switched
      OUTCOME=rolled_back
    else
      log "warm standby $STANDBY_COLOR/$REQUESTED has no current evidence; proving it as a candidate"
      clear_proof "$STANDBY_COLOR"
      set_state ".color_release.${STANDBY_COLOR} = \"$REQUESTED\" | .color_count.${STANDBY_COLOR} = $DESIRED"
      apply
      PASSED=false
      if wait_serving "$PREVIEW_URL" "$STANDBY_COLOR" "$REQUESTED" 360; then
        crash_at staged
        selftest "$PREVIEW_URL" "$STANDBY_COLOR" "$REQUESTED" && PASSED=true
      fi
      if "$PASSED"; then
        log "promoting freshly proven warm standby $REQUESTED"
        VERIFIED=$(jq -c --arg r "$REQUESTED" '. + [$r] | unique' <<<"$VERIFIED")
        set_proof "$STANDBY_COLOR" "$REQUESTED" "$SELFTEST_TASKS"
        set_state ".live_color = \"$STANDBY_COLOR\""
        apply
        crash_at switched
        OUTCOME=promoted
      else
        log "rejecting $REQUESTED; scaling $STANDBY_COLOR to zero"
        VERIFIED=$(jq -c --arg r "$REQUESTED" 'map(select(. != $r))' <<<"$VERIFIED")
        clear_proof "$STANDBY_COLOR"
        set_state ".color_count.${STANDBY_COLOR} = 0"
        apply
        wait_idle "$STANDBY_COLOR"
        OUTCOME=rejected
      fi
    fi
  else
    log "candidate $REQUESTED -> $STANDBY_COLOR (production stays on $LIVE_COLOR/$LIVE_VERSION)"
    set_state ".color_release.${STANDBY_COLOR} = \"$REQUESTED\" | .color_count.${STANDBY_COLOR} = $DESIRED"
    if ! "$CANDIDATE_PRECONVERGED"; then
      apply
    fi
    PASSED=false
    if wait_serving "$PREVIEW_URL" "$STANDBY_COLOR" "$REQUESTED" 360; then
      crash_at staged
      selftest "$PREVIEW_URL" "$STANDBY_COLOR" "$REQUESTED" && PASSED=true
    fi
    if "$PASSED"; then
      log "promoting $REQUESTED"
      VERIFIED=$(jq -c --arg r "$REQUESTED" '. + [$r] | unique' <<<"$VERIFIED")
      set_proof "$STANDBY_COLOR" "$REQUESTED" "$SELFTEST_TASKS"
      set_state ".live_color = \"$STANDBY_COLOR\""
      apply
      crash_at switched
      OUTCOME=promoted
    else
      log "rejecting $REQUESTED; scaling $STANDBY_COLOR to zero"
      VERIFIED=$(jq -c --arg r "$REQUESTED" 'map(select(. != $r))' <<<"$VERIFIED")
      clear_proof "$STANDBY_COLOR"
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
  clear_proof "$STANDBY_COLOR"
fi

# ---- 4. release record ------------------------------------------------------------------
# Written once per acting run, while the lock is held. The generation moves
# by exactly one when the recorded state changed during this run.
if [ "$PREV_GEN" = "0" ]; then
  GENERATION=1
elif [ "$(jq -cS . "$STATE_FILE")" != "$START_STATE" ]; then
  GENERATION=$(( PREV_GEN + 1 ))
else
  GENERATION=$PREV_GEN
fi
"$LOCK_HELD" || die "the release lock is not held; refusing to write the release record"
"${AWSCLI[@]}" dynamodb put-item --table-name "$LOCK_TABLE" --item "$(jq -c \
    --argjson g "$GENERATION" --arg v "$REQUESTED" --arg o "$OUTCOME" --argjson ver "$VERIFIED" --argjson proofs "$PROOFS" '{
      lock_id: {S: "release-record"}, generation: {N: ($g | tostring)},
      live_color: {S: .live_color},
      blue_release: {S: .color_release.blue}, green_release: {S: .color_release.green},
      blue_count: {N: (.color_count.blue | tostring)}, green_count: {N: (.color_count.green | tostring)},
      last_request_version: {S: $v}, last_request_outcome: {S: $o},
      verified: {L: [$ver[] | {S: .}]},
      blue_proof_release: {S: $proofs.blue.release},
      blue_proof_tasks: {L: [$proofs.blue.tasks[] | {S: .}]},
      green_proof_release: {S: $proofs.green.release},
      green_proof_tasks: {L: [$proofs.green.tasks[] | {S: .}]}}' "$STATE_FILE")" >/dev/null \
  || die "could not write the release record"
log "release record generation $GENERATION written"

# ---- 5. manifest ----------------------------------------------------------------------
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
touch "$CACHE_MARKER"
