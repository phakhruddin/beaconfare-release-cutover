#!/bin/sh
# Verifier-side runtime: same images, fresh randomized configuration and a
# fresh choice of which later release carries the pricing regression. The
# verifier never reuses the agent's config, so nothing a submission recorded
# at solve time can be replayed here.
set -eu

# Named volumes are mounted after the verifier image is built, so their root
# directory does not inherit the ownership established in the Dockerfile.
# The public contract permits agent diagnostics here; make that promise true
# in the verifier environment as well.
if [ -d /evidence ]; then
  chown -R 10001:10001 /evidence
  chmod 0775 /evidence
fi

APPLICATION_DIR="${APPLICATION_DIR:-/application}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
PRIVATE_DIR="${PRIVATE_DIR:-/private}"

rand() {
  od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

if [ $(( 0x$(rand 1) % 2 )) -eq 0 ]; then defective=3.5.0; good=3.6.0; else defective=3.6.0; good=3.5.0; fi

mkdir -p "$CONFIG_DIR" "$PRIVATE_DIR"
# Every release warms up for this long after start before it is ready.
# Long enough that a controller must distinguish warming from a verdict, but
# bounded so iterative deploy/test cycles remain practical for agents.
warmup=$(( 0x$(rand 1) % 16 + 20 ))
APPLICATION_DIR="$APPLICATION_DIR" DEFECTIVE_VERSION="$defective" WARMUP_SECONDS="$warmup" /bin/sh "$APPLICATION_DIR/build.sh"

# Keep every release image referenced by a running container for the life of
# this environment. An image no container uses can be removed from the shared
# daemon while the environment is still running, and a later release would
# then fail to start with "pull access denied". The containers carry this
# compose project's labels, so `compose down --remove-orphans` removes them.
project="${COMPOSE_PROJECT_NAME:-beaconfare}"
for version in 3.4.0 3.5.0 3.6.0; do
  name="${project}-release-keeper-$(echo "$version" | tr . -)"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --network none --restart unless-stopped \
    --label "com.docker.compose.project=${project}" \
    --label "com.docker.compose.service=release-keeper-$(echo "$version" | tr . -)" \
    --label "com.docker.compose.oneoff=False" \
    --entrypoint sleep "beaconfare/api:$version" infinity >/dev/null
done

image_id() {
  docker image inspect --format '{{.Id}}' "$1"
}

resource_prefix="bf-$(rand 5)"
desired=$(( 0x$(rand 1) % 2 + 2 ))
retention_choices="1 3 5 7 14"
retention=$(echo $retention_choices | cut -d' ' -f$(( 0x$(rand 1) % 5 + 1 )))
# Longer than the 720-second deploy budget, so a held lease never lapses mid-run.
lease=$(( 0x$(rand 1) % 301 + 900 ))
config_tmp="$CONFIG_DIR/config.json.tmp"

cat >"$config_tmp" <<JSON
{
  "resource_prefix": "$resource_prefix",
  "region": "us-east-1",
  "aws_endpoint_url": "http://aws:4566",
  "releases": [
    {"version": "3.4.0", "image": "beaconfare/api:3.4.0", "image_id": "$(image_id beaconfare/api:3.4.0)"},
    {"version": "3.5.0", "image": "beaconfare/api:3.5.0", "image_id": "$(image_id beaconfare/api:3.5.0)"},
    {"version": "3.6.0", "image": "beaconfare/api:3.6.0", "image_id": "$(image_id beaconfare/api:3.6.0)"}
  ],
  "initial_release": "3.4.0",
  "api_desired_count": $desired,
  "production_listener_port": 80,
  "preview_listener_port": 8081,
  "log_retention_days": $retention,
  "lock_lease_seconds": $lease
}
JSON

chmod 0444 "$config_tmp"
mv "$config_tmp" "$CONFIG_DIR/config.json"
cp "$CONFIG_DIR/config.json" /backup-config/config.json 2>/dev/null || true
chmod 0444 /backup-config/config.json 2>/dev/null || true

# Which release the verifier promotes and which it expects to be rejected.
# Only the verifier reads this; it is not part of the public contract.
printf '{"good_release": "%s", "defective_release": "%s"}\n' "$good" "$defective" > "$PRIVATE_DIR/release-truth.json"
chmod 0444 "$PRIVATE_DIR/release-truth.json"
chown -R 10001:10001 "$PRIVATE_DIR" 2>/dev/null || true

# The submission volume is shared with the unprivileged verifier user.
chown -R 10001:10001 /submission 2>/dev/null || true
chmod 0755 /submission 2>/dev/null || true
echo "BeaconFare verifier configuration is ready."
