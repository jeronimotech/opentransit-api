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

### Signing in with Google or Microsoft

Optional, off unless configured, and deliberately narrow.

- **A provider proves who you are; it never decides whether you may in.** A successful Google or Microsoft
  sign-in does **not** create an account. It looks up an existing, **enabled** `admin_user` by the verified
  email and signs that person in; if there is no such account the answer is `403`. An owner invites people
  first. This is the whole security posture: auto-provisioning "whoever can prove they own an address at
  some domain" is how admin panels get taken over.
- **The identity is linked to a subject id, not to an email string.** The first successful sign-in stores the
  provider's stable subject (`admin_identity`: Google's `sub`, Microsoft's `oid`). Every later sign-in must
  present the same subject for that account, and the same account for that subject; either mismatch is a
  `403`, never a silent re-link. So somebody who later acquires the address — a recycled corporate mailbox,
  a re-created directory account — does not inherit the admin account with it. Recover deliberately with
  `python scripts/admin_user.py unlink <email> --provider google`.
- **The id_token is verified, not read.** RS256 signature against the provider's published JWKS (`alg: none`
  and symmetric algorithms are refused before a key is even fetched), issuer, audience (`aud` = our client
  id), `exp`/`iat`/`nbf` with 60 s of leeway, and the `nonce` from the request that started the flow.
- **Email verification is required, per provider's own rules.** Google must send `email_verified: true`.
  Microsoft's `email` claim is documented as mutable and *not* guaranteed correct, so Microsoft sign-in is
  additionally restricted to an explicit allowlist of tenant ids (`MICROSOFT_TENANT` as a GUID, or
  `MICROSOFT_ALLOWED_TENANT_IDS`) and refuses a token whose `xms_edov` claim is present and false. Microsoft
  sign-in **will not enable at all** without that allowlist, because Entra signs every tenant with the same
  keys: a valid signature says nothing about *which* organisation issued the token.
- **`state` and the PKCE verifier are single-use, short-lived and browser-bound.** They live server-side in
  `admin_oidc_state`, are consumed with a `DELETE … RETURNING` (so a replay finds nothing), expire in
  `OIDC_STATE_TTL_SECONDS` (default 10 min), and require a second secret held only in an httpOnly cookie on
  the web origin — a stolen `state` alone cannot finish a sign-in.
- **The `redirect_uri` is computed from `OIDC_REDIRECT_BASE`, never accepted from the caller**, must be
  `https` outside development, and the web only redirects afterwards to a path inside its own `/admin`.
- **The resulting session is an ordinary `admin_session`** — same roles, same city scope, same expiry, same
  revocation, same httpOnly-cookie proxy. Nothing downstream can tell how you signed in.
- Client ids and secrets come from the environment per deployment and are never committed. Neither a secret,
  an authorization code, an id_token nor a session token is ever logged.

**`OIDC_AUTO_PROVISION_DOMAINS` is off by default and should stay off.** Setting it means anybody who can
obtain a verified address at one of those domains gets an admin account created for them on first sign-in —
you are delegating the "who is an operator here" decision to whoever administers that domain's mailboxes.
Only consider it for a corporate directory whose account lifecycle you control, keep
`OIDC_AUTO_PROVISION_ROLE=viewer`, scope it with `OIDC_AUTO_PROVISION_CITIES`, and expect the accounts it
creates to appear in `/admin/users` like any other. The API logs a warning at start-up whenever it is set.
- Admin endpoints (`/v1/admin/*`) are the only mutating surface; everything else is read-only and public.
- The service fetches third-party feeds (GTFS, GTFS-RT, GBFS, Overpass, Photon). Feed contents are treated as
  untrusted data and validated before use, but a compromised feed URL can still serve wrong transit data.
- Run behind TLS (a reverse proxy or your platform's edge); credentials travel in headers, and the web
  client's session cookie is `HttpOnly; Secure; SameSite=Lax`.
