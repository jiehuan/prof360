"""Secrets from a Databricks secret scope (D-057), for runs on a Databricks cluster.

Environment variables take precedence (see `clients/db.py` and `clients/storage.py`); config
holds only the scope and key names. Secret values are never logged.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)

SecretGetter = Callable[[str, str], str]


def databricks_secret(scope: str, key: str) -> str:
    """`dbutils.secrets.get(scope, key)`; only available on a Databricks cluster."""
    try:
        dbutils_module = importlib.import_module("pyspark.dbutils")
        sql_module = importlib.import_module("pyspark.sql")
    except ImportError as exc:
        raise RuntimeError(
            f"secret {key!r} in scope {scope!r}: Databricks secret scopes are only available on "
            "a Databricks cluster; elsewhere set the environment variables instead"
        ) from exc
    log.info("reading secret %s from Databricks secret scope %s", key, scope)
    spark = sql_module.SparkSession.builder.getOrCreate()
    return str(dbutils_module.DBUtils(spark).secrets.get(scope=scope, key=key))
