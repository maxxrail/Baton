"""HTTP client for the Northwind store API (the mock commerce app).

- Every request has a timeout (default 5 s).
- GETs are retried once on a 5xx, a timeout or a connection error. POSTs are never retried: a
  POST that failed may already have been applied (a 5xx after the refund committed), and the
  store doesn't dedupe, so a blind retry could refund twice. A failed POST is reported as
  `SERVICE_UNAVAILABLE` and the agent tells the customer; it can re-check with a GET.
- Error responses become `ToolError(code, customer_safe_message)`. The store's machine-readable
  `error.code` is kept; its free-text message is not passed on (it isn't written for customers).

Tests pass an `httpx.Client` that talks to the mock app in-process (Starlette's `TestClient`),
so no sockets are opened.
"""

from typing import cast
from urllib.parse import quote

import httpx

from baton.tools.errors import ToolError

JsonObject = dict[str, object]
DEFAULT_TIMEOUT_S = 5.0


def path_part(value: str) -> str:
    """Quote a user-supplied id for use in a URL path (`../customers` can't escape)."""
    return quote(value.strip(), safe="")


class CommerceClient:
    def __init__(self, http: httpx.Client, *, retries: int = 1) -> None:
        self.http = http
        self.retries = retries

    @classmethod
    def from_url(cls, base_url: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> "CommerceClient":
        return cls(httpx.Client(base_url=base_url, timeout=timeout_s))

    def close(self) -> None:
        self.http.close()

    def get(self, path: str, params: dict[str, str] | None = None) -> JsonObject:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: JsonObject | None = None) -> JsonObject:
        return self._request("POST", path, body=body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: JsonObject | None = None,
    ) -> JsonObject:
        attempts = 1 + (self.retries if method == "GET" else 0)
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                resp = self.http.request(method, path, params=params, json=body)
            except httpx.TransportError as exc:  # timeouts are TransportErrors too
                if not last:
                    continue
                raise ToolError("SERVICE_UNAVAILABLE") from exc
            if resp.status_code >= 500:
                if not last:
                    continue
                raise ToolError("SERVICE_UNAVAILABLE")
            return _parse(resp)
        raise AssertionError("unreachable")  # pragma: no cover


def _parse(resp: httpx.Response) -> JsonObject:
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.is_success and isinstance(data, dict):
        return cast(JsonObject, data)
    code = _error_code(data)
    if code is None:
        code = {404: "NOT_FOUND", 422: "INVALID_INPUT"}.get(resp.status_code, "UNKNOWN_ERROR")
    raise ToolError(code)


def _error_code(data: object) -> str | None:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return str(error["code"])
    return None
