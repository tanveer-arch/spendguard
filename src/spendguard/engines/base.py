"""Engine interface. All warehouse adapters implement this.

Design rule: importing an engine module must never require credentials or
perform I/O. Credential checks happen lazily inside the methods, raising
MissingCredentialsError with an actionable message.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from spendguard.models import ActualCost, Estimate


class MissingCredentialsError(RuntimeError):
    """Raised when an engine method needs cloud credentials that aren't set."""


class Engine(ABC):
    """One data warehouse: estimate, execute, and reconcile actual cost."""

    name: str = "engine"

    @abstractmethod
    def estimate(self, sql: str, warehouse: str | None = None) -> Estimate:
        """Pre-flight cost estimate. Must not execute the query.

        Should never raise on missing credentials without first raising
        MissingCredentialsError; never returns a bare number without an
        accuracy tier and caveats.
        """

    @abstractmethod
    def execute(
        self, sql: str, max_rows: int = 1000, **kwargs
    ) -> tuple[list[dict], str]:
        """Execute SQL and return (rows, query_id). Rows are capped at max_rows."""

    @abstractmethod
    def fetch_actual_cost(self, query_id: str) -> ActualCost:
        """Fetch the real billed cost of a previously executed query_id."""
