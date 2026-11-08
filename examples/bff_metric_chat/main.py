"""How the BFF wires the chat into its FastAPI app (reference; adapt to the BFF's own setup).

    uvicorn examples.bff_metric_chat.main:app      # needs BFF_DATABASE_URL and SECUREGPT_*

Environment: SECUREGPT_CLIENT_ID, SECUREGPT_CLIENT_SECRET, SECUREGPT_SUBSCRIPTION_KEY,
SECUREGPT_ENV (dv | pp | pr); BFF_DATABASE_URL (a SQLAlchemy URL to the application database,
e.g. mssql+pyodbc://...); METRIC_CHAT_MODEL (optional, default gpt-6-luna-2026-09-22).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy import create_engine

from uw_metric_chat import AsyncSecureGPT, SecureGPTSettings

from .api import ChatSettings, RateLimiter, router
from .store import ChatStore, SqlChatStore


def create_app(
    store: ChatStore | None = None,
    client: Any = None,
    settings: ChatSettings | None = None,
) -> FastAPI:
    """`store` and `client` default to the database and SecureGPT from the environment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or ChatSettings(model=os.environ.get("METRIC_CHAT_MODEL")
                                       or ChatSettings().model)  # fmt: skip
        own_client = client is None
        app.state.chat_settings = cfg
        app.state.chat_limiter = RateLimiter(cfg.questions_per_hour)
        app.state.chat_store = store or SqlChatStore(create_engine(os.environ["BFF_DATABASE_URL"]))
        app.state.chat_client = client or AsyncSecureGPT(SecureGPTSettings.from_env())
        try:
            yield
        finally:
            if own_client:  # one SecureGPT client per process, closed on shutdown
                await app.state.chat_client.aclose()

    app = FastAPI(title="Metric chat (reference)", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
