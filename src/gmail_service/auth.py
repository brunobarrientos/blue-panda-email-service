"""Gmail OAuth2 authentication — offline refresh tokens, auto-refresh."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from gmail_service.config import Settings

logger = logging.getLogger(__name__)


def ensure_token_dir(token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)


def load_credentials(settings: Settings) -> Credentials | None:
    """Load existing OAuth credentials from disk, refreshing if needed."""
    if not settings.token_path.exists():
        return None

    creds = Credentials.from_authorized_user_file(str(settings.token_path), list(settings.scopes))

    if creds.expired and creds.refresh_token:
        logger.info("Refreshing expired Gmail OAuth token")
        creds.refresh(Request())
        _save_credentials(creds, settings.token_path)

    return creds


def run_oauth_flow(settings: Settings, headless: bool = False) -> Credentials:
    """Run the desktop OAuth2 flow and persist the refresh token."""
    if not settings.credentials_path.exists():
        raise FileNotFoundError(
            f"Gmail credentials not found: {settings.credentials_path}\n"
            "Download them from Google Cloud Console → APIs & Services → Credentials."
        )

    ensure_token_dir(settings.token_path)

    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.credentials_path),
        list(settings.scopes),
    )

    if headless or not sys.stdout.isatty():
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        print(f"\nAUTH_URL={auth_url}\n")
        print("Open the URL above in your browser, authorize the app, then paste the authorization code here:")
        code = input("Code: ").strip()
        flow.fetch_token(code=code)
        creds = flow.credentials
    else:
        creds = flow.run_local_server(port=0)

    _save_credentials(creds, settings.token_path)
    logger.info("Gmail OAuth token saved to %s", settings.token_path)
    return creds


def _save_credentials(creds: Credentials, path: Path) -> None:
    ensure_token_dir(path)
    path.write_text(
        json.dumps(
            {
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": creds.scopes,
                "expiry": creds.expiry.isoformat() if creds.expiry else None,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def get_authorized_credentials(settings: Settings) -> Credentials:
    """Return valid Gmail credentials, running OAuth flow if necessary."""
    creds = load_credentials(settings)
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds, settings.token_path)
            return creds
        except Exception as exc:
            logger.warning("Token refresh failed, re-authenticating: %s", exc)

    return run_oauth_flow(settings, headless=True)


def auth_status(settings: Settings) -> dict:
    """Return current auth state without triggering interactive flow."""
    if not settings.credentials_path.exists():
        return {"authenticated": False, "reason": "credentials_file_missing"}

    if not settings.token_path.exists():
        return {"authenticated": False, "reason": "token_missing"}

    try:
        creds = Credentials.from_authorized_user_file(str(settings.token_path), list(settings.scopes))
    except Exception as exc:
        return {"authenticated": False, "reason": "token_invalid", "error": str(exc)}

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            return {"authenticated": True, "valid": False, "reason": "token_expired_refreshable"}
        return {"authenticated": True, "valid": False, "reason": "token_invalid"}

    return {"authenticated": True, "valid": True, "scopes": creds.scopes}
