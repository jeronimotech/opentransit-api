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

- The API is designed to run **without secrets in the repository**: `ADMIN_TOKEN` (admin endpoints) and
  `DATABASE_URL` come from the environment. Generate the token with `openssl rand -hex 32` and rotate it if leaked.
- Admin endpoints (`/v1/admin/*`) are the only mutating surface; everything else is read-only and public.
- The service fetches third-party feeds (GTFS, GTFS-RT, GBFS, Overpass, Photon). Feed contents are treated as
  untrusted data and validated before use, but a compromised feed URL can still serve wrong transit data.
- Run behind TLS (a reverse proxy or your platform's edge); the admin token travels in a header.
