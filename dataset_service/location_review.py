"""Shared metadata for operator-confirmed spatial registration."""

from datetime import datetime, timezone


def confirmed_location_review(*, source_crs: str, evidence: str, scope: list[str], reviewer: str) -> dict:
    """Build a server-timestamped record after an authenticated operator confirms location data."""
    return {
        "status": "confirmed",
        "source_crs": source_crs,
        "evidence": evidence.strip(),
        "coordinate_order": "longitude,latitude",
        "scope": scope,
        "reviewed_by": reviewer,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
