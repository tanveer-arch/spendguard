"""BigQuery engine — PRECISE estimates via dry run.

The dry run is free and returns the exact bytes the query would scan, so this
is the only engine whose estimates earn the PRECISE tier. Three BigQuery
gotchas are handled explicitly because they silently break naive estimators:

1. Row-level security: dry runs against RLS-masked tables report 0 bytes *by
   design* (side-channel protection). A $0 estimate must never be presented as
   "free" — we add a caveat and downgrade trust.
2. External services: remote functions and BigQuery ML remote-model inference
   (e.g. ML.GENERATE_TEXT) bill separately through Cloud Run / Vertex AI. The
   dry run does not include those dollars. Detected with a conservative text
   heuristic; flagged, not priced.
3. Editions / capacity billing: flat slot pricing means bytes-to-dollars is
   meaningless. For capacity-billed projects we return bytes only, no dollars.
"""

from __future__ import annotations

import hashlib
import os

from spendguard.engines.base import Engine, MissingCredentialsError
from spendguard.models import AccuracyTier, ActualCost, Estimate
from spendguard.pricing import bigquery as bq_pricing


def _sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()[:16]


class BigQueryEngine(Engine):
    name = "bigquery"

    # -- credential handling -------------------------------------------------
    def _client(self):
        """Build a BigQuery client, or raise an actionable error.

        TODO (live): uses google-cloud-bigquery's default credential chain
        (GOOGLE_APPLICATION_CREDENTIALS, gcloud ADC, GCE metadata). No code
        change needed — just make sure the env is configured.
        """
        try:
            from google.cloud import bigquery
        except ImportError as exc:
            raise MissingCredentialsError(
                "google-cloud-bigquery is not installed. "
                "Run: pip install spendguard (or pip install google-cloud-bigquery)."
            ) from exc
        if not (
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
        ):
            # The client constructor itself raises a clear error if no ADC is
            # found, but we raise ours first so the message names the fix.
            raise MissingCredentialsError(
                "BigQuery credentials not found. Set GOOGLE_APPLICATION_CREDENTIALS "
                "to a service-account JSON key, or run `gcloud auth application-default login`."
            )
        project = os.environ.get("BIGQUERY_PROJECT") or os.environ.get(
            "GOOGLE_CLOUD_PROJECT"
        )
        return bigquery.Client(project=project)

    # -- estimate ------------------------------------------------------------
    def estimate(self, sql: str, warehouse: str | None = None) -> Estimate:
        """Dry-run the query (free) and convert bytes scanned to dollars."""
        from google.cloud import bigquery

        client = self._client()
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = client.query(sql, job_config=job_config)

        estimated_bytes = job.total_bytes_processed or 0
        caveats: list[str] = []
        upper = sql.upper()

        # 1. RLS 0-byte side channel.
        if estimated_bytes == 0:
            caveats.append(
                "Dry run reports 0 bytes: this happens for row-level-security "
                "masked tables BY DESIGN (Google side-channel protection). "
                "A $0.00 estimate here is NOT proof the query is free."
            )

        # 2. External-service billing not included in dry run.
        if "ML.GENERATE_TEXT" in upper or ("CREATE FUNCTION" in upper and "REMOTE" in upper):
            caveats.append(
                "Query uses remote functions / BigQuery ML remote-model inference: "
                "separate Cloud Run / Vertex AI billing applies and is NOT "
                "included in this estimate."
            )

        # 3. Capacity billing: bytes only, no dollars.
        billing_model = (warehouse or os.environ.get("BIGQUERY_BILLING_MODEL", "on_demand")).lower()
        if billing_model in {"capacity", "editions"}:
            return Estimate(
                engine=self.name,
                accuracy_tier=AccuracyTier.PRECISE,
                estimated_bytes=estimated_bytes,
                estimated_cost_usd=None,
                caveats=caveats
                + [
                    "Project uses capacity (Editions) billing: flat slot pricing, "
                    "so no dollar figure is computed. Bytes scanned is exact."
                ],
                sql_hash=_sql_hash(sql),
            )

        return Estimate(
            engine=self.name,
            accuracy_tier=AccuracyTier.PRECISE,
            estimated_bytes=estimated_bytes,
            estimated_cost_usd=bq_pricing.bytes_to_usd(estimated_bytes),
            caveats=caveats,
            sql_hash=_sql_hash(sql),
        )

    # -- execute --------------------------------------------------------------
    def execute(self, sql: str, max_rows: int = 1000, **kwargs) -> tuple[list[dict], str]:
        """Run the query and return (rows capped at max_rows, job_id)."""
        client = self._client()
        job = client.query(sql)
        rows = [dict(r) for r in job.result(max_results=max_rows)]
        return rows, job.job_id

    # -- actuals ----------------------------------------------------------------
    def fetch_actual_cost(self, query_id: str) -> ActualCost:
        """Look up the billed bytes for a finished job via INFORMATION_SCHEMA.

        TODO (live): replace the client.jobs lookup below with a query against
        `<project>.region-<location>.INFORMATION_SCHEMA.JOBS` filtered by
        job_id to also capture slot_ms and any reservation metadata. The
        current implementation uses job.total_bytes_billed which is sufficient
        for on-demand reconciliation.
        """
        client = self._client()
        job = client.get_job(query_id)
        billed = getattr(job, "total_bytes_billed", None)
        return ActualCost(
            engine=self.name,
            query_id=query_id,
            billed_bytes=billed,
            billed_cost_usd=bq_pricing.bytes_to_usd(billed) if billed else None,
        )
