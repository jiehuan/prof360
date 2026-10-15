"""Engine for the application database: SQLite in development, Azure SQL in deployment (same
connection rules as the pipeline, D-040, with the BFF's own service principal)."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, make_url

from p360_bff.settings import Settings
from p360_bff.user_tables import user_metadata
from uw_pipeline.clients.db import azure_sql_url, load_azure_config, make_engine
from uw_pipeline.schema_app import app_metadata


def make_bff_engine(settings: Settings) -> Engine:
    if settings.bff_database_backend == "azure_sql":
        if not (settings.bff_sql_client_id and settings.bff_sql_client_secret):
            raise RuntimeError(
                "BFF_DATABASE_BACKEND=azure_sql needs BFF_SQL_CLIENT_ID and BFF_SQL_CLIENT_SECRET"
            )
        cfg = load_azure_config(settings.config_dir)
        env = settings.azure_env or cfg.default_env
        url = azure_sql_url(
            cfg.azure_sql,
            env,
            settings.bff_sql_client_id,
            settings.bff_sql_client_secret.get_secret_value(),
        )
        return make_engine(url, cfg.azure_sql.environments[env].app_schema)
    url = make_url(settings.bff_database_url)
    if url.get_backend_name() == "sqlite":
        # Requests run in a thread pool; SQLite connections are used by one request at a time.
        engine = create_engine(url, connect_args={"check_same_thread": False})
        user_metadata.create_all(engine)  # development only; Azure SQL gets deploy/sql DDL
        return engine
    return make_engine(url)


def create_dev_tables(engine: Engine) -> None:
    """Application and user tables on an empty SQLite database (tests, local development)."""
    app_metadata.create_all(engine)
    user_metadata.create_all(engine)
