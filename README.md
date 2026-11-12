# npm-bulk-ssl

Bulk-change the SSL certificate of many [Nginx Proxy Manager](https://nginxproxymanager.com/) hosts at once, instead of
editing them one by one in the web UI.

- **Interactive terminal UI** ([Textual](https://textual.textualize.io/)): pick one certificate, then search, sort and
  multi-select hosts. You can select all, select or deselect everything currently shown (filtered), or invert the shown rows.
- Covers **proxy hosts, redirection hosts and 404 hosts**.
- **Only the certificate changes by default.** Each host keeps its own Force SSL / HTTP/2 / HSTS settings unless you choose
  to override them for the whole batch.
- **Safety:** a review screen that flags hostnames the certificate doesn't cover, a dry-run mode, a backup file written
  before every run, a one-command rollback, and an append-only audit log.
- **Headless mode** for scripts and CI (`apply`, `rollback`, `list-certs`, `list-hosts`).
- Supports NPM accounts with **2FA** and HTTPS admin endpoints (custom CA bundle or `--insecure`).
- Tested against a real NPM **2.16** container.

![Host selection](docs/2-hosts.svg)

## Install and run on each OS

You don't install it on the NPM server. It runs on any machine that can reach the NPM admin port (usually `81`): your
laptop, a jump host, or the NPM server itself over SSH. It talks to NPM over its REST API only.

The recommended installer is [uv](https://docs.astral.sh/uv/). It installs the tool in its own isolated environment and
**downloads a suitable Python by itself**, so you don't need Python or git installed first. [pipx](https://pipx.pypa.io/)
works too if you already use it (that route needs Python 3.10+).

The release file used below is
`https://github.com/dr-rei/npm-bulk-ssl/releases/download/v1.0.0/npm_bulk_ssl-1.0.0-py3-none-any.whl`. Newer versions are
listed on the [Releases page](https://github.com/dr-rei/npm-bulk-ssl/releases).

### Windows 10 / 11

Open **PowerShell**, preferably in [Windows Terminal](https://aka.ms/terminal) for the best colours and mouse support.

1. Install uv, using either winget or the official script:

   ```powershell
   winget install --id=astral-sh.uv -e
   ```

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

2. Close and reopen the terminal, then install the tool:

   ```powershell
   uv tool install https://github.com/dr-rei/npm-bulk-ssl/releases/download/v1.0.0/npm_bulk_ssl-1.0.0-py3-none-any.whl
   ```

3. If `npm-bulk-ssl` is "not recognized", run `uv tool update-shell` and reopen the terminal.
4. Run it:

   ```powershell
   npm-bulk-ssl
   ```

Backups and the audit log go to `%USERPROFILE%\.npm-bulk-ssl\`. In PowerShell, environment variables for headless use are
set like `$env:NPM_URL = "http://10.0.0.5:81"`.

### macOS (Intel and Apple Silicon)

1. Install uv, using either Homebrew or the official script:

   ```bash
   brew install uv
   ```

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Install the tool:

   ```bash
   uv tool install https://github.com/dr-rei/npm-bulk-ssl/releases/download/v1.0.0/npm_bulk_ssl-1.0.0-py3-none-any.whl
   ```

3. If the command isn't found, run `uv tool update-shell` and open a new terminal tab.
4. Run `npm-bulk-ssl`.

Terminal.app works. iTerm2, Ghostty and WezTerm give smoother rendering. Backups go to `~/.npm-bulk-ssl/`.

### Linux (Debian/Ubuntu, RHEL/Rocky/Alma, Fedora, Arch, Alpine…)

1. Install uv. You need `curl`, which minimal images may lack: `sudo apt install curl` or `sudo dnf install curl`.

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Install the tool:

   ```bash
   uv tool install https://github.com/dr-rei/npm-bulk-ssl/releases/download/v1.0.0/npm_bulk_ssl-1.0.0-py3-none-any.whl
   ```

3. If the command isn't found, run `uv tool update-shell` or add `~/.local/bin` to your `PATH`, then open a new shell.
4. Run `npm-bulk-ssl`.

To use pipx instead: on Debian/Ubuntu, `sudo apt install pipx && pipx ensurepath`. On Fedora/RHEL, `sudo dnf install pipx`.
Then install with `pipx install <the .whl URL above>`.

**Running on the NPM server over SSH:** the interface works fine over SSH (`ssh user@npm-host`, then `npm-bulk-ssl`).
Use `127.0.0.1:81` as the NPM address. If NPM runs in Docker with port 81 published, that address reaches it. Mouse clicks
work in most SSH clients, and every action also has a keyboard shortcut.

### Upgrade, pin, uninstall (all OSes)

```bash
uv tool install --force https://github.com/dr-rei/npm-bulk-ssl/releases/download/vX.Y.Z/npm_bulk_ssl-X.Y.Z-py3-none-any.whl
```

```bash
uv tool install git+https://github.com/dr-rei/npm-bulk-ssl@v1.0.0
```

```bash
uv tool uninstall npm-bulk-ssl
```

The first command upgrades or pins a release. The second installs straight from a git tag, which needs git. The third
uninstalls; it leaves your backups in `~/.npm-bulk-ssl`.

To try it once without installing anything permanently:
`uvx --from https://github.com/dr-rei/npm-bulk-ssl/releases/download/v1.0.0/npm_bulk_ssl-1.0.0-py3-none-any.whl npm-bulk-ssl`.

### Offline / air-gapped machines

On a machine with internet and the **same OS and CPU type** as the target, download the tool and its dependencies:

```bash
python -m pip download --dest npm-bulk-ssl-offline https://github.com/dr-rei/npm-bulk-ssl/releases/download/v1.0.0/npm_bulk_ssl-1.0.0-py3-none-any.whl
```

Copy the folder to the target, which needs Python 3.10+, and install from it:

```bash
python -m pip install --user --no-index --find-links npm-bulk-ssl-offline npm-bulk-ssl
```

### First run checklist

1. Make sure this machine can reach the admin port: open `http://<npm-ip>:81` in a browser from it.
2. Run `npm-bulk-ssl` and enter the address (`10.0.0.5` → `http://10.0.0.5:81`; use `https://…` if your admin UI is behind TLS).
   For an internal CA add `--ca-bundle ca.pem`. For a self-signed certificate add `--insecure`.
3. On the review screen, tick **Dry run** and apply once. Nothing changes, but you see the full plan, and a backup and
   audit entry are written.
4. Go back (`Esc`), untick dry run, apply for real, and keep the printed `rollback` command.

### Automating it (cron / Task Scheduler / CI)

Use the headless `apply` command with a **dedicated NPM user** that has only the permissions this needs. Pass the password
through an environment variable from your secret store, never on the command line.

Linux/macOS cron. Keep the variables in a file only you can read, for example `chmod 600 ~/.npm-bulk-ssl.env`:

```bash
# ~/.npm-bulk-ssl.env
NPM_URL=http://10.0.0.5:81
NPM_EMAIL=automation@example.com
NPM_PASSWORD=change-me
```

```bash
set -a; . ~/.npm-bulk-ssl.env; set +a; npm-bulk-ssl apply --cert-id 7 --match '*.example.com' --current-cert 3 --yes
```

Windows Task Scheduler: create a task that runs `powershell.exe` with a script that sets `$env:NPM_URL`, `$env:NPM_EMAIL`
and `$env:NPM_PASSWORD` (read the password from Windows Credential Manager or your vault), then calls `npm-bulk-ssl apply … --yes`.

In CI (GitHub Actions, GitLab…), store `NPM_PASSWORD` as a masked secret, install with `uv tool install <whl URL>` and run
`npm-bulk-ssl apply … --yes`. Branch on the exit codes listed below. An account with 2FA needs `NPM_TOTP` for every run,
so use a non-2FA service account for automation.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Cannot reach NPM at …` | Wrong address or port, or a firewall is blocking it. Check the admin UI opens in a browser from this machine |
| `certificate verify failed` | HTTPS admin endpoint with an internal or self-signed certificate: use `--ca-bundle ca.pem` or `--insecure` |
| `Invalid email or password` / `Invalid verification code` | Check the credentials. 2FA codes expire every 30 s |
| `refusing to change hosts without confirmation` | Headless with no terminal: add `--yes`, or `--dry-run` to preview |
| `the interactive UI needs a terminal` | Output is piped or redirected. Run it in a real terminal, or use `apply` |
| Garbled boxes or colours on Windows | Use Windows Terminal instead of the legacy console |
| Some hosts `FAILED` | Others still applied. Fix the cause shown, then rerun: hosts already on the new certificate are skipped |

## Interactive use

```bash
npm-bulk-ssl
```

1. **Login.** You're asked for the NPM address (`10.0.0.5` → `http://10.0.0.5:81`, or a full URL such as
   `https://npm.example.com`), your email and password (hidden), and a 2FA code if the account has one.
2. **Certificate.** Choose exactly one certificate. The list shows its domains, expiry and how many hosts use it now.
3. **Hosts.** A sortable, searchable, multi-select table. The *Cert covers* column says whether the chosen certificate is
   valid for that host. Type `NO` in the search box to find the hosts it doesn't cover.
4. **SSL options.** Leave each option on *Keep current*, or force it On or Off for every selected host.
5. **Review.** See every change and any warnings, optionally tick *Dry run*, then apply. Progress is shown per host, and a
   failing host doesn't stop the run. Press `x` to cancel after the current host.

![Review screen](docs/4-confirm.svg)

### Keys in the host table

| Key | Action |
| --- | --- |
| `/` | Focus search (every column is searched, space-separated terms are ANDed). `Enter`/`Esc` return to the table |
| `Space` / `Enter` / click | Toggle the row under the cursor |
| `a` / `n` / `i` | Select / deselect / invert the rows **currently shown** (respects the search) |
| `A` / `N` | Select / clear **all** hosts, ignoring the search |
| click header, `s`, `r` | Sort by a column (click again to reverse), cycle the sort column, reverse the sort |
| `c` | Continue |
| `Esc` | Back |

## Headless use

Connection settings come from flags or environment variables. The password can only come from `NPM_PASSWORD` or the
prompt, never from a flag, so it doesn't end up in shell history or the process list.

| Variable | Meaning |
| --- | --- |
| `NPM_URL` | NPM admin URL (`--url`) |
| `NPM_EMAIL` | Login email (`--email`) |
| `NPM_PASSWORD` | Password |
| `NPM_TOTP` | Current 2FA code (only if the account uses 2FA) |
| `NPM_BULK_SSL_HOME` | Where backups and the audit log go (`--state-dir`, default `~/.npm-bulk-ssl`) |

```bash
npm-bulk-ssl list-certs
npm-bulk-ssl list-hosts --json

# Preview: every host on *.example.com that currently uses certificate 3 -> certificate 7
npm-bulk-ssl apply --cert-id 7 --match '*.example.com' --current-cert 3 --dry-run

# Do it, and force HTTPS on them too
npm-bulk-ssl apply --cert-id 7 --match '*.example.com' --current-cert 3 --force-ssl on --yes
```

Selectors are combined with AND: `--match` / `--exclude` (globs on any domain name, repeatable), `--current-cert ID`
(`0` = no certificate), `--host proxy:12`, `--kind proxy|redirect|dead`, or `--all`. `apply` refuses to run if the
certificate doesn't cover every selected hostname, unless you pass `--allow-mismatch`. Without `--yes` it asks for
confirmation, or refuses when there is no terminal.

Exit codes: `0` success, `1` some hosts failed or were skipped, `2` usage error, `3` NPM/login/connection error,
`130` aborted.

## Backups, rollback and audit

Every run, dry runs included, writes `~/.npm-bulk-ssl/runs/<timestamp>-<action>/backup.json` **before the first change**.
The file holds each host's previous certificate and SSL flags. `results.json` records the outcome per host.

```bash
npm-bulk-ssl rollback ~/.npm-bulk-ssl/runs/20261112T021000Z-apply/backup.json --dry-run
npm-bulk-ssl rollback ~/.npm-bulk-ssl/runs/20261112T021000Z-apply/backup.json
```

Rollback restores only what differs, reports hosts that were deleted since the backup, and writes its own backup, so a
rollback can also be undone.

`~/.npm-bulk-ssl/audit.jsonl` is an append-only JSON Lines trail: run start and end, and every host with its before and
after values, status and the actor's email. Credentials and tokens are never written to disk. The token lives in memory
only.

## How it works

The tool uses NPM's own REST API, the same one the web UI uses. It sends one partial `PUT` per host containing only the
changed fields (`certificate_id` and any overridden flags), so forwarding, locations, access lists and advanced config are
not touched. After each update it reads back the response and fails the host if NPM did not apply the change. Hosts are
updated one at a time, because NPM regenerates and reloads nginx on every change. Transient errors (connection drops
during those reloads, 502/503/504) are retried with backoff.

```
src/npm_bulk_ssl/
  models.py   Host / Certificate models, wildcard coverage check
  client.py   NPM REST client (login + 2FA, retries, partial updates)
  ops.py      selection, planning, execution with backup/audit, rollback (no UI code)
  tui.py      Textual screens: certificate -> hosts -> options -> review -> progress
  cli.py      argument parsing, prompts, headless commands, exit codes
```

## Development

```bash
uv venv && uv pip install -e ".[dev]"
ruff check . && mypy src && pytest
```

End-to-end test against a **disposable** NPM. It creates certificates and hosts, so never point it at production:

```bash
docker run -d --name npm-e2e -p 18081:81 -v npm-e2e-data:/data -v npm-e2e-le:/etc/letsencrypt \
  -e INITIAL_ADMIN_EMAIL=admin@example.com -e INITIAL_ADMIN_PASSWORD=changeme123 jc21/nginx-proxy-manager:latest
NPM_E2E_URL=http://127.0.0.1:18081 pytest tests/test_e2e_real_npm.py
```

CI runs lint, type checks and unit tests on Linux, Windows and macOS (Python 3.10 and 3.13), plus the end-to-end test
against the latest NPM image.

## License

MIT
