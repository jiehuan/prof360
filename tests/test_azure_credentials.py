"""D-057: Azure SQL and ADLS credentials come from the environment when set, else from the
Databricks secret scope named in config/azure.yaml; the URL follows config (driver,
certificate check)."""

import pytest

from tests.conftest import CONFIG_DIR
from uw_pipeline.clients.db import (
    AzureSqlConfig,
    azure_sql_url,
    database_url,
    load_azure_config,
    scoped_credentials,
)
from uw_pipeline.settings import Settings

BASE = {"sec_user_agent": "Test test@example.com", "database_backend": "azure_sql",
        "azure_env": "dv"}  # fmt: skip


class Scope:
    """A fake secret scope that records what was read."""

    def __init__(self) -> None:
        self.read: list[tuple[str, str]] = []

    def __call__(self, scope: str, key: str) -> str:
        self.read.append((scope, key))
        return f"value-of-{key}"


def test_environment_variables_win_over_the_secret_scope(tmp_path) -> None:
    scope = Scope()
    s = Settings(**BASE, data_dir=tmp_path, azure_sql_client_id="env-id",
                 azure_sql_client_secret="env-secret")  # fmt: skip
    url, schema = database_url(s, get_secret=scope)
    assert schema == "uw_pipeline"
    assert (url.username, url.password) == ("env-id", "env-secret")
    assert scope.read == []


def test_secret_scope_keys_from_config_when_no_environment(tmp_path) -> None:
    scope = Scope()
    url, _ = database_url(Settings(**BASE, data_dir=tmp_path), app=True, get_secret=scope)
    e = load_azure_config(CONFIG_DIR).azure_sql.environments["dv"]
    assert scope.read == [(e.secret_scope, e.username_key), (e.secret_scope, e.password_key)]
    assert url.username == f"value-of-{e.username_key}"
    assert url.query["driver"] == "ODBC Driver 17 for SQL Server"
    assert url.query["authentication"] == "ActiveDirectoryServicePrincipal"
    assert url.query["TrustServerCertificate"] == "no"  # checked unless config says otherwise
    assert f"value-of-{e.password_key}" not in repr(url)  # the secret is masked in logs


def test_missing_credentials_name_both_sources() -> None:
    with pytest.raises(RuntimeError, match=r"AZURE_SQL_CLIENT_ID.*secret scope"):
        scoped_credentials((None, None), None, (None, None), Scope(), "needs AZURE_SQL_CLIENT_ID")

    def off_databricks(scope: str, key: str) -> str:
        raise RuntimeError("only on a Databricks cluster")

    with pytest.raises(RuntimeError, match=r"needs X.*secret scope s needs Databricks"):
        scoped_credentials((None, None), "s", ("a", "b"), off_databricks, "needs X")


def test_certificate_check_follows_config() -> None:
    cfg = load_azure_config(CONFIG_DIR).azure_sql
    trusting = AzureSqlConfig.model_validate(cfg.model_dump() | {"trust_server_certificate": True})
    assert azure_sql_url(trusting, "dv", "i", "s").query["TrustServerCertificate"] == "yes"
    assert azure_sql_url(cfg, "dv", "i", "s").query["Encrypt"] == "yes"
