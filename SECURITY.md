# Security

- Passwords are read from a hidden prompt or `NPM_PASSWORD` only — never from a command-line flag — and are not stored.
- The NPM bearer token is kept in memory for the duration of the run and never written to disk or logs.
- Backups and the audit log contain host names and SSL settings, not secrets. They are written to `~/.npm-bulk-ssl`
  (or `NPM_BULK_SSL_HOME`); protect that directory like any other admin log.
- Prefer HTTPS to the NPM admin endpoint; use `--ca-bundle` for an internal CA instead of `--insecure`.
- Use an NPM user with only the permissions needed (manage proxy/redirection/404 hosts, view certificates).

Report vulnerabilities privately via GitHub Security Advisories on this repository.
