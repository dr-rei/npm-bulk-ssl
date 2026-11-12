from __future__ import annotations

import json

import pytest

from npm_bulk_ssl.client import NpmError, normalize_url
from npm_bulk_ssl.models import Certificate, HostKind
from npm_bulk_ssl.ops import build_plan, build_rollback_plan, execute, load_backup, select_hosts

from .conftest import FakeNpm


def cert(*domains: str) -> Certificate:
    return Certificate(1, "c", tuple(domains), "x", None)


@pytest.mark.parametrize(
    ("patterns", "name", "ok"),
    [
        (("*.example.com",), "a.example.com", True),
        (("*.example.com",), "example.com", False),
        (("*.example.com",), "a.b.example.com", False),
        (("*.example.com",), "badexample.com", False),
        (("example.com",), "EXAMPLE.com", True),
        (("*.example.com",), "a.example.com.", True),
    ],
)
def test_certificate_covers(patterns: tuple[str, ...], name: str, ok: bool) -> None:
    assert cert(*patterns).covers(name) is ok


@pytest.mark.parametrize(
    ("raw", "url"),
    [
        ("10.0.0.5", "http://10.0.0.5:81"),
        ("10.0.0.5:8181", "http://10.0.0.5:8181"),
        ("https://npm.example.com/", "https://npm.example.com"),
        (" npm.lan ", "http://npm.lan:81"),
    ],
)
def test_normalize_url(raw: str, url: str) -> None:
    assert normalize_url(raw) == url


def test_login_bad_password(fake: FakeNpm) -> None:
    with pytest.raises(NpmError, match="Invalid email or password"):
        fake.client(retries=0).login("admin@example.com", "nope")


def test_login_2fa(fake: FakeNpm) -> None:
    fake.totp = "123456"
    c = fake.client(retries=0)
    with pytest.raises(NpmError, match="2FA"):
        c.login("admin@example.com", "pw")
    c.login("admin@example.com", "pw", totp=lambda: " 123456 ")
    assert len(c.certificates()) == 2


def test_retries_transient_errors(fake: FakeNpm) -> None:
    fake.flaky = 2
    c = fake.client(retries=3)
    c.login("admin@example.com", "pw")
    assert c.certificates()


def test_loads_all_kinds(client) -> None:
    hosts = client.hosts()
    assert {h.kind for h in hosts} == set(HostKind)
    assert len(hosts) == 8
    assert client.hosts((HostKind.DEAD,))[0].certificate_id == 1


def test_select_hosts(client) -> None:
    hosts = client.hosts()
    assert len(select_hosts(hosts, match=["*.example.com"])) == 7
    assert len(select_hosts(hosts, match=["*.example.com"], exclude=["app1.*"])) == 6
    assert [h.key for h in select_hosts(hosts, current_cert=[0])] == ["redirect:1"]
    assert [h.key for h in select_hosts(hosts, keys=["proxy:3"])] == ["proxy:3"]
    assert len(select_hosts(hosts, kinds=[HostKind.PROXY], current_cert=[2])) == 6


def test_plan_skips_up_to_date_and_flags_mismatch(client) -> None:
    certs = {c.id: c for c in client.certificates()}
    plan = build_plan(certs[1], client.hosts())
    assert [h.key for h in plan.unchanged] == ["dead:1"]  # already on cert 1, no overrides
    assert [c.host.key for c in plan.mismatched] == ["redirect:1"]  # go.other.org not in *.example.com
    assert all(c.payload == {"certificate_id": 1} for c in plan.changes)

    plan = build_plan(certs[1], client.hosts(), {"ssl_forced": True})
    assert plan.changes[0].payload == {"certificate_id": 1, "ssl_forced": True}  # proxy:1 had ssl_forced False
    assert plan.changes[1].payload == {"certificate_id": 1}  # proxy:2 already forced
    with pytest.raises(ValueError):
        build_plan(certs[1], [], {"bogus": True})


def test_execute_backup_audit_and_rollback(fake: FakeNpm, client, tmp_path) -> None:
    certs = {c.id: c for c in client.certificates()}
    hosts = select_hosts(client.hosts(), kinds=[HostKind.PROXY])
    fake.fail_ids.add(("proxy", 3))
    seen = []
    run = execute(
        client,
        build_plan(certs[1], hosts, {"hsts_enabled": True}),
        actor="me",
        home=tmp_path,
        progress=lambda i, n, r: seen.append((i, n, r.status)),
    )
    assert len(run.succeeded) == 5 and [r.host.key for r in run.failed] == ["proxy:3"]
    assert seen[-1] == (6, 6, "ok")
    assert fake.hosts["proxy"][0]["certificate_id"] == 1 and fake.hosts["proxy"][0]["hsts_enabled"] is True
    assert fake.hosts["proxy"][2]["certificate_id"] == 2  # failed host untouched

    backup = load_backup(run.run_dir / "backup.json")
    assert backup["hosts"][0]["before"]["certificate_id"] == 2
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [a["event"] for a in audit][0] == "run_start" and audit[-1]["failed"] == 1
    assert "pw" not in (tmp_path / "audit.jsonl").read_text()

    fake.fail_ids.clear()
    rb = build_rollback_plan(backup, client.hosts())
    assert len(rb.changes) == 5 and [h.key for h in rb.unchanged] == ["proxy:3"]
    run2 = execute(client, rb, actor="me", home=tmp_path)
    assert not run2.failed
    assert all(h["certificate_id"] == 2 and h["hsts_enabled"] is False for h in fake.hosts["proxy"])


def test_rollback_reports_deleted_hosts(fake: FakeNpm, client, tmp_path) -> None:
    certs = {c.id: c for c in client.certificates()}
    run = execute(client, build_plan(certs[1], client.hosts()[:2]), actor="me", home=tmp_path)
    fake.hosts["proxy"].pop(0)
    rb = build_rollback_plan(load_backup(run.run_dir / "backup.json"), client.hosts())
    assert len(rb.missing) == 1 and len(rb.changes) == 1


def test_dry_run_and_cancel_change_nothing(fake: FakeNpm, client, tmp_path) -> None:
    certs = {c.id: c for c in client.certificates()}
    plan = build_plan(certs[1], client.hosts())
    run = execute(client, plan, actor="me", dry_run=True, home=tmp_path)
    assert not fake.puts and {r.status for r in run.results} == {"dry-run"}
    run = execute(client, plan, actor="me", home=tmp_path, cancelled=lambda: True)
    assert not fake.puts and len(run.skipped) == len(plan.changes)


def test_load_backup_rejects_foreign_json(tmp_path) -> None:
    p = tmp_path / "x.json"
    p.write_text('{"hello": 1}')
    with pytest.raises(ValueError):
        load_backup(p)
