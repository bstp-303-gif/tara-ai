# GPGD Deployment Progress

Last updated: 2026-07-31

## Status: live and publicly reachable over HTTPS

**https://34-124-176-218.sslip.io/** — verified working end-to-end (redirects to login, `200 OK`, valid Let's Encrypt cert, no domain purchase needed). `sslip.io` is a free service that maps `<ip-with-dashes>.sslip.io` to that IP, used here only because no real domain was available yet.

### 2026-07-31: added Caddy reverse proxy for HTTPS
- Added a `caddy` service to `docker-compose.yml` (image `caddy:2-alpine`, publishes 80/443, mounts `./Caddyfile` and two new named volumes for cert storage). `web` no longer publishes port 8000 to the host — only `caddy` is internet-facing now; it reverse-proxies to `web:8000` over the internal compose network.
- New `Caddyfile` at repo root: `34-124-176-218.sslip.io { reverse_proxy web:8000 }`. Caddy handles the ACME HTTP-01 challenge and cert renewal automatically.
- `mygpgd/settings.py`: added `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` when `DEBUG=False`, so Django trusts Caddy's forwarded proto header instead of redirect-looping.
- `.env.production`: added `34-124-176-218.sslip.io` to `DJANGO_ALLOWED_HOSTS`; `GPGD_SITE_URL` now `https://34-124-176-218.sslip.io`.
- `gcp/deploy.sh`: added `Caddyfile` to the list of files synced to the VM (it's a fixed file list, not a full-directory sync).
- To switch to a real domain later: point its DNS A record at `34.124.176.218`, add the domain to `Caddyfile` and `DJANGO_ALLOWED_HOSTS`, redeploy. sslip.io can stay as a fallback.

### 2026-07-31 deploy attempt and fix
The 2026-07-30 attempt (`logs/deploy-attempt-20260730b.log`) got as far as building the Docker image but the container crashed on boot with `SystemCheckError: agents.ActivityReport.photo_1/photo_2: Cannot use ImageField because Pillow is not installed` — `Pillow` was missing from `requirements.txt` despite the model using `ImageField`. Fixed by adding `Pillow==11.1.0` to `requirements.txt` and redeploying via `./gcp/deploy.sh`. Container is now `Up` and internally responsive.

## Former blocker (resolved 2026-07-30)

## Former blocker (resolved 2026-07-30)

Org policy `constraints/compute.vmExternalIpAccess` on org `710416391144` (inherited via folder `521228790524`) denies external IPs on Compute Engine VMs by default. It currently allows only one exception, for a different project:

```
projects/delima-3-dev-459201/zones/asia-southeast1-c/instances/canvas-lms-dev-20260128
```

It needs to also allow:

```
projects/prestij-jayanthi-gpgdselector/zones/asia-southeast1-b/instances/gpgd-app
```

**Who can fix this:** someone with `roles/orgpolicy.policyAdmin` at the **organization or folder** level (not project-level Owner/Editor — that was checked and does not include this permission). The account currently in use, `bstp-303@moe-dl.edu.my`, has `roles/owner` on the project only, which is confirmed insufficient (`org-policies set-policy` returns permission denied for it).

**Verify the fix landed** with:
```
gcloud resource-manager org-policies describe constraints/compute.vmExternalIpAccess \
  --project=prestij-jayanthi-gpgdselector --effective
```
It should list `.../instances/gpgd-app` in `allowedValues` alongside the canvas-lms entry (adding, not replacing).

Last checked 2026-07-27: still only the canvas-lms exception is present — the deploy was re-attempted and failed at the same step. Full log: `logs/deploy-attempt-20260727.log`.

## GCP environment

| Item | Value |
|---|---|
| Account | `bstp-303@moe-dl.edu.my` |
| Project | `prestij-jayanthi-gpgdselector` |
| Org / Folder | `710416391144` / `521228790524` |
| Region / Zone | `asia-southeast1` / `asia-southeast1-b` |
| VM name (planned) | `gpgd-app` (e2-medium, Debian 12, 30GB boot disk) |
| VPC / Subnet | `gpgd-vpc` / `gpgd-subnet` (10.10.0.0/24) — created |
| Static IP | `gpgd-app-ip` → `34.124.176.218` — reserved, unattached (small idle cost until VM exists) |
| Firewall | `gpgd-allow-http` (80), `gpgd-allow-https` (443) from anywhere; `gpgd-allow-ssh-iap` (22) restricted to Google's IAP range only — all created |

## What's already built and verified

### Django settings (`mygpgd/settings.py`)
- `DEBUG`, `SECRET_KEY`, `ALLOWED_HOSTS` all driven by env vars (`DJANGO_DEBUG`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`), safe defaults (DEBUG defaults False; refuses to boot with DEBUG=False and no secret key).
- HTTPS/security settings (`SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, HSTS) auto-enabled whenever `DEBUG=False`.
- `STATIC_ROOT` + whitenoise wired in; `collectstatic` verified working.
- `DATABASES.NAME` reads `DJANGO_DB_PATH` env var so SQLite can live on a mounted volume.
- Verified via `manage.py check --deploy` → 0 issues with prod-like env vars set.

### Docker
- `Dockerfile` — python:3.13-slim, non-root user, gunicorn via `entrypoint.sh` (runs migrate + collectstatic on boot).
- `docker-compose.yml` — named volumes for SQLite (`/app/data`), `media/`, `temp_uploads/` so data survives rebuilds.
- `.dockerignore`, `requirements.txt` (Django, python-dotenv, openpyxl, anthropic, pandas, whitenoise, gunicorn — all pinned).
- `.env.example` (local dev template) and `.env.production` (GCP-specific template, pre-filled with the static IP; `DJANGO_SECRET_KEY`/SMTP/Anthropic key still need real values before deploying).

### GCP scripts (all in `gcp/`)
- `setup.sh` — idempotent infra provisioning (network, subnet, firewall, static IP, VM). Currently the step that fails, at VM creation only.
- `startup-script.sh` — installs Docker + rsync on first VM boot.
- `deploy.sh` — pushes code + `.env.production` (renamed to `.env` remotely) via `gcloud compute scp`/`ssh` over IAP tunnel, runs `docker compose up --build -d`. Refuses to run if `.env.production` still has a blank secret key or `DJANGO_DEBUG=True`.
- `smoke-test.sh` — post-deploy health check (root/login/admin pages load, static files served, no debug/traceback leakage, optional SSH container-health check). Verified working against a live local server (5/5 pass) and against the unreachable static IP (fails cleanly, no crash).
- `release.sh` — full pipeline: setup → backup current release → deploy → smoke test → **auto-rollback to the previous release if the smoke test fails**, re-verified healthy after rollback. This is the one command to run for a real deploy.

## Not yet done

- **Custom domain + real HTTPS**: no reverse proxy exists yet. Currently gunicorn serves plain HTTP directly on 8000; firewall allows 80/443 but nothing listens there yet. Plan (agreed, not yet built): add a **Caddy** reverse-proxy container for automatic Let's Encrypt TLS, forward to gunicorn internally, and set `SECURE_PROXY_SSL_HEADER` in settings.py so Django trusts Caddy's `X-Forwarded-Proto`. **Still waiting on the actual domain name from you** to finish this.
- Rotate the Anthropic API key and Gmail app password that were sitting in plaintext in the original local `.env` (flagged early on, never actioned).
- First real deploy — blocked on the org policy exception above.

## Next steps (in order)

1. Get the org policy exception actually applied (see "Current blocker") and verify with the command above.
2. Run `./gcp/release.sh` from the project root.
3. Once it's live on the static IP, provide the custom domain name so the Caddy/HTTPS reverse-proxy setup can be finished and DNS instructions given.
4. Rotate the two leaked secrets mentioned above.
