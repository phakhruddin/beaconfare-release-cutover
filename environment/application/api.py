"""BeaconFare quote API.

Prices freight lanes and stores every issued quote in DynamoDB. One image is
built per release. The release version and its pricing build are baked into
the image at build time; nothing in the task definition can change them.

Every response carries the release version, the deployment color the task
was started in, and the task identity, so a caller can always tell exactly
which release answered.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from awslite import AwsError, DynamoDB, from_item, to_item


def log(event: str, **fields: Any) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    record.update(fields)
    print(json.dumps(record, default=str), flush=True)


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        log("config_missing", variable=name)
        sys.exit(f"environment variable {name} is required")
    return value


APP_VERSION = os.environ.get("APP_VERSION", "0.0.0")        # baked into the image
PRICING_BUILD = os.environ.get("PRICING_BUILD", "standard")  # baked into the image
WARMUP_SECONDS = float(os.environ.get("WARMUP_SECONDS", "0"))  # baked into the image
STARTED = time.monotonic()
ENDPOINT = required("AWS_ENDPOINT_URL")
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
QUOTES_TABLE = required("QUOTES_TABLE")
COLOR = required("DEPLOYMENT_COLOR")
PORT = int(os.environ.get("PORT", "8080"))
TASK = socket.gethostname()

if COLOR not in ("blue", "green"):
    log("config_invalid", variable="DEPLOYMENT_COLOR", value=COLOR)
    sys.exit("DEPLOYMENT_COLOR must be blue or green")

DDB = DynamoDB(ENDPOINT, REGION)
CODE = re.compile(r"^[A-Z]{3}$")
ID = re.compile(r"^[0-9a-f-]{36}$")

# Reference cases every correct pricing build reproduces exactly.
GOLDEN = [
    ("SEA", "PDX", 12.34, 8640),
    ("LAX", "JFK", 5.0, 6050),
    ("ORD", "ATL", 0.05, 5035),
    ("MIA", "DEN", 480.71, 170480),
]


def warming_remaining() -> float:
    """Seconds of warm-up left: the pricing cache loads after start."""
    return max(0.0, WARMUP_SECONDS - (time.monotonic() - STARTED))


def require_warm() -> None:
    left = warming_remaining()
    if left > 0:
        raise ApiError(503, "warming_up", f"pricing cache loading, about {int(left) + 1}s left")


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.status, self.code, self.detail = status, code, detail


def distance_band(origin: str, destination: str) -> int:
    return sum(map(ord, origin + destination)) % 5 + 1


def fare_cents(origin: str, destination: str, weight_kg: float) -> int:
    tenths = round(weight_kg * 10, 6)
    # Weight is billed per started 100 g. The legacy pricing build truncates.
    billed = math.floor(tenths) if PRICING_BUILD == "legacy_floor" else math.ceil(tenths)
    return 1500 + distance_band(origin, destination) * 700 + billed * 35


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_quote(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    require_warm()
    origin, destination = str(body.get("origin", "")), str(body.get("destination", ""))
    weight = body.get("weight_kg")
    if not CODE.match(origin) or not CODE.match(destination):
        raise ApiError(400, "invalid_request", "origin and destination are three upper-case letters")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < weight <= 20000:
        raise ApiError(400, "invalid_request", "weight_kg must be a number in (0, 20000]")
    quote = {
        "quote_id": str(uuid.uuid4()),
        "origin": origin,
        "destination": destination,
        "weight_kg": str(weight),
        "fare_cents": fare_cents(origin, destination, float(weight)),
        "priced_by": APP_VERSION,
        "created_at": now_iso(),
    }
    DDB.call("PutItem", {"TableName": QUOTES_TABLE, "Item": to_item(quote),
                         "ConditionExpression": "attribute_not_exists(quote_id)"})
    return 201, public(quote)


def public(quote: dict[str, Any]) -> dict[str, Any]:
    out = dict(quote)
    out["weight_kg"] = float(out["weight_kg"])
    return out


def get_quote(quote_id: str) -> tuple[int, dict[str, Any]]:
    if not ID.match(quote_id):
        raise ApiError(400, "invalid_request", "quote_id is not a quote identifier")
    found = DDB.call("GetItem", {"TableName": QUOTES_TABLE, "ConsistentRead": True,
                                 "Key": {"quote_id": {"S": quote_id}}}).get("Item")
    if not found:
        raise ApiError(404, "not_found", "unknown quote_id")
    return 200, public(from_item(found))


def ready() -> tuple[int, dict[str, Any]]:
    require_warm()
    try:
        status = DDB.call("DescribeTable", {"TableName": QUOTES_TABLE})["Table"].get("TableStatus")
    except AwsError as exc:
        raise ApiError(503, "not_ready", f"{QUOTES_TABLE}: {exc.code}") from exc
    if status != "ACTIVE":
        raise ApiError(503, "not_ready", f"{QUOTES_TABLE} is {status}")
    return 200, {"status": "ready", "version": APP_VERSION, "color": COLOR, "task": TASK}


def selftest() -> tuple[int, dict[str, Any]]:
    """Release verification: reference pricing plus a storage round trip."""
    require_warm()
    failures = []
    for origin, destination, weight, expected in GOLDEN:
        got = fare_cents(origin, destination, weight)
        if got != expected:
            failures.append({"case": f"{origin}-{destination}-{weight}", "expected": expected, "got": got})
    storage = "ok"
    try:
        probe = f"00000000-0000-4000-8000-{uuid.uuid4().hex[:12]}"
        DDB.call("PutItem", {"TableName": QUOTES_TABLE, "Item": to_item(
            {"quote_id": probe, "selftest": True, "priced_by": APP_VERSION, "created_at": now_iso(),
             "expires_at": int(time.time()) + 3600})})
        if not DDB.call("GetItem", {"TableName": QUOTES_TABLE, "ConsistentRead": True,
                                    "Key": {"quote_id": {"S": probe}}}).get("Item"):
            storage = "read_back_failed"
        DDB.call("DeleteItem", {"TableName": QUOTES_TABLE, "Key": {"quote_id": {"S": probe}}})
    except AwsError as exc:
        storage = f"error:{exc.code}"
    passed = not failures and storage == "ok"
    payload = {"passed": passed, "version": APP_VERSION, "color": COLOR, "task": TASK,
               "cases": len(GOLDEN), "failures": failures, "storage": storage}
    return (200 if passed else 500), payload


ROUTES = [
    ("GET", re.compile(r"^/health/live$"), "live"),
    ("GET", re.compile(r"^/health/ready$"), "ready"),
    ("GET", re.compile(r"^/release/selftest$"), "selftest"),
    ("GET", re.compile(r"^/release$"), "release"),
    ("POST", re.compile(r"^/quotes$"), "create"),
    ("GET", re.compile(r"^/quotes/(?P<id>[^/]+)$"), "get"),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BeaconFare"

    def log_message(self, *_args: Any) -> None:
        return

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            raise ApiError(413, "payload_too_large")
        raw = self.rfile.read(length) if length else b""
        try:
            value = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ApiError(400, "invalid_json") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_json", "body must be a JSON object")
        return value

    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        request_id = self.headers.get("X-Request-Id") or str(uuid.uuid4())
        path = self.path.split("?", 1)[0]
        status, payload = 404, {"code": "not_found"}
        try:
            for verb, pattern, name in ROUTES:
                match = pattern.match(path)
                if not match or verb != method:
                    continue
                if name == "live":
                    status, payload = 200, {"status": "live"}
                elif name == "ready":
                    status, payload = ready()
                elif name == "selftest":
                    status, payload = selftest()
                elif name == "release":
                    status, payload = 200, {"version": APP_VERSION, "color": COLOR, "task": TASK}
                elif name == "create":
                    status, payload = create_quote(self._body())
                elif name == "get":
                    status, payload = get_quote(match.group("id"))
                break
        except ApiError as exc:
            status, payload = exc.status, {"code": exc.code, "detail": exc.detail}
        except AwsError as exc:
            status, payload = 502, {"code": "storage_error", "detail": exc.code}
        except Exception as exc:  # noqa: BLE001
            status, payload = 500, {"code": "internal_error", "detail": type(exc).__name__}

        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", request_id)
        self.send_header("X-BeaconFare-Version", APP_VERSION)
        self.send_header("X-BeaconFare-Color", COLOR)
        self.send_header("X-BeaconFare-Task", TASK)
        self.end_headers()
        self.wfile.write(body)
        if not path.startswith("/health/"):
            log("request", request_id=request_id, method=method, path=path, status=status,
                version=APP_VERSION, color=COLOR, duration_ms=int((time.monotonic() - started) * 1000))

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


def main() -> None:
    log("api_starting", port=PORT, version=APP_VERSION, color=COLOR, task=TASK, quotes_table=QUOTES_TABLE,
        warmup_seconds=WARMUP_SECONDS)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
