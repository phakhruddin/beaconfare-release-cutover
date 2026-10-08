"""HTTP client for the quote API through one load balancer listener.

Every request carries the manifest's host header and a fresh connection, so
consecutive calls are spread across tasks and a listener change is seen on
the very next request.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class ApiResponse:
    status: int
    headers: dict[str, str]
    text: str
    error: str = ""

    @property
    def version(self) -> str:
        return self.headers.get("x-beaconfare-version", "")

    @property
    def color(self) -> str:
        return self.headers.get("x-beaconfare-color", "")

    @property
    def task(self) -> str:
        return self.headers.get("x-beaconfare-task", "")

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            return None


class Api:
    def __init__(self, base_url: str, host_header: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.host_header = host_header
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None) -> ApiResponse:
        headers = {"Host": self.host_header, "Connection": "close"}
        # One retry after a timeout only (release-process.md, Production
        # during a release): the endpoint host can stall briefly under load.
        # A refused connection or any HTTP status is final.
        for attempt in (1, 2):
            try:
                response = requests.request(method, f"{self.base_url}{path}", headers=headers,
                                            json=body, timeout=self.timeout, allow_redirects=False)
                break
            except requests.Timeout as exc:
                if attempt == 2:
                    return ApiResponse(status=0, headers={}, text="", error=type(exc).__name__)
            except requests.RequestException as exc:
                return ApiResponse(status=0, headers={}, text="", error=type(exc).__name__)
        return ApiResponse(status=response.status_code,
                           headers={k.lower(): v for k, v in response.headers.items()},
                           text=response.text)

    def get(self, path: str) -> ApiResponse:
        return self.request("GET", path)

    def post(self, path: str, body: Any) -> ApiResponse:
        return self.request("POST", path, body)

