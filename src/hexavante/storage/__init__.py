"""Storage SQLite — re-exporta API pública sem efeitos na importação."""

from hexavante.storage.db import connect, get_default_db_path, get_user_version, migrate
from hexavante.storage.schema import CURRENT_SCHEMA_VERSION, EXPECTED_TABLES

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "EXPECTED_TABLES",
    "connect",
    "get_default_db_path",
    "get_user_version",
    "migrate",
]
