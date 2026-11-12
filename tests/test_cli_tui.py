from __future__ import annotations

import json

import pytest
from textual.widgets import Checkbox, Select

from npm_bulk_ssl import cli
from npm_bulk_ssl.tui import BulkSslApp, ConfirmScreen, HostScreen, OptionsScreen, ProgressScreen, Session

from .conftest import FakeNpm


@pytest.fixture
def env(fake: FakeNpm, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "NpmClient", lambda url, **kw: fake.client(**kw))
    monkeypatch.setenv("NPM_URL", "npm.test")
    monkeypatch.setenv("NPM_EMAIL", "admin@example.com")
    monkeypatch.setenv("NPM_PASSWORD", "pw")
    monkeypatch.setenv("NPM_BULK_SSL_HOME", str(tmp_path))
    return fake


def test_cli_apply_headless(env: FakeNpm, tmp_path, capsys) -> None:
    rc = cli.main(["apply", "--cert-id", "1", "--match", "app*.example.com", "--force-ssl", "on", "--yes"])
    assert rc == cli.EXIT_OK
    assert len(env.puts) == 6
    assert all(h["certificate_id"] == 1 and h["ssl_forced"] for h in env.hosts["proxy"])
    backup = next(tmp_path.glob("runs/*/backup.json"))
    assert cli.main(["rollback", str(backup), "--yes"]) == cli.EXIT_OK
    assert all(h["certificate_id"] == 2 for h in env.hosts["proxy"])


def test_cli_refuses_mismatch_and_unconfirmed(env: FakeNpm, monkeypatch) -> None:
    assert cli.main(["apply", "--cert-id", "1", "--all", "--yes"]) == cli.EXIT_USAGE  # go.other.org uncovered
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert cli.main(["apply", "--cert-id", "1", "--match", "*.example.com"]) == cli.EXIT_USAGE  # no --yes
    assert cli.main(["apply", "--cert-id", "1"]) == cli.EXIT_USAGE  # no selector
    assert cli.main(["apply", "--cert-id", "99", "--all"]) == cli.EXIT_USAGE
    assert not env.puts
    assert cli.main(["apply", "--cert-id", "1", "--all", "--allow-mismatch", "--dry-run"]) == cli.EXIT_OK
    assert not env.puts


def test_cli_partial_failure_exit_code(env: FakeNpm) -> None:
    env.fail_ids.add(("proxy", 2))
    assert cli.main(["apply", "--cert-id", "1", "--match", "*.example.com", "--yes"]) == cli.EXIT_PARTIAL


def test_cli_bad_login_and_options_after_command(env: FakeNpm, monkeypatch, capsys) -> None:
    monkeypatch.setenv("NPM_PASSWORD", "wrong")
    assert cli.main(["list-certs"]) == cli.EXIT_NPM
    monkeypatch.setenv("NPM_PASSWORD", "pw")
    assert cli.main(["--kind", "dead", "list-hosts", "--json"]) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert [h["key"] for h in data] == ["dead:1"]
    assert cli.main(["list-hosts", "--kind", "redirect", "--json"]) == cli.EXIT_OK
    assert [h["key"] for h in json.loads(capsys.readouterr().out)] == ["redirect:1"]


def make_app(fake: FakeNpm, tmp_path) -> BulkSslApp:
    client = fake.client(retries=0)
    client.login("admin@example.com", "pw")
    return BulkSslApp(Session(client, "me", client.certificates(), client.hosts(), home=tmp_path))


async def wait_for_result(app, pilot) -> None:
    """The run happens in a worker thread; wait until the progress screen has its result."""
    for _ in range(200):
        if isinstance(app.screen, ProgressScreen) and app.screen.result is not None:
            return
        await pilot.pause(0.05)
    raise AssertionError("run did not finish")


async def test_tui_full_flow(fake: FakeNpm, tmp_path) -> None:
    app = make_app(fake, tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        # Step 1: certificates sorted by name -> row 0 is "Old other.org", row 1 the wildcard.
        await pilot.press("down", "enter")
        assert isinstance(app.screen, HostScreen)
        assert app.session.certificate.id == 1
        screen = app.screen
        # Search: typed letters go to the input, not to the 'a'/'n' bindings.
        await pilot.press("slash", *"app", "enter")
        assert screen.ft.search.value == "app"
        assert not app.session.selected
        assert len(screen.ft.shown) == 6
        await pilot.press("a")  # select all shown
        assert len(app.session.selected) == 6
        await pilot.press("space")  # toggle cursor row off
        assert len(app.session.selected) == 5
        await pilot.press("i")  # invert shown -> only the toggled one
        assert len(app.session.selected) == 1
        await pilot.press("A")  # select ALL, ignoring the filter
        assert len(app.session.selected) == 8
        await pilot.press("N")
        assert not app.session.selected
        # Narrow by AND-ed terms, then select those.
        screen.ft.search.value = "app1 example"
        await pilot.pause()
        assert [h.key for h in screen.ft.shown] == ["proxy:1"]
        await pilot.press("a")
        screen.ft.search.value = ""
        await pilot.pause()
        await pilot.press("s")  # sort by first column (selected first)
        assert screen.ft.shown[0].key == "proxy:1"
        await pilot.press("c")
        assert isinstance(app.screen, OptionsScreen)
        app.screen.query_one("#hsts_enabled", Select).value = "on"
        await pilot.press("ctrl+n")
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.plan.changes[0].payload == {"certificate_id": 1, "hsts_enabled": True}
        await pilot.click("#apply")
        await pilot.pause()
        assert isinstance(app.screen, ProgressScreen)
        await wait_for_result(app, pilot)
        await pilot.press("q")
    assert app.return_value is not None and len(app.return_value.succeeded) == 1
    assert fake.hosts["proxy"][0]["certificate_id"] == 1 and fake.hosts["proxy"][0]["hsts_enabled"] is True
    assert fake.hosts["proxy"][1]["certificate_id"] == 2


async def test_tui_dry_run_and_header_sort(fake: FakeNpm, tmp_path) -> None:
    app = make_app(fake, tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("down", "enter")
        screen = app.screen
        screen.ft.sort_by("domains")
        screen.ft.sort_by("domains")  # second click reverses
        names = [h.domain_names[0] for h in screen.ft.shown]
        assert names == sorted(names, reverse=True)
        await pilot.press("c")  # nothing selected -> stays
        assert app.screen is screen
        await pilot.press("A", "c", "ctrl+n")
        confirm = app.screen
        assert isinstance(confirm, ConfirmScreen)
        assert len(confirm.plan.mismatched) == 1
        confirm.query_one("#dry", Checkbox).value = True
        await pilot.click("#apply")
        await wait_for_result(app, pilot)
        await pilot.press("q")
    assert app.return_value.dry_run and not fake.puts
    assert {r.status for r in app.return_value.results} == {"dry-run"}
