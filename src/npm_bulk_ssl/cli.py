"""Command-line entry point: interactive TUI (default) and headless commands."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__
from .client import NpmClient, NpmError
from .models import OPTION_FIELDS, Host, HostKind
from .ops import (
    HostResult,
    Plan,
    RunResult,
    build_plan,
    build_rollback_plan,
    describe_payload,
    execute,
    load_backup,
    select_hosts,
    state_dir,
)

EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, EXIT_NPM, EXIT_ABORT = 0, 1, 2, 3, 130
KIND_NAMES = {k.value: k for k in HostKind}
console = Console(stderr=True, highlight=False)
out = Console(highlight=False)


class UsageError(Exception):
    pass


def _interactive() -> bool:
    return sys.stdin.isatty()


def _ask(label: str, value: str | None, *, secret: bool = False) -> str:
    if value:
        return value
    if not _interactive():
        raise UsageError(f"{label} is required (set it via flag or environment variable)")
    while True:
        answer = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ")
        if answer.strip():
            return answer.strip() if not secret else answer


def _tri(value: str) -> bool | None:
    return None if value == "keep" else value == "on"


def _common(sub: bool) -> argparse.ArgumentParser:
    """Connection options, accepted before or after the command.

    Subcommand copies default to SUPPRESS so they never overwrite a value given before the command.
    """
    d = (lambda v: argparse.SUPPRESS) if sub else (lambda v: v)
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("connection")
    g.add_argument("--url", default=d(os.environ.get("NPM_URL")), help="NPM admin URL, e.g. http://10.0.0.5:81 (env NPM_URL)")
    g.add_argument(
        "--email", default=d(os.environ.get("NPM_EMAIL")), help="NPM login email (env NPM_EMAIL); password via prompt or NPM_PASSWORD"
    )
    g.add_argument("--insecure", action="store_true", default=d(False), help="skip TLS certificate verification (self-signed NPM)")
    g.add_argument("--ca-bundle", default=d(None), help="CA bundle file to verify the NPM HTTPS certificate")
    g.add_argument("--timeout", type=float, default=d(30.0), help="per-request timeout in seconds (default 30)")
    g.add_argument(
        "--kind",
        action="append",
        choices=sorted(KIND_NAMES),
        default=d(None),
        help="host type(s) to load: proxy, redirect, dead (default: all; repeatable)",
    )
    g.add_argument(
        "--state-dir", default=d(None), help="where backups and audit.jsonl are written (default ~/.npm-bulk-ssl, env NPM_BULK_SSL_HOME)"
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common(sub=True)
    p = argparse.ArgumentParser(
        prog="npm-bulk-ssl",
        description="Bulk-change the SSL certificate of Nginx Proxy Manager hosts.",
        parents=[_common(sub=False)],
    )
    p.set_defaults(dry_run=False)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command")

    t = sub.add_parser("tui", parents=[common], help="interactive picker (default when no command is given)")
    t.add_argument("--dry-run", action="store_true", help="start with 'dry run' ticked on the confirm screen")

    lc = sub.add_parser("list-certs", parents=[common], help="list certificates")
    lc.add_argument("--json", action="store_true")
    lh = sub.add_parser("list-hosts", parents=[common], help="list hosts and their current certificate")
    lh.add_argument("--json", action="store_true")

    a = sub.add_parser("apply", parents=[common], help="headless: apply a certificate to matching hosts")
    a.add_argument("--cert-id", type=int, required=True, help="certificate ID to apply (see list-certs)")
    sel = a.add_argument_group("host selection (combined with AND; at least one required)")
    sel.add_argument("--match", action="append", default=[], help="glob on any domain, e.g. '*.example.com' (repeatable, OR)")
    sel.add_argument("--exclude", action="append", default=[], help="glob on any domain to skip (repeatable)")
    sel.add_argument("--current-cert", action="append", type=int, help="only hosts currently on this cert ID (0 = none; repeatable)")
    sel.add_argument("--host", action="append", default=[], help="explicit host KIND:ID, e.g. proxy:12 (repeatable)")
    sel.add_argument("--all", action="store_true", help="select every loaded host")
    opts = a.add_argument_group("SSL option overrides (default keep)")
    for f, flag in zip(OPTION_FIELDS, ("--force-ssl", "--http2", "--hsts", "--hsts-subdomains"), strict=True):
        opts.add_argument(flag, dest=f, choices=("keep", "on", "off"), default="keep")
    a.add_argument("--allow-mismatch", action="store_true", help="proceed even if the cert does not cover some hostnames")
    a.add_argument("--dry-run", action="store_true", help="show and record what would change, change nothing")
    a.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")

    r = sub.add_parser("rollback", parents=[common], help="restore hosts from a backup.json written by a previous run")
    r.add_argument("backup", type=Path)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--yes", "-y", action="store_true")
    return p


def connect(args: argparse.Namespace) -> tuple[NpmClient, str]:
    url = _ask("NPM URL (e.g. 10.0.0.5:81 or https://npm.example.com)", args.url)
    email = _ask("NPM email", args.email)
    password = os.environ.get("NPM_PASSWORD") or _ask("NPM password", None, secret=True)
    verify: bool | str = args.ca_bundle or (not args.insecure)
    client = NpmClient(url, verify=verify, timeout=args.timeout)

    def totp() -> str:
        return os.environ.get("NPM_TOTP") or _ask("2FA code", None)

    console.print(f"Connecting to [b]{client.base_url}[/b] ...")
    try:
        client.login(email, password, totp)
    except BaseException:
        client.close()
        raise
    return client, email


def load_hosts(client: NpmClient, args: argparse.Namespace) -> list[Host]:
    kinds = tuple(KIND_NAMES[k] for k in args.kind) if args.kind else tuple(HostKind)
    return client.hosts(kinds)


def print_plan(plan: Plan, names: dict[int, str]) -> None:
    table = Table(title=f"{plan.action}: {len(plan.changes)} host(s) to change, {len(plan.unchanged)} already up to date")
    for col in ("Host", "Domains", "Current cert", "Changes", "Warning"):
        table.add_column(col)
    for c in plan.changes:
        table.add_row(
            c.host.key,
            ", ".join(c.host.domain_names),
            names.get(c.host.certificate_id, str(c.host.certificate_id)),
            describe_payload(c.payload, names),
            ("not covered: " + ", ".join(c.uncovered)) if c.uncovered else "",
        )
    console.print(table)
    for m in plan.missing:
        console.print(f"[yellow]missing (deleted since backup):[/yellow] {m}")


def confirm(plan: Plan, args: argparse.Namespace) -> bool:
    if args.yes or args.dry_run:
        return True
    if not _interactive():
        raise UsageError("refusing to change hosts without confirmation; pass --yes (or --dry-run)")
    return input(f"Apply to {len(plan.changes)} host(s)? Type 'yes' to continue: ").strip().lower() == "yes"


def report(result: RunResult) -> int:
    for r in result.failed:
        console.print(f"[red]FAILED[/red] {r.host.key} {', '.join(r.host.domain_names)}: {r.error}")
    console.print(
        f"ok={len(result.succeeded)} failed={len(result.failed)} skipped={len(result.skipped)}"
        f"{' (dry run)' if result.dry_run else ''}\nbackup: {result.run_dir / 'backup.json'}"
    )
    if result.succeeded:
        console.print(f'undo:   npm-bulk-ssl rollback "{result.run_dir / "backup.json"}"', soft_wrap=True, markup=False)
    return EXIT_PARTIAL if result.failed or result.skipped else EXIT_OK


def run(args: argparse.Namespace) -> int:
    command = args.command or "tui"
    home = state_dir(args.state_dir)
    if command == "rollback":
        backup = load_backup(args.backup)  # validate before prompting for credentials
    client, actor = connect(args)
    with client:
        certs = client.certificates()
        names = {c.id: c.nice_name for c in certs} | {0: "none"}

        if command == "list-certs":
            if args.json:
                out.print_json(
                    data=[
                        {
                            "id": c.id,
                            "nice_name": c.nice_name,
                            "domain_names": c.domain_names,
                            "provider": c.provider,
                            "expires_on": c.expires_on,
                        }
                        for c in certs
                    ],
                    default=str,
                )
            else:
                table = Table("ID", "Name", "Domains", "Provider", "Expires")
                for c in certs:
                    table.add_row(str(c.id), c.nice_name, ", ".join(c.domain_names), c.provider, str(c.expires_on or "-"))
                out.print(table)
            return EXIT_OK

        hosts = load_hosts(client, args)
        console.print(f"Loaded {len(certs)} certificate(s) and {len(hosts)} host(s).")

        if command == "list-hosts":
            if args.json:
                out.print_json(
                    data=[{"key": h.key, "domain_names": h.domain_names, "target": h.target, "enabled": h.enabled, **h.ssl} for h in hosts]
                )
            else:
                table = Table("Host", "Domains", "Target", "Certificate", "Enabled", "Flags")
                for h in hosts:
                    table.add_row(
                        h.key,
                        ", ".join(h.domain_names),
                        h.target,
                        names.get(h.certificate_id, f"#{h.certificate_id}"),
                        "yes" if h.enabled else "no",
                        h.flags_text(),
                    )
                out.print(table)
            return EXIT_OK

        if command == "tui":
            if not certs:
                raise UsageError("NPM has no certificates to apply")
            if not sys.stdout.isatty():
                raise UsageError("the interactive UI needs a terminal; use the 'apply' command for automation")
            from .tui import BulkSslApp, Session  # imported lazily: headless use never loads Textual

            session = Session(client, actor, certs, hosts, dry_run=args.dry_run, home=home)
            result = BulkSslApp(session).run()
            if result is None:
                console.print("No changes applied.")
                return EXIT_OK
            return report(result)

        if command == "apply":
            cert = next((c for c in certs if c.id == args.cert_id), None)
            if cert is None:
                raise UsageError(f"certificate #{args.cert_id} not found (see list-certs)")
            if not (args.match or args.current_cert is not None or args.host or args.all):
                raise UsageError("select hosts with --match, --current-cert, --host or --all")
            chosen = select_hosts(hosts, match=args.match, exclude=args.exclude, current_cert=args.current_cert, keys=args.host)
            overrides = {f: _tri(getattr(args, f)) for f in OPTION_FIELDS}
            plan = build_plan(cert, chosen, {k: v for k, v in overrides.items() if v is not None})
            if cert.is_expired():
                console.print(f"[bold red]WARNING: certificate #{cert.id} is expired[/bold red]")
        else:  # rollback
            plan = build_rollback_plan(backup, hosts)

        print_plan(plan, names)
        if command == "apply" and plan.mismatched and not args.allow_mismatch:
            raise UsageError(f"certificate does not cover {len(plan.mismatched)} host(s); review or pass --allow-mismatch")
        if not plan.changes:
            console.print("Nothing to change.")
            return EXIT_OK
        if not confirm(plan, args):
            console.print("Aborted; nothing changed.")
            return EXIT_ABORT

        def progress(i: int, n: int, r: HostResult) -> None:
            console.print(f"[{i}/{n}] {r.status:7} {r.host.key} {', '.join(r.host.domain_names)} {r.error}", markup=False)

        result = execute(client, plan, actor=actor, dry_run=args.dry_run, home=home, progress=progress)
        return report(result)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except UsageError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return EXIT_USAGE
    except (ValueError, OSError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        return EXIT_USAGE
    except NpmError as exc:
        console.print(f"[red]NPM error:[/red] {exc}")
        return EXIT_NPM
    except (KeyboardInterrupt, EOFError):
        console.print("\nInterrupted.")
        return EXIT_ABORT


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
