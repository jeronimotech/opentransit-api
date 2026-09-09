# Deploying a city on Railway

This is how the Bogotá sandbox is deployed. The same steps work for any city and for the
production environment: only the environment token and the graph release change.

## Architecture (one Railway project, one environment per stage)

```
                 ┌──────────────┐  https  ┌──────────────┐
  browser/app ──▶│  web (Next)  │────────▶│  api (FastAPI)│──▶ postgres (PostGIS, volume)
                 └──────────────┘         │              │──▶ otp (OpenTripPlanner, volume)
                                          │  GTFS-RT /   │        ▲ downloads graph.obj from a
                                          │  GBFS pollers│        │ GitHub Release on first boot
                                          └──────────────┘
```

| service | source | image / build | port | volume |
|---|---|---|---|---|
| `postgres` | Docker image | `postgis/postgis:16-3.4` | 5432 (private only) | `/var/lib/postgresql/data` |
| `otp` | this repo | `deploy/otp/Dockerfile` (`RAILWAY_DOCKERFILE_PATH`) | 8080 (private only) | `/var/opentripplanner` |
| `api` | this repo | `Dockerfile` (root) + `railway.json` health check `/healthz` | 8000 (public domain) | – |
| `web` | `opentransit-web` repo | `Dockerfile` (root) | 3000 (public domain) | – |

Services talk over Railway's private network (`<service>.railway.internal`). Only `api` and `web`
get public domains. Nothing is built on Railway except the two app images: the OTP graph is built
locally (or in CI) and published as a release asset.

## 0. Prerequisites

- Railway CLI ≥ 5 (`brew install railway`), a **project token** per environment (Project → Settings →
  Tokens). Export it as `RAILWAY_TOKEN` for every command below. Never commit it.
- `gh` CLI logged in to the GitHub org that hosts this repo.
- A graph built for the city: `scripts/otp-native.sh build <city>` (macOS) or `scripts/build-graph.sh <city>`.
  The graph must be built with the **same OTP version** the `otp` image runs (`2.9.0`).

## 1. Publish the graph as a release

```bash
CITY=bogota; TAG=graph-$CITY-$(date +%F)
cd data/$CITY && shasum -a 256 graph.obj > SHA256SUMS
gh release create $TAG graph.obj build-config.json ../../otp/$CITY/router-config.json SHA256SUMS \
  --title "$CITY OTP graph $(date +%F)" --notes "OTP 2.9.0 graph for $CITY"
```
Note the asset URLs: `https://github.com/<org>/opentransit-api/releases/download/$TAG/{graph.obj,router-config.json}`.

## 2. Create the services

```bash
export RAILWAY_TOKEN=<project token for the environment>
railway add --service postgres --image postgis/postgis:16-3.4 \
  --variables POSTGRES_USER=opentransit --variables "POSTGRES_PASSWORD=$(openssl rand -hex 16)" \
  --variables POSTGRES_DB=opentransit --variables PGDATA=/var/lib/postgresql/data/pgdata
railway add --service otp
railway add --service api
railway add --service web
```
Volumes (`railway volume add -m <path>` needs the directory linked to the service, or use the dashboard):
`postgres` → `/var/lib/postgresql/data`, `otp` → `/var/opentripplanner`.

## 3. Variables

| service | variable | value |
|---|---|---|
| otp | `GRAPH_URL` | release asset URL of `graph.obj` |
| otp | `GRAPH_SHA256` | from `SHA256SUMS` (changing it forces a re-download) |
| otp | `ROUTER_CONFIG_URL` | release asset URL of `router-config.json` (re-fetched on every boot) |
| otp | `JAVA_OPTS` | `-Xmx5G -XX:+UseParallelGC` (Bogotá needs ≥ 4 GB heap; the plan must allow ≥ 6 GB) |
| otp | `PORT` | `8080` |
| otp | `RAILWAY_DOCKERFILE_PATH` | `deploy/otp/Dockerfile` |
| api | `DATABASE_URL` | `postgresql://opentransit:<POSTGRES_PASSWORD>@postgres.railway.internal:5432/opentransit` |
| api | `OTP_<CITY>_URL` | `http://otp.railway.internal:8080` (referenced from `cities/<city>.yaml`) |
| api | `ADMIN_TOKEN` | `openssl rand -hex 32` — the **machine credential** for CI and scripts. Set it only in Railway, never in the repo. `ADMIN_TOKEN_ENABLED=false` switches it off once nothing automated uses it |
| api | `ADMIN_BOOTSTRAP_EMAIL` / `ADMIN_BOOTSTRAP_PASSWORD` | optional: creates the first owner account on the next boot and then does nothing forever after (see §4b). Remove them once the account exists |
| api | `OIDC_REDIRECT_BASE` | optional, for Google/Microsoft sign-in: the **web** client's public origin, e.g. `https://bogota.opentransit.tech`. Falls back to `WEB_BASE_URL` (see §4c) |
| api | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | optional: enables the "Continue with Google" button. Both or neither |
| api | `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` / `MICROSOFT_TENANT` | optional: enables "Continue with Microsoft". `MICROSOFT_TENANT` is your Directory (tenant) ID; anything other than a GUID also needs `MICROSOFT_ALLOWED_TENANT_IDS` or the provider stays off (§4c) |
| api | `CORS_ORIGINS` | `https://<web public domain>` (use `*` only while testing) |
| api | `LOG_JSON` / `LOG_LEVEL` | `true` / `INFO` |
| web | `NEXT_PUBLIC_API_URL` | `https://<api public domain>` (build-time: redeploy web after changing it) |
| web | `NEXT_PUBLIC_DEFAULT_CITY` | `<city>` |
| web | `NEXT_PUBLIC_ADMIN_ENABLED` | `1` to expose `/admin` |
| web | `PORT` | `3000` |

