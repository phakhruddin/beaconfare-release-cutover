"""Observed plane.

Real requests through the production listener with fresh data. Asserts on
product results: stored quotes and the release that issued them.
"""
from __future__ import annotations

import random

from .tools.errors import SubmissionFailure
from .tools.results import CheckResult, Outcome
from .tools.trial import TrialContext, obligation


@obligation("observed.quotes_roundtrip")
def test_quotes_roundtrip(trial: TrialContext) -> CheckResult:
    """Fresh quotes are issued by the live release, stored and read back unchanged."""
    rng = random.Random()
    lanes = ["SEA", "PDX", "LAX", "JFK", "ORD", "ATL", "MIA", "DEN"]
    issued = []
    for _ in range(8):
        origin, destination = rng.sample(lanes, 2)
        weight = round(rng.uniform(0.05, 500.0), 2)
        response = trial.production.post("/quotes", {"origin": origin, "destination": destination,
                                                     "weight_kg": weight})
        if response.status != 201:
            raise SubmissionFailure(f"POST /quotes returned {response.status or response.error}: {response.text[:300]}")
        body = response.json() or {}
        if not isinstance(body.get("fare_cents"), int) or body["fare_cents"] <= 0:
            raise SubmissionFailure(f"POST /quotes returned no fare: {body}")
        if body.get("priced_by") != response.version or response.version != trial.manifest["release"]["live_version"]:
            raise SubmissionFailure(f"quote priced_by {body.get('priced_by')} but served by {response.version}")
        issued.append(body)

    for quote in issued:
        read = trial.production.get(f"/quotes/{quote['quote_id']}")
        if read.status != 200:
            raise SubmissionFailure(f"GET /quotes/{quote['quote_id']} returned {read.status or read.error}")
        body = read.json() or {}
        if body.get("fare_cents") != quote["fare_cents"] or body.get("priced_by") != quote["priced_by"]:
            raise SubmissionFailure(f"quote {quote['quote_id']} read back as {body}")
        item = trial.cloud.ddb.get_item(TableName=trial.quotes_table, ConsistentRead=True,
                                        Key={"quote_id": {"S": quote["quote_id"]}}).get("Item")
        if not item:
            raise SubmissionFailure(f"quote {quote['quote_id']} is not in the quotes table named in the manifest")

    # Keep the immutable response fields for later lifecycle checks.  A
    # cutover is not allowed to merely keep a quote addressable: callers must
    # receive the same price and issuing release after the services swap.
    trial.facts["early_quotes"] = issued
    return CheckResult(
        "observed.quotes_roundtrip", Outcome.PASS,
        f"{len(issued)} fresh quotes were issued by the live release, stored and read back unchanged",
    )
