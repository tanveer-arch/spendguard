"""BigQuery on-demand pricing.

BigQuery bills on-demand queries by bytes scanned: $6.25 per TiB
(1 TiB = 2**40 bytes), as published on Google Cloud's pricing page.
The first 1 TiB per month per project is free; spendguard does not
subtract the free tier — estimates are deliberately conservative.

Capacity/Editions billing is byte-free (flat slot pricing), so dollar
estimates are meaningless there: the BigQuery engine returns bytes only
for capacity-billed projects.
"""

USD_PER_TIB = 6.25
BYTES_PER_TIB = 2**40


def bytes_to_usd(total_bytes: int) -> float:
    """Convert bytes scanned to on-demand USD. Pure math, no I/O."""
    if total_bytes < 0:
        raise ValueError("total_bytes must be >= 0")
    return total_bytes / BYTES_PER_TIB * USD_PER_TIB
