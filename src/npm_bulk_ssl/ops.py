"""Planning, applying and rolling back bulk SSL changes (UI-independent core)."""

from __future__ import annotations

import fnmatch
import json
import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .client import NpmClient, NpmError
from .models import OPTION_FIELDS, SSL_FIELDS, Certificate, Host, HostKind

BACKUP_FORMAT = 1


@dataclass(frozen=True)
class Change:
    host: Host
    payload: dict[str, Any]  # only the fields that actually differ
    uncovered: tuple[str, ...] = ()  # hostnames the target certificate does not cover


@dataclass
class Plan:
    action: str  # "apply" | "rollback"
    changes: list[Change]
    unchanged: list[Host] = field(default_factory=list)
    certificate: Certificate | None = None
    missing: list[str] = field(default_factory=list)  # rollback: hosts that no longer exist

    @property
    def mismatched(self) -> list[Change]:
        return [c for c in self.changes if c.uncovered]


@dataclass(frozen=True)
class HostResult:
    host: Host
    payload: dict[str, Any]
    status: str  # "ok" | "error" | "dry-run" | "skipped"
    error: str = ""


@dataclass
class RunResult:
    run_dir: Path
    dry_run: bool
    results: list[HostResult]

    @property
    def failed(self) -> list[HostResult]:
        return [r for r in self.results if r.status == "error"]

    @property
    def skipped(self) -> list[HostResult]:
        return [r for r in self.results if r.status == "skipped"]

    @property
    def succeeded(self) -> list[HostResult]:
        return [r for r in self.results if r.status == "ok"]


OPTION_LABELS = {
    "ssl_forced": "Force SSL",
    "http2_support": "HTTP/2",
    "hsts_enabled": "HSTS",
    "hsts_subdomains": "HSTS subdomains",
}


def describe_payload(payload: dict[str, Any], cert_names: dict[int, str] | None = None) -> str:
    parts = []
    for k, v in payload.items():
        if k == "certificate_id":
            parts.append(f"cert → {(cert_names or {}).get(v, f'#{v}')}")
        else:
            parts.append(f"{OPTION_LABELS.get(k, k)} → {'on' if v else 'off'}")
    return ", ".join(parts)


# -- selection -----------------------------------------------------------
def select_hosts(
    hosts: Iterable[Host],
    *,
    kinds: Iterable[HostKind] | None = None,
    match: Iterable[str] = (),
    exclude: Iterable[str] = (),
    current_cert: Iterable[int] | None = None,
    keys: Iterable[str] = (),
) -> list[Host]:
    """Filter hosts for headless use. ``match``/``exclude`` are shell globs against any domain name."""
    kinds = set(kinds) if kinds else None
    match, exclude, keys = list(match), list(exclude), set(keys)
    certs = set(current_cert) if current_cert is not None else None
    selected = []
    for h in hosts:
        if kinds is not None and h.kind not in kinds:
            continue
        if certs is not None and h.certificate_id not in certs:
            continue
        if keys and h.key not in keys:
            continue
        if match and not any(fnmatch.fnmatch(d, p.lower()) for d in h.domain_names for p in match):
            continue
        if exclude and any(fnmatch.fnmatch(d, p.lower()) for d in h.domain_names for p in exclude):
            continue
        selected.append(h)
    return selected


# -- planning ------------------------------------------------------------
def build_plan(certificate: Certificate, hosts: Iterable[Host], overrides: dict[str, bool] | None = None) -> Plan:
    overrides = {k: bool(v) for k, v in (overrides or {}).items() if v is not None}
    unknown = set(overrides) - set(OPTION_FIELDS)
    if unknown:
        raise ValueError(f"Unknown SSL option(s): {', '.join(sorted(unknown))}")
    desired = {"certificate_id": certificate.id, **overrides}
    plan = Plan(action="apply", changes=[], certificate=certificate)
    for host in hosts:
        payload = {k: v for k, v in desired.items() if host.ssl.get(k) != v}
        if payload:
            plan.changes.append(Change(host, payload, certificate.uncovered(host.domain_names)))
        else:
            plan.unchanged.append(host)
    return plan


