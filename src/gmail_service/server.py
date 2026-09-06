"""FastAPI app — Gmail HTTP API."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from google.auth.exceptions import RefreshError
from pydantic import BaseModel, Field

from gmail_service.auth import auth_status, get_authorized_credentials
from gmail_service.config import Settings
from gmail_service.gmail_client import GmailClient
from gmail_service.monitoring import MonitoringStore, RECIPIENT, SENDER, matches

logger = logging.getLogger(__name__)

# Once a refresh fails with invalid_grant, the refresh_token is dead until a human
# re-authenticates — retrying immediately on every incoming request just hammers
# Google's token endpoint. Circuit-break for a cooldown window instead.
_REFRESH_FAILURE_COOLDOWN_SEC = 60
_HEALTH_CHECK_TTL_SEC = 60

# ── Pydantic models ──────────────────────────────────────────────────────────

class AttachmentRequest(BaseModel):
    filename: str
    content_base64: str
    mime_type: str = "application/octet-stream"


class SendRequest(BaseModel):
    to: str
    subject: str
    body: str
    html: bool = False
    cc: str = ""
    bcc: str = ""
    attachments: list[AttachmentRequest] = Field(default_factory=list)


class SendResponse(BaseModel):
    success: bool
    message_id: str | None = None
    thread_id: str | None = None
    delivery_status: str = 'sent'
    queue_id: int | None = None


class DigestRequest(BaseModel):
    body: str = Field(..., max_length=64000)


class MessageRef(BaseModel):
    id: str
    thread_id: str


class ListResponse(BaseModel):
    messages: list[MessageRef]
    next_page_token: str | None = None
    result_size_estimate: int


class SearchRequest(BaseModel):
    q: str = Field(..., description="Gmail search query (e.g. 'from:me subject:test')")
    limit: int = Field(20, ge=1, le=100)


class NormalizedMessage(BaseModel):
    id: str | None
    thread_id: str | None
    label_ids: list[str]
    snippet: str | None
    from_: str = Field(..., alias="from")
    to: str
    subject: str
    date: str
    body: str
    attachments: list[dict] = Field(default_factory=list)


class ProfileResponse(BaseModel):
    email_address: str
    messages_total: int | None = None
    threads_total: int | None = None


class ThreadResponse(BaseModel):
    thread_id: str | None
    history_id: str | None
    messages: list[NormalizedMessage]


# ── App factory ──────────────────────────────────────────────────────────────

def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings.from_env()

    # Shared circuit-breaker state + health cache, closed over by the routes below.
    circuit = {"broken_until": 0.0, "last_error": None}
    health_cache = {"checked_at": 0.0, "result": None}

    send_state: dict = {"consecutive": 0, "last_failure": None}
    monitoring = MonitoringStore(settings.token_path.parent / 'monitoring.sqlite3') if settings.monitoring_enabled else None

    def require_gmail_client() -> GmailClient:
        if time.monotonic() < circuit["broken_until"]:
            raise HTTPException(
                status_code=503,
                detail=f"Gmail auth known-broken, cooling down: {circuit['last_error']}",
            )
        client: GmailClient | None = app.state.gmail
        if client is None:
            raise HTTPException(status_code=503, detail="Gmail client not initialized")
        return client

    def record_gmail_error(exc: Exception) -> None:
        if isinstance(exc, RefreshError) or "invalid_grant" in str(exc):
            circuit["broken_until"] = time.monotonic() + _REFRESH_FAILURE_COOLDOWN_SEC
            circuit["last_error"] = str(exc)

    def record_send_failure(exc: Exception) -> None:
        """A lost email used to leave no trace outside the journal."""
        send_state["consecutive"] += 1
        send_state["last_failure"] = {
            "error": str(exc),
            "at": datetime.now(timezone.utc).isoformat(),
            "consecutive": send_state["consecutive"],
        }

    def record_send_success() -> None:
        send_state["consecutive"] = 0
        send_state["last_failure"] = None

    def probe_gmail_auth() -> dict:
        """Real auth check, not just local expiry — cached to avoid hammering Google."""
        if time.monotonic() < circuit["broken_until"]:
            return {"authenticated": True, "valid": False, "reason": "refresh_failed_recently", "error": circuit["last_error"]}
        client: GmailClient | None = app.state.gmail
        if client is None:
            return auth_status(settings)
        try:
            client.get_profile()
            return {"authenticated": True, "valid": True}
        except Exception as exc:
            record_gmail_error(exc)
            return {"authenticated": True, "valid": False, "reason": "live_check_failed", "error": str(exc)}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        try:
            creds = get_authorized_credentials(settings)
            app.state.gmail = GmailClient(creds)
            app.state.settings = settings
            logger.info("Gmail client initialized")
        except Exception as exc:
            logger.error("Failed to initialize Gmail client: %s", exc)
            app.state.gmail = None
        yield
        logger.info("Gmail service shutting down")

    app = FastAPI(
        title="Gmail Service",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── Health ──────────────────────────────────────────────────────────

    @app.get("/health")
    def health() -> dict:
        now = time.monotonic()
        if now - health_cache["checked_at"] > _HEALTH_CHECK_TTL_SEC:
            health_cache["result"] = probe_gmail_auth()
            health_cache["checked_at"] = now
        return {
            "status": "ok",
            "gmail_auth": health_cache["result"],
            "last_send_failure": send_state["last_failure"],
            "monitoring": monitoring.status() if monitoring else {'enabled': False},
            "service": "gmail-service",
            "version": "0.1.0",
        }

    # ── Profile ─────────────────────────────────────────────────────────

    @app.get("/profile", response_model=ProfileResponse)
    def profile() -> dict:
        client = require_gmail_client()
        try:
            return client.get_profile()
        except Exception as exc:
            logger.exception("get_profile failed")
            record_gmail_error(exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── Send ────────────────────────────────────────────────────────────

    @app.post("/send", response_model=SendResponse)
    def send_email(req: SendRequest) -> dict:
        if monitoring and matches(req.to, req.cc, req.bcc, req.subject):
            if req.to.strip().lower() != RECIPIENT or req.cc or req.bcc or req.attachments or req.html:
                raise HTTPException(422, 'Monitoring notices must be plain text to Bruno only, without attachments')
            try:
                return monitoring.enqueue(req.subject, req.body)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        client = require_gmail_client()
        try:
            result = client.send_email(
                to=req.to,
                subject=req.subject,
                body=req.body,
                html=req.html,
                cc=req.cc,
                bcc=req.bcc,
                attachments=[att.model_dump() if hasattr(att, "model_dump") else att.dict() for att in req.attachments],
            )
            record_send_success()
            return result
        except Exception as exc:
            logger.exception("send_email failed")
            record_gmail_error(exc)
            record_send_failure(exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    def local_monitoring(request):
        if not monitoring:
            raise HTTPException(404, 'Monitoring digest disabled')
        if not request.client or request.client.host not in ('127.0.0.1', '::1'):
            raise HTTPException(403, 'Local monitoring controller only')

    @app.get('/monitoring/status')
    def monitoring_status():
        return monitoring.status() if monitoring else {'enabled': False}

    @app.get('/monitoring/events')
    def monitoring_events(request: Request):
        local_monitoring(request)
        return {'events': monitoring.events()}

    @app.post('/monitoring/digest')
    def monitoring_digest(req: DigestRequest, request: Request):
        local_monitoring(request)
        client = require_gmail_client()
        # Preflight before consuming the daily budget; no send has begun yet.
        try:
            if client.get_profile().get('email_address') != SENDER:
                raise HTTPException(503, 'Panda sender identity mismatch')
        except HTTPException:
            raise
        except Exception as exc:
            record_gmail_error(exc)
            raise HTTPException(503, 'Panda sender preflight failed') from exc
        acquired, reservation = monitoring.reserve(req.body)
        if not acquired:
            if reservation['status'] == 'sent':
                return {'success': True, 'delivery_status': 'already_sent', 'message_id': reservation['message_id']}
            raise HTTPException(409, 'Daily send already reserved or uncertain; reconcile Gmail before any recovery')
        try:
            result = client.send_email(to=RECIPIENT,
                subject=f'[Universe Alert] Daily review — {reservation["day"]}',
                body=reservation['body'], max_attempts=1)
            if not result.get('success') or not result.get('message_id'):
                raise RuntimeError('Gmail did not confirm a message ID')
            monitoring.finish(reservation, result=result)
            record_send_success()
            return {**result, 'delivery_status': 'sent'}
        except Exception as exc:
            monitoring.finish(reservation, error=str(exc))
            record_gmail_error(exc)
            record_send_failure(exc)
            raise HTTPException(502, 'Daily delivery uncertain; automatic resend blocked') from exc

    # ── Inbox ───────────────────────────────────────────────────────────

    @app.get("/inbox", response_model=ListResponse)
    def inbox(limit: int = 20, page_token: str | None = None) -> dict:
        client = require_gmail_client()
        try:
            return client.list_inbox(limit=limit, page_token=page_token)
        except Exception as exc:
            logger.exception("list_inbox failed")
            record_gmail_error(exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── Search ──────────────────────────────────────────────────────────

    @app.get("/search", response_model=ListResponse)
    def search(q: str, limit: int = 20) -> dict:
        client = require_gmail_client()
        try:
            return client.search_emails(query=q, limit=limit)
        except Exception as exc:
            logger.exception("search_emails failed")
            record_gmail_error(exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── Message ─────────────────────────────────────────────────────────

    @app.get("/messages/{message_id}", response_model=NormalizedMessage)
    def get_message(message_id: str) -> dict:
        client = require_gmail_client()
        try:
            return client.get_message(message_id)
        except Exception as exc:
            logger.exception("get_message failed")
            record_gmail_error(exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── Thread ──────────────────────────────────────────────────────────

    @app.get("/threads/{thread_id}", response_model=ThreadResponse)
    def get_thread(thread_id: str) -> dict:
        client = require_gmail_client()
        try:
            return client.get_thread(thread_id)
        except Exception as exc:
            logger.exception("get_thread failed")
            record_gmail_error(exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app
