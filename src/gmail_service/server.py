"""FastAPI app — Gmail HTTP API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from gmail_service.auth import auth_status, get_authorized_credentials
from gmail_service.config import Settings
from gmail_service.gmail_client import GmailClient

logger = logging.getLogger(__name__)

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
        status = auth_status(settings)
        return {
            "status": "ok",
            "gmail_auth": status,
            "service": "gmail-service",
            "version": "0.1.0",
        }

    # ── Profile ─────────────────────────────────────────────────────────

    @app.get("/profile", response_model=ProfileResponse)
    def profile() -> dict:
        client: GmailClient | None = app.state.gmail
        if client is None:
            raise HTTPException(status_code=503, detail="Gmail client not initialized")
        try:
            return client.get_profile()
        except Exception as exc:
            logger.exception("get_profile failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── Send ────────────────────────────────────────────────────────────

    @app.post("/send", response_model=SendResponse)
    def send_email(req: SendRequest) -> dict:
        client: GmailClient | None = app.state.gmail
        if client is None:
            raise HTTPException(status_code=503, detail="Gmail client not initialized")
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
            return result
        except Exception as exc:
            logger.exception("send_email failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── Inbox ───────────────────────────────────────────────────────────

    @app.get("/inbox", response_model=ListResponse)
    def inbox(limit: int = 20, page_token: str | None = None) -> dict:
        client: GmailClient | None = app.state.gmail
        if client is None:
            raise HTTPException(status_code=503, detail="Gmail client not initialized")
        try:
            return client.list_inbox(limit=limit, page_token=page_token)
        except Exception as exc:
            logger.exception("list_inbox failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── Search ──────────────────────────────────────────────────────────

    @app.get("/search", response_model=ListResponse)
    def search(q: str, limit: int = 20) -> dict:
        client: GmailClient | None = app.state.gmail
        if client is None:
            raise HTTPException(status_code=503, detail="Gmail client not initialized")
        try:
            return client.search_emails(query=q, limit=limit)
        except Exception as exc:
            logger.exception("search_emails failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── Message ─────────────────────────────────────────────────────────

    @app.get("/messages/{message_id}", response_model=NormalizedMessage)
    def get_message(message_id: str) -> dict:
        client: GmailClient | None = app.state.gmail
        if client is None:
            raise HTTPException(status_code=503, detail="Gmail client not initialized")
        try:
            return client.get_message(message_id)
        except Exception as exc:
            logger.exception("get_message failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── Thread ──────────────────────────────────────────────────────────

    @app.get("/threads/{thread_id}", response_model=ThreadResponse)
    def get_thread(thread_id: str) -> dict:
        client: GmailClient | None = app.state.gmail
        if client is None:
            raise HTTPException(status_code=503, detail="Gmail client not initialized")
        try:
            return client.get_thread(thread_id)
        except Exception as exc:
            logger.exception("get_thread failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app
