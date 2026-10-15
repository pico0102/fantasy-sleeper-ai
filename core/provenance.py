"""Inspectable provenance record builders and staleness checkers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from core.schemas import ProvenanceRecord, ProvenanceSourceId, ProvenanceSourceType, ProvenanceStatus


def now_utc_iso() -> str:
    """Current UTC timestamp formatted as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def build_provenance_record(
    source_id: ProvenanceSourceId,
    source_type: ProvenanceSourceType,
    endpoint_or_path: str,
    status: ProvenanceStatus = "ok",
    record_count: int = 0,
    error_detail: Optional[str] = None,
    fetched_at_utc: Optional[str] = None,
    age_seconds: float = 0.0,
) -> ProvenanceRecord:
    """Construct a validated ProvenanceRecord."""
    timestamp = fetched_at_utc or now_utc_iso()
    return ProvenanceRecord(
        source_id=source_id,
        source_type=source_type,
        endpoint_or_path=endpoint_or_path,
        fetched_at_utc=timestamp,
        age_seconds=max(0.0, float(age_seconds)),
        status=status,
        record_count=max(0, int(record_count)),
        error_detail=error_detail,
    )


def is_stale(record: ProvenanceRecord, max_age_seconds: float) -> bool:
    """Evaluate whether a ProvenanceRecord exceeds its maximum allowable age."""
    if record.status != "ok":
        return True
    try:
        dt = datetime.fromisoformat(record.fetched_at_utc)
        current = datetime.now(timezone.utc)
        elapsed = (current - dt).total_seconds()
        return elapsed > max_age_seconds
    except Exception:
        return record.age_seconds > max_age_seconds