```bash
railway variables --service otp --set GRAPH_URL=... --set GRAPH_SHA256=... --set ROUTER_CONFIG_URL=... \
  --set "JAVA_OPTS=-Xmx5G -XX:+UseParallelGC" --set PORT=8080 --set RAILWAY_DOCKERFILE_PATH=deploy/otp/Dockerfile
railway domain -s api -p 8000        # prints the public domain
railway domain -s web -p 3000
```

## 4. Deploy

```bash
# from a clean checkout of opentransit-api
railway up -s otp -e <env> -d -c
railway up -s api -e <env> -d -c
# from a clean checkout of opentransit-web
railway up -s web -e <env> -d -c
```
`otp` downloads the graph into its volume on first boot (≈ 1–2 min) and then loads it (≈ 1–2 min).
`api` creates the schema, starts the GTFS-RT and GBFS pollers immediately and ingests the static GTFS
in the background (≈ 118 MB download; a few minutes). `web` is a Next.js server build against the API URL;
it also serves `/api/admin/*`, the route handler that holds the operator session cookie.

## 4b. The first admin account

People sign in to `/admin` with a named account (email + password), so a fresh database needs one owner
before anybody can get in — and nobody can create it through the UI. Two ways, pick either:

```bash
# a) one-shot seed: set the variables, redeploy once, then remove them
railway variables --service api --set ADMIN_BOOTSTRAP_EMAIL=you@example.com \
  --set "ADMIN_BOOTSTRAP_PASSWORD=$(openssl rand -base64 18)"     # note it down, it is shown nowhere else
railway redeploy -s api
railway variables --service api --unset ADMIN_BOOTSTRAP_PASSWORD --unset ADMIN_BOOTSTRAP_EMAIL

# b) from a shell with DATABASE_URL pointing at the deployment
railway run -s api python scripts/admin_user.py create-owner you@example.com --name "You"
```

The seed refuses to do anything once any account exists, so leaving it set cannot re-create or reset an
owner. Afterwards the owner creates the rest from `/admin/users`: `viewer` (reads analytics), `admin`
(edits city config) and `owner` (manages accounts), each scoped to specific cities or to all of them.

`ADMIN_TOKEN` keeps working exactly as before as `X-Admin-Token` for CI, cron and `make ingest`; it never
manages accounts, and every use is logged. `NEXT_PUBLIC_API_URL` stays the browser-facing API URL; the web
service may also set `API_URL` to an internal hostname (`http://api.railway.internal:8000`) that the admin
proxy uses server-side.

## 4c. Sign in with Google / Microsoft (optional)

Operators can use a Google or Microsoft account instead of a password. Read this first, because it changes
nothing about *who* may sign in:

> **Signing in with a provider does not create an account.** It signs in an existing, enabled account whose
> email matches the verified address the provider returned. Somebody with no account gets `403` and a "ask an
> owner to invite you" message. Create the account first (§4b or `/admin/users`), then they can use the
> button. Full rules in `SECURITY.md`.

The redirect URI is built from `OIDC_REDIRECT_BASE` (or `WEB_BASE_URL`) and must be registered **exactly** in
each provider's console. For production at `https://bogota.opentransit.tech`:

| provider | redirect URI to register |
|---|---|
| Google | `https://bogota.opentransit.tech/admin/auth/callback/google` |
| Microsoft | `https://bogota.opentransit.tech/admin/auth/callback/microsoft` |

For a staging environment, register the staging origin as a second redirect URI on the same app (or a
separate app); for local development add `http://localhost:3000/admin/auth/callback/<provider>` and set
`OIDC_ALLOW_INSECURE_HTTP=true` on the API. A mismatched or unregistered URI fails at the provider with
`redirect_uri_mismatch` before anything reaches us.

