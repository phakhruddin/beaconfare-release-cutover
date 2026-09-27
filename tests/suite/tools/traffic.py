"""Production traffic that runs while a lifecycle script runs.

A background thread keeps issuing fresh quotes and reading earlier ones back
through the production listener. Every response is recorded with the release
and color that answered, so a check can prove what customers saw during a
release, not just the end state.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field

from .api import Api, expected_fare

LANES = ["SEA", "PDX", "LAX", "JFK", "ORD", "ATL", "MIA", "DEN", "BOS", "DFW"]


@dataclass
class Sample:
    at: float
    method: str
    status: int
    version: str
    color: str
    error: str = ""
    fare_ok: bool = True
    quote_id: str = ""


@dataclass
class TrafficReport:
    samples: list[Sample] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.samples)

    def failures(self) -> list[Sample]:
        return [s for s in self.samples if s.status == 0 or s.status >= 500]

    def versions(self) -> set[str]:
        return {s.version for s in self.samples if s.version}

    def mispriced(self) -> list[Sample]:
        return [s for s in self.samples if not s.fare_ok]

    def quote_ids(self) -> list[str]:
        return [s.quote_id for s in self.samples if s.quote_id]

    def describe(self, limit: int = 6) -> str:
        bad = self.failures()[:limit]
        return ", ".join(f"{s.method} {s.status or s.error} ({s.version or '-'}/{s.color or '-'})" for s in bad)


class Traffic:
    """Context manager: steady production traffic until exit."""

    def __init__(self, api: Api, interval: float = 0.15) -> None:
        self.api = api
        self.interval = interval
        self.report = TrafficReport()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._lock = threading.Lock()

    def _record(self, sample: Sample) -> None:
        with self._lock:
            self.report.samples.append(sample)

    def _run(self) -> None:
        rng = random.Random()
        written: list[str] = []
        while not self._stop.is_set():
            origin, destination = rng.sample(LANES, 2)
            weight = round(rng.uniform(0.05, 900.0), 2)
            response = self.api.post("/quotes", {"origin": origin, "destination": destination,
                                                 "weight_kg": weight})
            sample = Sample(time.monotonic(), "POST", response.status, response.version,
                            response.color, response.error)
            if response.status == 201:
                body = response.json() or {}
                sample.quote_id = str(body.get("quote_id", ""))
                sample.fare_ok = body.get("fare_cents") == expected_fare(origin, destination, weight)
                if sample.quote_id:
                    written.append(sample.quote_id)
            self._record(sample)
            if written:
                read = self.api.get(f"/quotes/{rng.choice(written[-50:])}")
                self._record(Sample(time.monotonic(), "GET", read.status, read.version,
                                    read.color, read.error))
            self._stop.wait(self.interval)

    def __enter__(self) -> "Traffic":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=30)
