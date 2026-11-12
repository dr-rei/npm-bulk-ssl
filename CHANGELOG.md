# Changelog

## 1.0.0 — 2026-11-12

- Interactive Textual UI: single certificate picker, searchable/sortable multi-select host table
  (select all / select shown / deselect shown / invert), optional SSL option overrides, review screen
  with certificate-coverage and expiry warnings, dry run, per-host progress and cancel.
- Proxy, redirection and 404 hosts.
- Headless `apply`, `rollback`, `list-certs`, `list-hosts` commands with stable exit codes.
- Backup before every run, one-command rollback, append-only JSONL audit log.
- NPM 2FA login, HTTPS with custom CA bundle or `--insecure`, retries on transient errors.
