"""Warehouse engine adapters."""

from spendguard.engines.base import Engine, MissingCredentialsError
from spendguard.engines.bigquery import BigQueryEngine
from spendguard.engines.databricks import DatabricksEngine
from spendguard.engines.snowflake import SnowflakeEngine

ENGINES: dict[str, type[Engine]] = {
    "bigquery": BigQueryEngine,
    "snowflake": SnowflakeEngine,
    "databricks": DatabricksEngine,
}


def get_engine(name: str) -> Engine:
    """Return an engine instance by name (bigquery | snowflake | databricks)."""
    try:
        return ENGINES[name.strip().lower()]()
    except KeyError:
        raise ValueError(
            f"Unknown engine {name!r}; expected one of {sorted(ENGINES)}"
        ) from None


__all__ = [
    "ENGINES",
    "BigQueryEngine",
    "DatabricksEngine",
    "Engine",
    "MissingCredentialsError",
    "SnowflakeEngine",
    "get_engine",
]