**Google** — [Cloud console](https://console.cloud.google.com/apis/credentials) → *APIs & Services* →
*Credentials* → *Create credentials* → *OAuth client ID* → **Web application**. Add the redirect URI above
under *Authorised redirect URIs*. On the *OAuth consent screen*, `Internal` (Workspace) or `External` with
the operators added as test users; the scopes needed are only `openid`, `email` and `profile`. Copy the
client ID and secret:

```bash
railway variables --service api --set GOOGLE_CLIENT_ID=... --set GOOGLE_CLIENT_SECRET=...
railway variables --service api --set OIDC_REDIRECT_BASE=https://bogota.opentransit.tech
```

**Microsoft (Entra ID)** — [Entra admin center](https://entra.microsoft.com) → *App registrations* →
*New registration*. Supported account types: **Accounts in this organizational directory only** unless you
have a reason otherwise. Platform **Web**, redirect URI as above. Then *Certificates & secrets* → *New client
secret* (note the expiry — a rotated secret has to be updated here), and copy *Application (client) ID* and
*Directory (tenant) ID* from the Overview page.

```bash
railway variables --service api --set MICROSOFT_CLIENT_ID=... --set MICROSOFT_CLIENT_SECRET=... \
  --set MICROSOFT_TENANT=<Directory (tenant) ID>
```

`MICROSOFT_TENANT` as a tenant GUID is the safe setup: only that directory may sign in. If you deliberately
use `common`, `organizations`, `consumers` or a domain name, you **must** also set
`MICROSOFT_ALLOWED_TENANT_IDS` to the tenant GUIDs allowed in — otherwise Microsoft sign-in refuses to enable
at all, and the API logs why. Entra signs every tenant's tokens with the same keys, so without that list a
stranger's tenant could issue a perfectly valid token carrying one of your operators' email addresses.
Optionally add the `email` and `xms_edov` **optional claims** to the app registration (*Token configuration*
→ *Add optional claim* → *ID*): `email` guarantees the address is present, and `xms_edov` lets the API refuse
an address whose domain the tenant admin has not verified.

Neither provider needs anything on the `web` service. Restart `api` after setting the variables — it logs
`provider sign-in enabled: google, microsoft` — and the buttons appear on `/admin/login` on their own,
because the login screen draws only what `GET /v1/admin/auth/providers` reports. Nothing appears for a
provider that is not configured, and its endpoints answer `404`.

If a linked account ever stops matching (a directory account re-created, an app registration replaced), the
sign-in is refused as a mismatch by design. Clear the link deliberately:

```bash
railway run -s api python scripts/admin_user.py unlink you@example.com --provider microsoft
```

## 5. Verify

```bash
API=https://<api public domain>
curl -s $API/healthz
curl -s $API/v1/cities/<city>/health      # router.up, realtime.vehicles, rental.networks[].up
curl -s "$API/v1/cities/<city>/plan?fromLat=..&fromLon=..&toLat=..&toLon=.."
curl -sI https://<web public domain>/<city>
```

## 6. Updating the graph

Build a new graph, publish a new release (step 1), then set `GRAPH_URL` + `GRAPH_SHA256` on `otp`
(the checksum change triggers the re-download) and redeploy `otp`. `router-config.json` changes alone
only need `ROUTER_CONFIG_URL` to point at the new asset and a restart. Rebuild monthly: the graph is
bounded to −1 month / +6 months of transit service.

## 7. Promoting to production

Same steps with the **production** project token: create the four services in the `production`
environment (or duplicate the sandbox environment from the dashboard), attach the two volumes, set the
variables with a fresh `ADMIN_TOKEN` and `POSTGRES_PASSWORD`, create the production owner account (§4b),
point `NEXT_PUBLIC_API_URL` at the prod
API domain, tighten `CORS_ORIGINS` to the prod web domain, add custom domains if any, then `railway up`
each service. Keep the same graph release unless the feed changed.

## Operations notes

- Secrets live only in Railway variables (and your password manager). This repo never contains them.
- Admin passwords are stored as argon2id digests and session tokens as sha256 digests; neither is recoverable.
  An owner resets a forgotten password from `/admin/users`, or `scripts/admin_user.py passwd <email>` does it
  from a shell.
- The `otp` service needs a plan that allows ≥ 6 GB RAM per service for Bogotá-sized graphs.
- Health checks: `api` uses `/healthz` (in `railway.json`). `otp` has none because its boot takes
  minutes; the API reports `router.up` instead.
- Logs: `railway logs -s <service> -e <env>` (`-b` for build logs).
