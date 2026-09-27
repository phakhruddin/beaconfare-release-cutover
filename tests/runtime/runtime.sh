#!/bin/sh
# Verifier-side runtime: same images, fresh randomized configuration and a
# fresh choice of which later release carries the pricing regression. The
# verifier never reuses the agent's config, so nothing a submission recorded
# at solve time can be replayed here.
set -eu

APPLICATION_DIR="${APPLICATION_DIR:-/application}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
PRIVATE_DIR="${PRIVATE_DIR:-/private}"

rand() {
  od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

if [ $(( 0x$(rand 1) % 2 )) -eq 0 ]; then defective=3.5.0; good=3.6.0; else defective=3.6.0; good=3.5.0; fi

mkdir -p "$CONFIG_DIR" "$PRIVATE_DIR"
APPLICATION_DIR="$APPLICATION_DIR" DEFECTIVE_VERSION="$defective" /bin/sh "$APPLICATION_DIR/build.sh"

image_id() {
  docker image inspect --format '{{.Id}}' "$1"
}

resource_prefix="bf-$(rand 5)"
desired=$(( 0x$(rand 1) % 2 + 2 ))
retention_choices="1 3 5 7 14"
retention=$(echo $retention_choices | cut -d' ' -f$(( 0x$(rand 1) % 5 + 1 )))
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
  "log_retention_days": $retention
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
