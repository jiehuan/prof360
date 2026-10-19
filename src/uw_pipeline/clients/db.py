"""Database engine construction (pipeline and application databases).

Local development uses SQLite files; the corporate deployment uses Azure SQL (Phase 12, D-040)
with service-principal authentication, its URL built from config/azure.yaml plus credentials
from the environment. Tables are defined without a schema; an engine maps them to its schema
(`schema_translate_map`), so the same definitions serve the pipeline schema and the app schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel
from sqlalchemy import (
    URL,
    Connection,
    Engine,
    MetaData,
    create_engine,
    event,
    inspect,
    make_url,
    text,
)
from sqlalchemy.schema import CreateColumn, CreateSchema

from uw_pipeline.schema import metadata

if TYPE_CHECKING:
    from uw_pipeline.settings import Settings


class AzureSqlEnv(BaseModel):
    server: str
    port: int = 1433
    database: str
    pipeline_schema: str
    app_schema: str


class AzureSqlConfig(BaseModel):
    driver: str
    authentication: str
    environments: dict[str, AzureSqlEnv]


class AdlsEnv(BaseModel):
    storage_account: str
    container: str
    root: str = ""


class AdlsConfig(BaseModel):
    tenant_id: str
    environments: dict[str, AdlsEnv]


class AzureConfig(BaseModel):
    default_env: str
    azure_sql: AzureSqlConfig
    adls: AdlsConfig


def load_azure_config(config_dir: Path) -> AzureConfig:
    with (config_dir / "azure.yaml").open(encoding="utf-8") as f:
        return AzureConfig.model_validate(yaml.safe_load(f))


def _sqlite_wal(dbapi_conn: Any, _record: Any) -> None:
    """WAL journal mode (kept in the file once set)."""
    dbapi_conn.execute("PRAGMA journal_mode=WAL")


def make_engine(url: str | URL, schema: str | None = None) -> Engine:
    """Engine for `url`; with `schema`, unqualified tables live in that schema."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        if parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
            # Parallel workers write in short transactions (D-052): a writer waits up to 60 s
            # for the lock instead of failing, and WAL lets readers and the writer proceed
            # together.
            engine = create_engine(parsed, connect_args={"timeout": 60})
            event.listen(engine, "connect", _sqlite_wal)
        else:
            engine = create_engine(parsed)
    elif parsed.get_backend_name() == "mssql":
        engine = create_engine(parsed, fast_executemany=True, pool_pre_ping=True)
    else:
        engine = create_engine(parsed)
    if schema:
        engine = engine.execution_options(schema_translate_map={None: schema})
    return engine


def azure_sql_url(cfg: AzureSqlConfig, env: str, client_id: str, client_secret: str) -> URL:
    """mssql+pyodbc URL with service-principal authentication. The server certificate is
    verified (Encrypt=yes, TrustServerCertificate=no)."""
    if env not in cfg.environments:
        raise ValueError(f"AZURE_ENV={env!r} is not in config/azure.yaml")
    e = cfg.environments[env]
    return URL.create(
        "mssql+pyodbc", username=client_id, password=client_secret, host=e.server,
        port=e.port, database=e.database,
        # MARS: a loop may read one result while issuing another query on the connection;
        # without it SQL Server answers "connection is busy" (call sites read fully as well).
        query={"driver": cfg.driver, "authentication": cfg.authentication, "Encrypt": "yes",
               "TrustServerCertificate": "no", "MARS_Connection": "yes"},
    )  # fmt: skip


def azure_config(settings: Settings) -> tuple[AzureConfig, str]:
    """config/azure.yaml and the environment in use (AZURE_ENV, else the config default)."""
    cfg = load_azure_config(settings.config_dir)
    return cfg, settings.azure_env or cfg.default_env


def _sql_engine(settings: Settings, app: bool) -> Engine:
    url = settings.app_database_url if app else settings.database_url
    schema = settings.app_database_schema if app else settings.database_schema
    if settings.database_backend == "azure_sql":
        cfg, env = azure_config(settings)
        if not (settings.azure_sql_client_id and settings.azure_sql_client_secret):
            raise RuntimeError("DATABASE_BACKEND=azure_sql needs AZURE_SQL_CLIENT_ID and "
                               "AZURE_SQL_CLIENT_SECRET")  # fmt: skip
        e = cfg.azure_sql.environments.get(env)
        if e is None:
            raise ValueError(f"AZURE_ENV={env!r} is not in config/azure.yaml")
        url_obj = azure_sql_url(cfg.azure_sql, env, settings.azure_sql_client_id,
                                settings.azure_sql_client_secret.get_secret_value())  # fmt: skip
        return make_engine(url_obj, schema or (e.app_schema if app else e.pipeline_schema))
    return make_engine(url, schema)


def pipeline_engine(settings: Settings) -> Engine:
    """The pipeline's own database (DATABASE_URL, or Azure SQL with DATABASE_BACKEND)."""
    return _sql_engine(settings, app=False)


def app_engine(settings: Settings) -> Engine:
    """The application database the BFF reads (APP_DATABASE_URL, or Azure SQL)."""
    return _sql_engine(settings, app=True)


def table_schema(conn: Connection) -> str | None:
    """The schema unqualified tables map to on this connection (None = default schema)."""
    mapping = conn.get_execution_options().get("schema_translate_map") or {}
    schema: str | None = mapping.get(None)
    return schema


def ensure_schema(engine: Engine, md: MetaData = metadata) -> None:
    """Create missing tables and add missing columns (local SQLite only; Azure SQL migrations
    are owned elsewhere). New columns must be nullable or have a server default."""
    if engine.dialect.name != "sqlite":
        raise RuntimeError("ensure_schema is only for local SQLite; apply migrations for other DBs")
    md.create_all(engine)
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in md.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = CreateColumn(col).compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}'))


def create_tables(engine: Engine, md: MetaData = metadata) -> None:
    """Create the schema and missing tables on a server database. For local SQL Server tests
    and new environments only; shared environments get the reviewed scripts in deploy/sql/."""
    with engine.begin() as conn:
        schema = table_schema(conn)
        if schema and not inspect(conn).has_schema(schema):
            conn.execute(CreateSchema(schema))
    md.create_all(engine)
