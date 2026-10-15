"""FastAPI application: `/api/v1` over the application database (D-048). OpenAPI at /docs."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine, text

from p360_bff.db import make_bff_engine
from p360_bff.routers import companies, me
from p360_bff.settings import Settings

API = "/api/v1"


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(
        title="Professional 360 BFF",
        version="0.1.0",
        description="Company pages for D&O underwriting: metrics, insights, risks, "
        "follow-up questions, news and user state.",
    )
    app.state.settings = settings
    app.state.engine = engine or make_bff_engine(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.bff_cors_origins,
        allow_methods=["GET", "PUT", "DELETE"],
        allow_headers=["*"],
    )
    app.include_router(companies.router, prefix=API)
    app.include_router(me.router, prefix=API)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, Any]:
        try:
            with app.state.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return {"status": "ok", "database": "ok"}
        except Exception as exc:  # report, do not crash the probe
            return {"status": "degraded", "database": type(exc).__name__}

    return app
