from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from npm_bulk_ssl.client import NpmClient

PATHS = {"proxy-hosts": "proxy", "redirection-hosts": "redirect", "dead-hosts": "dead"}


class FakeNpm:
    """In-memory stand-in for the NPM REST API, mirroring the 2.16 payload shapes."""

    def __init__(self, n_proxy: int = 6, totp: str | None = None) -> None:
        self.totp = totp
        self.token = "tok-123"
        self.calls: list[tuple[str, str, Any]] = []
        self.fail_ids: set[tuple[str, int]] = set()
        self.flaky = 0  # number of 503s to return before succeeding
        self.certs = [
            {
                "id": 1,
                "nice_name": "Wildcard example.com",
                "domain_names": ["*.example.com", "example.com"],
                "provider": "letsencrypt",
                "expires_on": "2099-01-01 00:00:00",
            },
            {
                "id": 2,
                "nice_name": "Old other.org",
                "domain_names": ["other.org"],
                "provider": "other",
                "expires_on": "2020-01-01 00:00:00",
            },
        ]
        self.hosts: dict[str, list[dict[str, Any]]] = {"proxy": [], "redirect": [], "dead": []}
        for i in range(1, n_proxy + 1):
            self.hosts["proxy"].append(
                {
                    "id": i,
                    "domain_names": [f"app{i}.example.com"],
                    "forward_scheme": "http",
                    "forward_host": f"10.0.0.{i}",
                    "forward_port": 8080,
                    "certificate_id": 2,
                    "ssl_forced": i % 2 == 0,
                    "http2_support": False,
                    "hsts_enabled": False,
                    "hsts_subdomains": False,
                    "enabled": True,
                }
            )
        self.hosts["redirect"].append(
            {
                "id": 1,
                "domain_names": ["go.other.org"],
                "forward_http_code": 301,
                "forward_scheme": "https",
                "forward_domain_name": "other.org",
                "certificate_id": 0,
                "ssl_forced": False,
                "http2_support": False,
                "hsts_enabled": False,
                "hsts_subdomains": False,
                "enabled": True,
            }
        )
        self.hosts["dead"].append(
            {
                "id": 1,
                "domain_names": ["gone.example.com"],
                "certificate_id": 1,
                "ssl_forced": True,
                "http2_support": True,
                "hsts_enabled": False,
                "hsts_subdomains": False,
                "enabled": True,
            }
        )

    @property
    def puts(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] == "PUT"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, body))
        if self.flaky:
            self.flaky -= 1
            return httpx.Response(503)
        if path == "/api/tokens" and request.method == "POST":
            if body != {"identity": "admin@example.com", "secret": "pw"}:
                return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid email or password"}})
            if self.totp:
                return httpx.Response(200, json={"requires_2fa": True, "challenge_token": "chal"})
            return httpx.Response(200, json={"token": self.token, "expires": "2099-01-01T00:00:00Z"})
        if path == "/api/tokens/2fa":
            if body == {"challenge_token": "chal", "code": self.totp}:
                return httpx.Response(200, json={"token": self.token})
            return httpx.Response(401, json={"error": {"message": "Invalid verification code"}})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": {"message": "Unauthorized"}})
        if path == "/api/nginx/certificates":
            return httpx.Response(200, json=self.certs)
        parts = path.strip("/").split("/")  # api nginx <kind> [id]
        kind = PATHS.get(parts[2]) if len(parts) >= 3 else None
        if kind is None:
            return httpx.Response(404, json={"error": {"message": "Not found"}})
        if len(parts) == 3:
            return httpx.Response(200, json=self.hosts[kind])
        host = next((h for h in self.hosts[kind] if h["id"] == int(parts[3])), None)
        if host is None:
            return httpx.Response(404, json={"error": {"message": "Not found"}})
        if request.method == "PUT":
            if (kind, host["id"]) in self.fail_ids:
                return httpx.Response(500, json={"error": {"message": "nginx reload failed"}})
            host.update(body)
        return httpx.Response(200, json=host)

    def client(self, **kw: Any) -> NpmClient:
        return NpmClient("http://npm.test:81", transport=httpx.MockTransport(self.handler), **kw)


@pytest.fixture
def fake() -> FakeNpm:
    return FakeNpm()


@pytest.fixture
def client(fake: FakeNpm) -> NpmClient:
    c = fake.client(retries=0)
    c.login("admin@example.com", "pw")
    return c
