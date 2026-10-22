#!/usr/bin/env bash
set -Eeuo pipefail

mkdir -p /logs/verifier

has_valid_reward() {
  python -c 'import json,sys; x=json.load(open(sys.argv[1])); assert isinstance(x.get("reward"), (int,float)) and isinstance(x.get("score"), (int,float)) and 0 <= x["reward"] <= 1' /logs/verifier/reward.json >/dev/null 2>&1
}

# Harbor parses every top-level value in reward.json as a numeric reward
# component, so this file stays a flat {reward, score} object. Diagnostics
# belong in report.json and summary.json.
ensure_result() {
  local rc="$1" line="$2"
  if ! has_valid_reward; then
    printf '{"trial_valid":false,"error":"verifier exited before producing a result","exit_code":%s,"line":%s}\n' \
      "$rc" "$line" >/logs/verifier/report.json
    printf '{"reward":0,"score":0}\n' >/logs/verifier/reward.json
    printf '0\n' >/logs/verifier/reward.txt
  fi
}
trap 'rc=$?; ensure_result "$rc" "$LINENO"' EXIT

rm -f /logs/verifier/reward.txt /logs/verifier/reward.json \
      /logs/verifier/report.json /logs/verifier/summary.json

export PYTHONPATH="/tests${PYTHONPATH:+:$PYTHONPATH}"
: "${BEACONFARE_SUBMISSION_DIR:?BEACONFARE_SUBMISSION_DIR is required}"

printf 'BeaconFare verifier: starting\n'

set +e
python -m pytest \
  -p no:cacheprovider \
  --tb=short \
  -rA \
  /tests/suite/test_declared.py \
  /tests/suite/test_live.py \
  /tests/suite/test_behavior.py \
  /tests/suite/test_lifecycle.py
pytest_rc=$?
set -e

if has_valid_reward; then
  python -c 'import json,sys; print(json.load(open(sys.argv[1]))["reward"])' /logs/verifier/reward.json >/logs/verifier/reward.txt
  exit 0
fi

# SIGKILL can terminate pytest before its session fixture reaches the scored
# destroy obligation. Preserve the invalid result first, then make one bounded
# best-effort attempt to stop this trial's workload so it cannot contaminate a
# retry or a neighboring panel run. A broken candidate cleanup script cannot
# turn an invalid trial into a score or keep this wrapper alive indefinitely.
if [[ "${pytest_rc:-3}" -eq 137 ]]; then
  ensure_result "$pytest_rc" "$LINENO"
  timeout 180 /workspace/submission/destroy.sh \
    >>/logs/verifier/emergency-cleanup.txt 2>&1 || true
fi
exit "${pytest_rc:-3}"
