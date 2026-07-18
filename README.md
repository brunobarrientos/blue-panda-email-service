# blue-panda-email-service

> Last revised: 2026-07-11

Standalone FastAPI service that sends email on behalf of `focusedbluepanda@gmail.com`.

Repo: https://github.com/brunobarrientos/blue-panda-email-service  
Runtime path on Hetzner: `/home/hetzner/AI/blue-panda-email-service`  
Internal Python package: `gmail_service` (kept for import compatibility).

## Canonical project

Use Google Cloud project **`blue-panda-email-service`** with the Desktop OAuth client named **`blue-panda-email-service-desktop`**.

This is distinct from:

- `bbfn-500809` — used for agent reads/orchestration (Sheets, Drive, etc.).
- `brunocode-2026` — used by the DTC Cloud Access Broker for gcloud/firebase CLI unlock.

## Identity rule

The service **must** authenticate as `focusedbluepanda@gmail.com`. It must never be
authenticated as `brunobarrientosf@gmail.com` or any other personal account.

## Credentials

| File | Purpose | Permissions |
|------|---------|-------------|
| `~/.gmail-service/credentials.json` | Desktop OAuth client secrets | `600` |
| `~/.gmail-service/token.json` | OAuth refresh/access token | `600` |

`credentials.json` is downloaded from Cloud Console under **APIs & Services → Credentials → OAuth 2.0 Client IDs → blue-panda-email-service-desktop**.

## OAuth flow

The OAuth app is **In production** as of 2026-07-11. This is required because
Google expires refresh tokens from external Testing apps after seven days. The
app remains unverified and private-use under Google's 100-user cap; publishing
the OAuth audience does not expose this service publicly. Authenticate only as
`focusedbluepanda@gmail.com`.

After moving Testing → Production, perform one fresh consent flow. A refresh
token issued while the app was still Testing may retain the seven-day lifetime.

### Option A: manual

```bash
cd /home/hetzner/AI/blue-panda-email-service
.venv/bin/python -c "from gmail_service.auth import run_oauth_flow; from gmail_service.config import Settings; run_oauth_flow(Settings.from_env())"
```

Open the printed URL in a browser signed in to `focusedbluepanda@gmail.com`,
authorize, and paste the code.

### Option B: Brave CDP automation

With Brave running on CDP port `9222` (see `google-cloud-control-plane` skill),
run the automation script that captures the authorization code from network
events and writes `token.json`.

Key detail: set the flow redirect URI explicitly because `http://localhost` may
be intercepted by Caddy/accueil on port 80:

```python
flow.redirect_uri = 'http://localhost'
auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')
```

## Running

Systemd service:

```bash
sudo systemctl enable --now gmail-service
```

Manual:

```bash
cd /home/hetzner/AI/blue-panda-email-service
.venv/bin/python -m gmail_service
```

## Verification

```bash
curl -fsS http://hetzner:9770/health
curl -fsS http://hetzner:9770/profile
```

`/profile` must show:

```json
{"email_address": "focusedbluepanda@gmail.com", ...}
```

The Hermes `Critical access health watchdog` forces this token and the personal
Workspace token through their refresh paths every 30 minutes. It is silent while
healthy and alerts on a new failure, recovery, or 24-hour unresolved reminder.
Revocation, password/security events, or a missing refresh token still require
human OAuth consent; automation must not bypass that boundary.

## Send a test email

```bash
curl -fsS -X POST http://hetzner:9770/send \
  -H 'Content-Type: application/json' \
  -d '{"to":"brunobarrientosf@gmail.com","subject":"Test","body":"Hello"}'
```

## Environment variables

| Variable | Default |
|----------|---------|
| `GMAIL_SERVICE_HOST` | `0.0.0.0` |
| `GMAIL_SERVICE_PORT` | `9770` |
| `GMAIL_CREDENTIALS_PATH` | `~/.gmail-service/credentials.json` |
| `GMAIL_TOKEN_PATH` | `~/.gmail-service/token.json` |
