"""Domain models for Nginx Proxy Manager hosts and certificates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

#: SSL-related host fields this tool reads, changes and restores.
SSL_FIELDS = ("certificate_id", "ssl_forced", "http2_support", "hsts_enabled", "hsts_subdomains")
#: Option fields that can be overridden in bulk (everything except certificate_id).
OPTION_FIELDS = SSL_FIELDS[1:]


class HostKind(str, Enum):
    PROXY = "proxy"
    REDIRECT = "redirect"
    DEAD = "dead"

    @property
    def api_path(self) -> str:
        return {
            HostKind.PROXY: "/api/nginx/proxy-hosts",
            HostKind.REDIRECT: "/api/nginx/redirection-hosts",
            HostKind.DEAD: "/api/nginx/dead-hosts",
        }[self]

    @property
    def label(self) -> str:
        return {HostKind.PROXY: "Proxy", HostKind.REDIRECT: "Redirect", HostKind.DEAD: "404"}[self]


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Certificate:
    id: int
    nice_name: str
    domain_names: tuple[str, ...]
    provider: str
    expires_on: datetime | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Certificate:
        domains = tuple(str(d).lower() for d in data.get("domain_names") or ())
        return cls(
            id=int(data["id"]),
            nice_name=str(data.get("nice_name") or ", ".join(domains) or f"#{data['id']}"),
            domain_names=domains,
            provider=str(data.get("provider") or ""),
            expires_on=_parse_time(data.get("expires_on")),
        )

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_on is None:
            return False
        return self.expires_on <= (now or datetime.now(timezone.utc))

    def covers(self, hostname: str) -> bool:
        """True when this certificate is valid for ``hostname`` (RFC 6125 single-label wildcard)."""
        name = hostname.lower().rstrip(".")
        for pattern in self.domain_names:
            pattern = pattern.rstrip(".")
            if pattern == name:
                return True
            if pattern.startswith("*."):
                suffix = pattern[1:]  # ".example.com"
                head = name[: -len(suffix)] if name.endswith(suffix) else ""
                if head and "." not in head:
                    return True
        return False

    def uncovered(self, hostnames: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(h for h in hostnames if not self.covers(h))


@dataclass(frozen=True)
class Host:
    kind: HostKind
    id: int
    domain_names: tuple[str, ...]
    target: str
    enabled: bool
    ssl: dict[str, Any] = field(default_factory=dict)  # values of SSL_FIELDS
    certificate_name: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.id}"

    @property
    def certificate_id(self) -> int:
        return int(self.ssl.get("certificate_id") or 0)

    @classmethod
    def from_api(cls, kind: HostKind, data: dict[str, Any]) -> Host:
        if kind is HostKind.PROXY:
            target = f"{data.get('forward_scheme', 'http')}://{data.get('forward_host', '')}:{data.get('forward_port', '')}"
        elif kind is HostKind.REDIRECT:
            target = f"{data.get('forward_http_code', '')} -> {data.get('forward_scheme', '')}://{data.get('forward_domain_name', '')}"
        else:
            target = "404"
        cert = data.get("certificate") or {}
        ssl = {f: data.get(f) for f in SSL_FIELDS}
        ssl["certificate_id"] = int(ssl.get("certificate_id") or 0)
        for f in OPTION_FIELDS:
            ssl[f] = bool(ssl.get(f))
        return cls(
            kind=kind,
            id=int(data["id"]),
            domain_names=tuple(str(d).lower() for d in data.get("domain_names") or ()),
            target=target,
            enabled=bool(data.get("enabled", True)),
            ssl=ssl,
            certificate_name=str(cert.get("nice_name") or "") if isinstance(cert, dict) else "",
        )

    def flags_text(self) -> str:
        names = (("ssl_forced", "Force"), ("http2_support", "H2"), ("hsts_enabled", "HSTS"), ("hsts_subdomains", "HSTS-sub"))
        return " ".join(label for f, label in names if self.ssl.get(f)) or "-"
