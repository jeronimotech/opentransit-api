# Security policy

## Supported versions

`main` is the only supported line. Deployments should track the latest commit on `main` (or the latest
tagged release once releases start); older commits do not receive fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

- Preferred: open a private report through GitHub Security Advisories:
  https://github.com/jeronimotech/opentransit-api/security/advisories/new
- If you cannot use GitHub, email the maintainers (see `CODEOWNERS`) with "opentransit security" in the subject.

You will get an acknowledgement within 5 working days and a fix or mitigation plan within 30 days for
confirmed issues. We credit reporters in the changelog unless they prefer otherwise.

## Scope and hardening notes

- The API is designed to run **without secrets in the repository**: `ADMIN_TOKEN` (the machine credential)
  and `DATABASE_URL` come from the environment. Generate the token with `openssl rand -hex 32` and rotate it
  if leaked.
- People reach `/v1/admin/*` with a **named account**: an email and a password, hashed with argon2id, and a
  session whose token is stored only as a sha256 digest. Sessions expire (`ADMIN_SESSION_HOURS`) and are
  revoked when the account is disabled or its role, city scope or password changes. Passwords and session
  tokens are never logged and never returned; a token is shown once, at sign-in.
- Roles are `viewer` (reads), `admin` (edits city config) and `owner` (also manages accounts), each scoped to
  specific cities or to all of them. Both axes are enforced on every admin route, not only in the UI.
- `ADMIN_TOKEN` remains as an explicitly-labelled machine credential for CI and scripts. Every use is logged;
  it never manages accounts; set `ADMIN_TOKEN_ENABLED=false` to switch it off entirely.
- Admin endpoints (`/v1/admin/*`) are the only mutating surface; everything else is read-only and public.
- The service fetches third-party feeds (GTFS, GTFS-RT, GBFS, Overpass, Photon). Feed contents are treated as
  untrusted data and validated before use, but a compromised feed URL can still serve wrong transit data.
- Run behind TLS (a reverse proxy or your platform's edge); credentials travel in headers, and the web
  client's session cookie is `HttpOnly; Secure; SameSite=Lax`.
