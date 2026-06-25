"""Thin HTTP client for the Kion v3 REST API.

Kion wraps list/detail responses as ``{"status": 200, "data": ...}``; this client
unwraps ``data`` for callers. All paths are relative to ``<url>/api`` and should
start with ``/v3/...``.
"""
from __future__ import annotations

import time
from typing import Any

import requests


class KionAPIError(Exception):
    def __init__(self, status: int, method: str, path: str, body: str):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body}")


# Status codes worth retrying with backoff.
_RETRY = {429, 500, 502, 503, 504}


class KionClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        verify_ssl: bool = True,
        timeout: int = 60,
        max_retries: int = 3,
        api_prefix: str = "/api",
    ):
        # Hosted installs serve the API under /api (reverse proxy); a Kion app hit
        # directly (e.g. localhost) serves it at the root. api_prefix selects which.
        self.base = base_url.rstrip("/") + "/" + api_prefix.strip("/") if api_prefix.strip("/") else base_url.rstrip("/")
        self.verify = verify_ssl
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        if not verify_ssl:
            requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json: Any = None,
    ) -> Any:
        url = self.base + path
        resp = None
        for attempt in range(self.max_retries + 1):
            resp = self.session.request(
                method,
                url,
                params=params,
                json=json,
                verify=self.verify,
                timeout=self.timeout,
            )
            if resp.status_code in _RETRY and attempt < self.max_retries:
                time.sleep(2**attempt)
                continue
            break

        assert resp is not None
        if not resp.ok:
            raise KionAPIError(resp.status_code, method, path, resp.text[:1000])

        if not resp.content:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return resp.text
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, json: Any = None) -> Any:
        return self._request("POST", path, json=json)

    def patch(self, path: str, json: Any = None) -> Any:
        return self._request("PATCH", path, json=json)
