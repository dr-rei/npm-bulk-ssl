"""Minimal, typed client for the Nginx Proxy Manager REST API."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from . import __version__
from .models import Certificate, Host, HostKind

RETRY_STATUSES = {502, 503, 504}


class NpmError(Exception):
    """A failed NPM API call, with a human-readable message."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        raise ValueError("NPM URL is empty")
    if "://" not in url:
        # Bare "10.0.0.5" or "npm.lan": default to the NPM admin port 81.
        host = url.split("/", 1)[0]
        url = "http://" + url + ("" if ":" in host else ":81")
    return url


class NpmClient:
    def __init__(
        self,
        base_url: str,
        *,
        verify: bool | str = True,
        timeout: float = 30.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = normalize_url(base_url)
        self.retries = retries
        self._http = httpx.Client(
            base_url=self.base_url,
            verify=verify,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": f"npm-bulk-ssl/{__version__}", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> NpmClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport -------------------------------------------------------
    def _request(self, method: str, path: str, json: Any = None, params: dict[str, Any] | None = None) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._http.request(method, path, json=json, params=params)
            except httpx.TransportError as exc:  # connection refused, timeout, TLS ...
                last_exc = exc
            else:
                if resp.status_code in RETRY_STATUSES and attempt < self.retries:
                    last_exc = NpmError(f"HTTP {resp.status_code}", resp.status_code)
                else:
                    return self._decode(resp)
            if attempt < self.retries:
                time.sleep(min(0.5 * 2**attempt, 4.0))
        if isinstance(last_exc, NpmError):
            raise last_exc
        raise NpmError(f"Cannot reach NPM at {self.base_url}: {last_exc}") from last_exc

    @staticmethod
    def _decode(resp: httpx.Response) -> Any:
        try:
            body = resp.json() if resp.content else None
        except ValueError:
            body = None
        if resp.is_success:
            return body
        message = ""
        if isinstance(body, dict):
            err = body.get("error")
            message = err.get("message", "") if isinstance(err, dict) else str(err or "")
        raise NpmError(message or f"HTTP {resp.status_code} {resp.reason_phrase}", resp.status_code)

    # -- auth ------------------------------------------------------------
    def login(self, email: str, password: str, totp: Callable[[], str] | None = None) -> None:
        """Authenticate and keep the bearer token in memory only."""
        data = self._request("POST", "/api/tokens", {"identity": email, "secret": password})
        if isinstance(data, dict) and data.get("requires_2fa"):
            if totp is None:
                raise NpmError("This account has 2FA enabled; a TOTP code is required")
            data = self._request("POST", "/api/tokens/2fa", {"challenge_token": data["challenge_token"], "code": totp().strip()})
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise NpmError("Login failed: NPM returned no token")
        self._http.headers["Authorization"] = f"Bearer {token}"

    # -- resources -------------------------------------------------------
    def certificates(self) -> list[Certificate]:
        certs = [Certificate.from_api(c) for c in self._request("GET", "/api/nginx/certificates") or []]
        return sorted(certs, key=lambda c: c.nice_name.lower())

    def hosts(self, kinds: tuple[HostKind, ...] = tuple(HostKind)) -> list[Host]:
        result: list[Host] = []
        for kind in kinds:
            result.extend(Host.from_api(kind, h) for h in self._request("GET", kind.api_path) or [])
        return result

    def get_host(self, kind: HostKind, host_id: int) -> Host:
        return Host.from_api(kind, self._request("GET", f"{kind.api_path}/{host_id}"))

    def update_host(self, kind: HostKind, host_id: int, changes: dict[str, Any]) -> Host:
        """Partial update; NPM only touches the fields sent and regenerates that host's nginx config."""
        return Host.from_api(kind, self._request("PUT", f"{kind.api_path}/{host_id}", changes))
