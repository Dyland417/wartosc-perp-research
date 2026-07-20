"""Validated, idempotent persistence services."""

from .oracle_archive import OracleArchiveIngestionResult, OracleArchiveIngestionService
from .service import IngestionResult, IngestionService

__all__ = [
    "IngestionResult",
    "IngestionService",
    "OracleArchiveIngestionResult",
    "OracleArchiveIngestionService",
]
