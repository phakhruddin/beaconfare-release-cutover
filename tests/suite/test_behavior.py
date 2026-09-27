"""Observed plane.

Real requests through the production listener with fresh data. Asserts on
product results: prices, stored quotes and the release that priced them.
"""
from __future__ import annotations

import random

from .tools.api import expected_fare
from .tools.errors import SubmissionFailure
from .tools.results import CheckResult, Outcome
from .tools.trial import TrialContext, obligation


@obligation("observed.quotes_roundtrip")
def test_quotes_roundtrip(trial: TrialContext) -> CheckResult:
    """Fresh quotes are priced correctly, stored and read back."""
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
        want = expected_fare(origin, destination, weight)
        if body.get("fare_cents") != want:
            raise SubmissionFailure(f"{origin}-{destination} {weight} kg priced {body.get('fare_cents')}, expected {want}")
        if body.get("priced_by") != response.version:
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

    trial.facts["early_quotes"] = [q["quote_id"] for q in issued]
    return CheckResult(
        "observed.quotes_roundtrip", Outcome.PASS,
        f"{len(issued)} fresh quotes were priced correctly, stored and read back",
    )