def load_backup(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != BACKUP_FORMAT or not isinstance(data.get("hosts"), list):
        raise ValueError(f"{path} is not an npm-bulk-ssl backup file")
    return data


def build_rollback_plan(backup: dict[str, Any], current_hosts: Iterable[Host]) -> Plan:
    by_key = {h.key: h for h in current_hosts}
    plan = Plan(action="rollback", changes=[])
    for entry in backup["hosts"]:
        key = f"{entry['kind']}:{entry['id']}"
        host = by_key.get(key)
        if host is None:
            plan.missing.append(f"{key} ({', '.join(entry.get('domain_names', []))})")
            continue
        before = {k: entry["before"][k] for k in SSL_FIELDS if k in entry["before"]}
        payload = {k: v for k, v in before.items() if host.ssl.get(k) != v}
        if payload:
            plan.changes.append(Change(host, payload))
        else:
            plan.unchanged.append(host)
    return plan


# -- state files ---------------------------------------------------------
def state_dir(override: str | os.PathLike[str] | None = None) -> Path:
    base = override or os.environ.get("NPM_BULK_SSL_HOME") or Path.home() / ".npm-bulk-ssl"
    return Path(base)


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class AuditLog:
    """Append-only JSON Lines audit trail shared by all runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


# -- execution -----------------------------------------------------------
def execute(
    client: NpmClient,
    plan: Plan,
    *,
    actor: str,
    dry_run: bool = False,
    home: Path | None = None,
    progress: Callable[[int, int, HostResult], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RunResult:
    """Apply ``plan`` host by host. A backup of every touched host is written before the first change.

    Hosts are updated sequentially: NPM regenerates and reloads nginx per update, so parallel
    writes buy little and risk overlapping reloads. A failing host never aborts the run.
    """
    home = home or state_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{plan.action}{'-dryrun' if dry_run else ''}"
    run_dir = home / "runs" / run_id
    audit = AuditLog(home / "audit.jsonl")
    cert = plan.certificate

    _write_json_atomic(
        run_dir / "backup.json",
        {
            "format": BACKUP_FORMAT,
            "tool_version": __version__,
            "run_id": run_id,
            "action": plan.action,
            "npm_url": client.base_url,
            "actor": actor,
            "dry_run": dry_run,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "certificate": {"id": cert.id, "nice_name": cert.nice_name} if cert else None,
            "hosts": [
                {
                    "kind": c.host.kind.value,
                    "id": c.host.id,
                    "domain_names": list(c.host.domain_names),
                    "before": dict(c.host.ssl),
                    "payload": c.payload,
                }
                for c in plan.changes
            ],
        },
    )
    audit.write(
        "run_start",
        run_id=run_id,
        action=plan.action,
        npm_url=client.base_url,
        actor=actor,
        dry_run=dry_run,
        hosts=len(plan.changes),
        certificate_id=cert.id if cert else None,
    )

    results: list[HostResult] = []
    total = len(plan.changes)
    for i, change in enumerate(plan.changes, 1):
        h = change.host
        if cancelled and cancelled():
            result = HostResult(h, change.payload, "skipped", "run cancelled")
        elif dry_run:
            result = HostResult(h, change.payload, "dry-run")
        else:
            try:
                updated = client.update_host(h.kind, h.id, change.payload)
                drift = {k: v for k, v in change.payload.items() if updated.ssl.get(k) != v}
                if drift:
                    raise NpmError(f"NPM accepted the update but did not apply: {sorted(drift)}")
                result = HostResult(h, change.payload, "ok")
            except NpmError as exc:
                result = HostResult(h, change.payload, "error", str(exc))
        results.append(result)
        audit.write(
            "host",
            run_id=run_id,
            host=h.key,
            domains=list(h.domain_names),
            status=result.status,
            before={k: h.ssl.get(k) for k in change.payload},
            after=change.payload,
            error=result.error or None,
        )
        if progress:
            progress(i, total, result)

    run = RunResult(run_dir, dry_run, results)
    _write_json_atomic(
        run_dir / "results.json",
        [{"host": r.host.key, "domains": list(r.host.domain_names), "status": r.status, "error": r.error} for r in results],
    )
    audit.write("run_end", run_id=run_id, ok=len(run.succeeded), failed=len(run.failed), skipped=len(run.skipped), dry_run=dry_run)
    return run
