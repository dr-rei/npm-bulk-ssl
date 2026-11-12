"""Interactive Textual UI: pick one certificate, multi-select hosts, review, apply."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
)

from .client import NpmClient
from .models import OPTION_FIELDS, Certificate, Host
from .ops import OPTION_LABELS, Plan, RunResult, build_plan, describe_payload, execute


@dataclass
class Session:
    """Everything the screens share. Data is loaded once before the UI starts."""

    client: NpmClient
    actor: str
    certificates: list[Certificate]
    hosts: list[Host]
    dry_run: bool = False
    home: Path | None = None
    certificate: Certificate | None = None
    selected: set[str] = field(default_factory=set)
    overrides: dict[str, bool] = field(default_factory=dict)

    def cert_name(self, cert_id: int) -> str:
        if not cert_id:
            return "none"
        for c in self.certificates:
            if c.id == cert_id:
                return c.nice_name
        return f"#{cert_id} (missing)"


def matches(haystack: str, query: str) -> bool:
    """Every whitespace-separated term must appear (case-insensitive) somewhere in the row."""
    hay = haystack.lower()
    return all(term in hay for term in query.lower().split())


# ---------------------------------------------------------------------------
# Reusable searchable / sortable table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Column:
    key: str
    label: str
    value: Callable[[Any], str]  # display text
    sort: Callable[[Any], Any] | None = None  # sort key, defaults to lower-cased text


class FilterTable(Vertical):
    """An Input + DataTable pair with live filtering and click/keyboard sorting."""

    DEFAULT_CSS = """
    FilterTable { height: 1fr; }
    FilterTable > Input { margin-bottom: 0; }
    FilterTable > DataTable { height: 1fr; }
    """

    def __init__(self, columns: list[Column], items: list[Any], key: Callable[[Any], str], placeholder: str, **kw: Any):
        super().__init__(**kw)
        self.columns = columns
        self.items = items
        self.item_key = key
        self.by_key = {key(i): i for i in items}
        self.placeholder = placeholder
        self.sort_col: str | None = None
        self.sort_reverse = False
        self.shown: list[Any] = []

    def compose(self) -> ComposeResult:
        yield Input(placeholder=self.placeholder, id="search")
        table: DataTable[Any] = DataTable(cursor_type="row", zebra_stripes=True, id="table")
        yield table

    @property
    def table(self) -> DataTable[Any]:
        return self.query_one("#table", DataTable)

    @property
    def search(self) -> Input:
        return self.query_one("#search", Input)

    def on_mount(self) -> None:
        for col in self.columns:
            self.table.add_column(col.label, key=col.key)
        self.refresh_rows()

    def row_text(self, item: Any) -> str:
        return " ".join(col.value(item) for col in self.columns)

    def refresh_rows(self) -> None:
        query = self.search.value
        rows = [i for i in self.items if matches(self.row_text(i), query)]
        if self.sort_col:
            col = next(c for c in self.columns if c.key == self.sort_col)
            rows.sort(key=col.sort or (lambda i: col.value(i).lower()), reverse=self.sort_reverse)
        self.shown = rows
        table = self.table
        current = self.cursor_key()
        table.clear()
        for item in rows:
            table.add_row(*(Text(col.value(item)) for col in self.columns), key=self.item_key(item))
        if current is not None and current in {self.item_key(i) for i in rows}:
            table.move_cursor(row=table.get_row_index(current))
        for col in self.columns:  # sort indicator in header
            arrow = (" ▼" if self.sort_reverse else " ▲") if col.key == self.sort_col else ""
            table.columns[col.key].label = Text(col.label + arrow)  # type: ignore[index]
        table.refresh()
        self.post_message(self.Changed())

    def cursor_key(self) -> str | None:
        table = self.table
        if not table.row_count:
            return None
        try:
            return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        except Exception:
            return None

    def sort_by(self, col_key: str) -> None:
        if self.sort_col == col_key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col, self.sort_reverse = col_key, False
        self.refresh_rows()

    def cycle_sort(self) -> None:
        keys = [c.key for c in self.columns]
        nxt = keys[(keys.index(self.sort_col) + 1) % len(keys)] if self.sort_col in keys else keys[0]
        self.sort_col, self.sort_reverse = nxt, False
        self.refresh_rows()

    def update_value(self, item: Any, col_key: str) -> None:
        col = next(c for c in self.columns if c.key == col_key)
        key = self.item_key(item)
        if key in self.table.rows:
            self.table.update_cell(key, col_key, Text(col.value(item)))

    class Changed(Message):
        """Rows were re-filtered or re-sorted."""

    @on(Input.Changed, "#search")
    def _search_changed(self) -> None:
        self.refresh_rows()

    @on(Input.Submitted, "#search")
    def _search_done(self) -> None:
        self.table.focus()

    @on(DataTable.HeaderSelected)
    def _header_clicked(self, event: DataTable.HeaderSelected) -> None:
        event.stop()
        self.sort_by(str(event.column_key.value))


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------
class CertificateScreen(Screen[None]):
    TITLE = "Step 1/4 · Choose the certificate to apply"
    BINDINGS = [
        Binding("slash", "focus_search", "Search"),
        Binding("escape", "focus_table", "Table", show=False),
        Binding("s", "cycle_sort", "Sort column"),
        Binding("r", "reverse_sort", "Reverse"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        usage: dict[int, int] = {}
        for h in session.hosts:
            usage[h.certificate_id] = usage.get(h.certificate_id, 0) + 1
        self.usage = usage

    def compose(self) -> ComposeResult:
        def expires(c: Certificate) -> str:
            if c.expires_on is None:
                return "-"
            return c.expires_on.strftime("%Y-%m-%d") + (" EXPIRED" if c.is_expired() else "")

        cols = [
            Column("id", "ID", lambda c: str(c.id), lambda c: c.id),
            Column("name", "Name", lambda c: c.nice_name),
            Column("domains", "Domains", lambda c: ", ".join(c.domain_names)),
            Column("provider", "Provider", lambda c: c.provider),
            Column("expires", "Expires", expires, lambda c: c.expires_on.timestamp() if c.expires_on else 0),
            Column("used", "Used by", lambda c: str(self.usage.get(c.id, 0)), lambda c: self.usage.get(c.id, 0)),
        ]
        yield Header()
        yield Label("Pick exactly one certificate. [b]Enter[/b] selects it. Type to search; click a header to sort.")
        yield FilterTable(cols, self.session.certificates, lambda c: str(c.id), "Search certificates…", id="certs")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(FilterTable).table.focus()

    @property
    def ft(self) -> FilterTable:
        return self.query_one(FilterTable)

    def action_focus_search(self) -> None:
        self.ft.search.focus()

    def action_focus_table(self) -> None:
        self.ft.table.focus()

    def action_cycle_sort(self) -> None:
        self.ft.cycle_sort()

    def action_reverse_sort(self) -> None:
        if self.ft.sort_col:
            self.ft.sort_by(self.ft.sort_col)

    @on(DataTable.RowSelected)
    def chosen(self, event: DataTable.RowSelected) -> None:
        cert = self.ft.by_key[str(event.row_key.value)]
        self.session.certificate = cert
        self.app.push_screen(HostScreen(self.session))


class HostScreen(Screen[None]):
    TITLE = "Step 2/4 · Select hosts"
    BINDINGS = [
        Binding("space", "toggle_row", "Toggle"),
        Binding("a", "select_shown", "Select shown"),
        Binding("n", "deselect_shown", "Deselect shown"),
        Binding("i", "invert_shown", "Invert shown"),
        Binding("A", "select_all", "Select ALL"),
        Binding("N", "clear_all", "Clear ALL"),
        Binding("slash", "focus_search", "Search"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("r", "reverse_sort", "Reverse", show=False),
        Binding("c", "next", "Continue"),
        Binding("escape", "back", "Back"),
    ]
    CSS = """
    #toolbar { height: auto; }
    #toolbar Button { min-width: 14; margin-right: 1; }
    #status { padding: 0 1; color: $text-muted; }
    """

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        s = self.session
        cert = s.certificate
        assert cert is not None

        def mark(h: Host) -> str:
            return "[x]" if h.key in s.selected else "[ ]"

        def covered(h: Host) -> str:
            return "NO" if cert.uncovered(h.domain_names) else "yes"

        cols = [
            Column("sel", "✓", mark, lambda h: h.key not in s.selected),
            Column("type", "Type", lambda h: h.kind.label),
            Column("domains", "Domains", lambda h: ", ".join(h.domain_names)),
            Column("target", "Target", lambda h: h.target),
            Column("cert", "Current certificate", lambda h: s.cert_name(h.certificate_id)),
            Column("covers", "Cert covers", covered),
            Column("enabled", "Enabled", lambda h: "yes" if h.enabled else "no"),
            Column("flags", "SSL flags", lambda h: h.flags_text()),
        ]
        yield Header()
        yield Label(f"Applying [b]{cert.nice_name}[/b] (#{cert.id}). Search filters by every column; terms are ANDed.")
        with Horizontal(id="toolbar"):
            yield Button("Select shown", id="select_shown")
            yield Button("Deselect shown", id="deselect_shown")
            yield Button("Select ALL", id="select_all")
            yield Button("Clear ALL", id="clear_all")
            yield Button("Continue →", id="next", variant="primary")
        yield FilterTable(cols, s.hosts, lambda h: h.key, "Search hosts… (e.g. example.com proxy, or 'NO' for uncovered)")
        yield Static(id="status")
        yield Footer()

    @property
    def ft(self) -> FilterTable:
        return self.query_one(FilterTable)

    def on_mount(self) -> None:
        self.ft.table.focus()
        self.update_status()

    @on(FilterTable.Changed)
    def update_status(self, *_: Any) -> None:
        s = self.session
        shown_sel = sum(1 for h in self.ft.shown if h.key in s.selected)
        self.query_one("#status", Static).update(
            f"Selected {len(s.selected)} of {len(s.hosts)} hosts · showing {len(self.ft.shown)} "
            f"({shown_sel} selected) · Space/Enter toggle · a/n/i shown · A/N all · c continue"
        )

    def _set(self, hosts: list[Host], value: bool | None) -> None:
        sel = self.session.selected
        for h in hosts:
            on_now = (h.key not in sel) if value is None else value
            (sel.add if on_now else sel.discard)(h.key)
            self.ft.update_value(h, "sel")
        self.update_status()

    def _current(self) -> list[Host]:
        key = self.ft.cursor_key()
        return [self.ft.by_key[key]] if key else []

    def action_toggle_row(self) -> None:
        self._set(self._current(), None)

    @on(DataTable.RowSelected)
    def _enter(self, event: DataTable.RowSelected) -> None:
        self._set([self.ft.by_key[str(event.row_key.value)]], None)

    def action_select_shown(self) -> None:
        self._set(self.ft.shown, True)

    def action_deselect_shown(self) -> None:
        self._set(self.ft.shown, False)

    def action_invert_shown(self) -> None:
        self._set(self.ft.shown, None)

    def action_select_all(self) -> None:
        self._set(self.session.hosts, True)

    def action_clear_all(self) -> None:
        self._set(self.session.hosts, False)

    def action_focus_search(self) -> None:
        self.ft.search.focus()

    def action_cycle_sort(self) -> None:
        self.ft.cycle_sort()

    def action_reverse_sort(self) -> None:
        if self.ft.sort_col:
            self.ft.sort_by(self.ft.sort_col)

    def action_back(self) -> None:
        if self.ft.search.has_focus:
            self.ft.table.focus()
        else:
            self.app.pop_screen()

    def action_next(self) -> None:
        if not self.session.selected:
            self.notify("Select at least one host first.", severity="warning")
            return
        self.app.push_screen(OptionsScreen(self.session))

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()


class OptionsScreen(Screen[None]):
    TITLE = "Step 3/4 · SSL options (optional override)"
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("ctrl+n", "next", "Continue")]
    CSS = """
    .opt { height: auto; margin: 0 1; }
    .opt Label { width: 20; padding-top: 1; }
    .opt Select { width: 30; }
    #next { margin: 1; }
    """

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(
            "By default each host keeps its own SSL options; only the certificate changes.\n"
            "Set an option to On/Off to force it on every selected host."
        )
        for f in OPTION_FIELDS:
            cur = self.session.overrides.get(f)
            value = "keep" if cur is None else ("on" if cur else "off")
            with Horizontal(classes="opt"):
                yield Label(OPTION_LABELS[f])
                yield Select([("Keep current", "keep"), ("On", "on"), ("Off", "off")], value=value, allow_blank=False, id=f)
        yield Button("Review changes →", id="next", variant="primary")
        yield Footer()

    @on(Button.Pressed, "#next")
    def action_next(self) -> None:
        overrides: dict[str, bool] = {}
        for f in OPTION_FIELDS:
            v = self.query_one(f"#{f}", Select).value
            if v in ("on", "off"):
                overrides[f] = v == "on"
        self.session.overrides = overrides
        self.app.push_screen(ConfirmScreen(self.session))


class ConfirmScreen(Screen[None]):
    TITLE = "Step 4/4 · Review and confirm"
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]
    CSS = """
    #summary { padding: 0 1; height: auto; }
    #changes { height: 1fr; }
    #actions { height: auto; padding: 0 1; }
    #actions Button { margin-right: 2; }
    """

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        hosts = [h for h in session.hosts if h.key in session.selected]
        assert session.certificate is not None
        self.plan: Plan = build_plan(session.certificate, hosts, session.overrides)

    def compose(self) -> ComposeResult:
        s, plan = self.session, self.plan
        cert = plan.certificate
        assert cert is not None
        lines = Text()
        lines.append(f"Certificate: {cert.nice_name} (#{cert.id}) — {', '.join(cert.domain_names)}\n", "bold")
        lines.append(f"Hosts to change: {len(plan.changes)}   already up to date (skipped): {len(plan.unchanged)}\n")
        if s.overrides:
            lines.append("Overrides: " + ", ".join(f"{OPTION_LABELS[k]}={'on' if v else 'off'}" for k, v in s.overrides.items()) + "\n")
        if cert.is_expired():
            lines.append("WARNING: this certificate is EXPIRED.\n", "bold red")
        if plan.mismatched:
            lines.append(
                f"WARNING: the certificate does not cover {len(plan.mismatched)} host(s) "
                "(marked below). Browsers will show TLS errors for them.\n",
                "bold yellow",
            )
        yield Header()
        yield Static(lines, id="summary")
        table: DataTable[Any] = DataTable(id="changes", zebra_stripes=True, cursor_type="row")
        table.add_columns("Type", "Domains", "Current certificate", "Changes", "Warning")
        for c in plan.changes:
            desc = describe_payload(c.payload, {x.id: x.nice_name for x in s.certificates})
            warn = Text("not covered: " + ", ".join(c.uncovered), "yellow") if c.uncovered else Text("")
            table.add_row(
                c.host.kind.label, Text(", ".join(c.host.domain_names)), Text(s.cert_name(c.host.certificate_id)), Text(desc), warn
            )
        yield table
        with Horizontal(id="actions"):
            yield Checkbox("Dry run (no changes)", value=s.dry_run, id="dry")
            yield Button(f"Apply to {len(plan.changes)} host(s)", id="apply", variant="error", disabled=not plan.changes)
            yield Button("Back", id="back")
        yield Footer()

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#apply")
    def _apply(self) -> None:
        self.session.dry_run = self.query_one("#dry", Checkbox).value
        self.app.push_screen(ProgressScreen(self.session, self.plan))


class ProgressScreen(Screen[None]):
    TITLE = "Applying"
    BINDINGS = [Binding("q", "finish", "Quit"), Binding("x", "cancel", "Cancel run")]
    CSS = "#log { height: 1fr; border: round $primary; } #done { padding: 0 1; height: auto; }"

    def __init__(self, session: Session, plan: Plan) -> None:
        super().__init__()
        self.session, self.plan = session, plan
        self.result: RunResult | None = None
        self._cancel = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(("DRY RUN — " if self.session.dry_run else "") + f"{len(self.plan.changes)} host(s)")
        yield ProgressBar(total=len(self.plan.changes), id="bar")
        yield RichLog(id="log", markup=False, wrap=True)
        yield Static(id="done")
        yield Footer()

    def on_mount(self) -> None:
        self.run_plan()

    @work(thread=True, exclusive=True)
    def run_plan(self) -> None:
        s = self.session

        def progress(i: int, total: int, r: Any) -> None:
            self.app.call_from_thread(self._step, r)

        result = execute(
            s.client, self.plan, actor=s.actor, dry_run=s.dry_run, home=s.home, progress=progress, cancelled=lambda: self._cancel
        )
        self.app.call_from_thread(self._finished, result)

    def _step(self, r: Any) -> None:
        self.query_one("#bar", ProgressBar).advance(1)
        style = {"ok": "green", "dry-run": "cyan", "skipped": "yellow"}.get(r.status, "red")
        line = Text(f"{r.status.upper():8} ", style)
        line.append(f"{r.host.kind.label} {', '.join(r.host.domain_names)}")
        if r.error:
            line.append(f"  {r.error}", "red")
        self.query_one("#log", RichLog).write(line)

    def _finished(self, result: RunResult) -> None:
        self.result = result
        self.app.result = result  # type: ignore[attr-defined]
        msg = (
            f"Done. ok={len(result.succeeded)} failed={len(result.failed)} skipped={len(result.skipped)}"
            f"{' (dry run)' if result.dry_run else ''}\nBackup: {result.run_dir / 'backup.json'}\n"
            f'Undo with:  npm-bulk-ssl rollback "{result.run_dir / "backup.json"}"\nPress q to quit.'
        )
        self.query_one("#done", Static).update(Text(msg, "bold red" if result.failed else "bold green"))

    def action_cancel(self) -> None:
        if self.result is None:
            self._cancel = True
            self.notify("Cancelling after the current host…", severity="warning")

    def action_finish(self) -> None:
        if self.result is None:
            self.notify("Still running — press x to cancel first.", severity="warning")
            return
        self.app.exit(self.result)


class BulkSslApp(App[RunResult]):
    TITLE = "npm-bulk-ssl"

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self.result: RunResult | None = None
        self.sub_title = f"{session.client.base_url} · {session.actor}"

    def on_mount(self) -> None:
        self.push_screen(CertificateScreen(self.session))
