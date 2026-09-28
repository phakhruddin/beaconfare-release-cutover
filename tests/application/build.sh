#!/bin/sh
# Builds one image per release. Invoked by runtime.sh in the agent environment
# and by the verifier runtime. DEFECTIVE_VERSION names the release whose
# pricing build carries the regression; the caller chooses it at random.
set -eu
APPLICATION_DIR="${APPLICATION_DIR:-/application}"
DEFECTIVE_VERSION="${DEFECTIVE_VERSION:?DEFECTIVE_VERSION is required}"
WARMUP_SECONDS="${WARMUP_SECONDS:?WARMUP_SECONDS is required}"
for version in 3.4.0 3.5.0 3.6.0; do
  build=standard
  [ "$version" = "$DEFECTIVE_VERSION" ] && build=legacy_floor
  docker build -q -f "$APPLICATION_DIR/Dockerfile.api" \
    --build-arg "APP_VERSION=$version" --build-arg "PRICING_BUILD=$build" --build-arg "WARMUP_SECONDS=$WARMUP_SECONDS" \
    -t "beaconfare/api:$version" "$APPLICATION_DIR" >/dev/null
done
