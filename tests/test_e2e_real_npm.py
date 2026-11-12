"""End-to-end test against a real, disposable Nginx Proxy Manager.

Opt-in: set NPM_E2E_URL (e.g. http://127.0.0.1:18081) plus NPM_E2E_EMAIL / NPM_E2E_PASSWORD.
NEVER point this at a production NPM: it creates and modifies hosts and certificates.
"""

from __future__ import annotations

import datetime as dt
import os

import httpx
import pytest

from npm_bulk_ssl import cli

URL = os.environ.get("NPM_E2E_URL")
EMAIL = os.environ.get("NPM_E2E_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("NPM_E2E_PASSWORD", "changeme123")
pytestmark = pytest.mark.skipif(not URL, reason="NPM_E2E_URL not set")


def self_signed(cn: str, sans: list[str]) -> tuple[bytes, bytes]:
    crypto = pytest.importorskip("cryptography")  # noqa: F841
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
    )


@pytest.fixture(scope="module")
def api():
    # NPM reloads nginx (which fronts its API) after each change, dropping keep-alive sockets.
    http = httpx.Client(base_url=URL, timeout=60, headers={"Connection": "close"})
    token = http.post("/api/tokens", json={"identity": EMAIL, "secret": PASSWORD}).raise_for_status().json()["token"]
    http.headers["Authorization"] = f"Bearer {token}"
    yield http
    http.close()


@pytest.fixture(scope="module")
def seeded(api):
    def cert(nice: str, sans: list[str]) -> int:
        cid = api.post("/api/nginx/certificates", json={"provider": "other", "nice_name": nice}).raise_for_status().json()["id"]
        pem, key = self_signed(sans[0], sans)
        api.post(
            f"/api/nginx/certificates/{cid}/upload", files={"certificate": ("cert.pem", pem), "certificate_key": ("key.pem", key)}
        ).raise_for_status()
        return cid

    run = dt.datetime.now().strftime("%H%M%S")
    old = cert(f"e2e-old-{run}", ["old.invalid"])
    new = cert(f"e2e-new-{run}", [f"*.e2e{run}.test", f"e2e{run}.test"])
    hosts = []
    for i in range(12):
        hosts.append(
            (
                "proxy-hosts",
                api.post(
                    "/api/nginx/proxy-hosts",
                    json={
                        "domain_names": [f"h{i}.e2e{run}.test"],
                        "forward_scheme": "http",
                        "forward_host": "127.0.0.1",
                        "forward_port": 8080,
                        "certificate_id": old,
                        "ssl_forced": False,
                        "http2_support": i % 2 == 0,
                    },
                )
                .raise_for_status()
                .json()["id"],
            )
        )
    hosts.append(
        (
            "redirection-hosts",
            api.post(
                "/api/nginx/redirection-hosts",
                json={
                    "domain_names": [f"r.e2e{run}.test"],
                    "forward_http_code": 301,
                    "forward_scheme": "https",
                    "forward_domain_name": "example.org",
                    "preserve_path": True,
                    "certificate_id": old,
                },
            )
            .raise_for_status()
            .json()["id"],
        )
    )
    hosts.append(
        (
            "dead-hosts",
            api.post(
                "/api/nginx/dead-hosts",
                json={
                    "domain_names": [f"d.e2e{run}.test"],
                    "certificate_id": old,
                },
            )
            .raise_for_status()
            .json()["id"],
        )
    )
    return {"old": old, "new": new, "run": run, "hosts": hosts}


def test_apply_and_rollback_against_real_npm(api, seeded, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NPM_URL", URL)
    monkeypatch.setenv("NPM_EMAIL", EMAIL)
    monkeypatch.setenv("NPM_PASSWORD", PASSWORD)
    monkeypatch.setenv("NPM_BULK_SSL_HOME", str(tmp_path))
    match = f"*.e2e{seeded['run']}.test"

    def state():
        return {(k, i): api.get(f"/api/nginx/{k}/{i}").raise_for_status().json() for k, i in seeded["hosts"]}

    before = state()
    rc = cli.main(["apply", "--cert-id", str(seeded["new"]), "--match", match, "--force-ssl", "on", "--yes"])
    assert rc == cli.EXIT_OK
    after = state()
    for key, host in after.items():
        assert host["certificate_id"] == seeded["new"], key
        assert host["ssl_forced"] is True or host["ssl_forced"] == 1, key
        # untouched fields survive the partial update
        assert host["domain_names"] == before[key]["domain_names"]
        assert bool(host["http2_support"]) == bool(before[key]["http2_support"])
        if key[0] == "proxy-hosts":
            assert host["forward_host"] == "127.0.0.1" and host["forward_port"] == 8080

    backup = next(tmp_path.glob("runs/*apply/backup.json"))
    assert cli.main(["rollback", str(backup), "--yes"]) == cli.EXIT_OK
    for key, host in state().items():
        assert host["certificate_id"] == seeded["old"], key
        assert bool(host["ssl_forced"]) is False, key
