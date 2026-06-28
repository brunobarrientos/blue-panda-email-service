"""Configuration — env-var driven, frozen dataclass pattern."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Create via ``Settings.from_env()``.

    Tokens and credentials are stored outside the repo in ``~/.gmail-service/``.
    """

    # Server
    host: str = "0.0.0.0"
    port: int = 9770
    log_level: str = "info"

    # Gmail OAuth
    credentials_path: Path = field(
        default_factory=lambda: Path.home() / ".gmail-service" / "credentials.json"
    )
    token_path: Path = field(
        default_factory=lambda: Path.home() / ".gmail-service" / "token.json"
    )
    # Scopes needed for send + read/search
    scopes: tuple[str, ...] = (
        "https://www.googleapis.com/auth/gmail.modify",
    )

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            host=_env("GMAIL_SERVICE_HOST", "0.0.0.0"),
            port=_int_env("GMAIL_SERVICE_PORT", 9770),
            log_level=_env("GMAIL_SERVICE_LOG_LEVEL", "info"),
            credentials_path=Path(_env("GMAIL_CREDENTIALS_PATH", str(Path.home() / ".gmail-service" / "credentials.json"))),
            token_path=Path(_env("GMAIL_TOKEN_PATH", str(Path.home() / ".gmail-service" / "token.json"))),
        )
