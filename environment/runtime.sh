#!/bin/sh
# Builds the supplied release images and writes the per-run configuration.
# Every value a submission might be tempted to hard code is randomized here,
# including which later release carries the pricing regression.
set -eu

APPLICATION_DIR="${APPLICATION_DIR:-/application}"
CONFIG_DIR="${CONFIG_DIR:-/config}"

rand() {
  od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

if [ $(( 0x$(rand 1) % 2 )) -eq 0 ]; then defective=3.5.0; else defective=3.6.0; fi

mkdir -p "$CONFIG_DIR"
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
echo "BeaconFare release images and configuration are ready."
