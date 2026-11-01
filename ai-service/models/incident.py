from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Alert(BaseModel):
    status: str
    labels: dict[str, str]
    annotations: dict[str, str]
    startsAt: datetime
    endsAt: datetime | None = None
    generatorURL: str | None = None
    fingerprint: str | None = None

class RootCauseAnalysis(BaseModel):
    root_cause: str
    impact: str
    recommended_fix: str
    confidence_score: float = Field(ge=0, le=100)
    additional_context: dict[str, Any] | None = None
    analyzed_at: datetime | None = None

class IncidentStatus(str, Enum):
    INVESTIGATING = "investigating"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    ANALYSIS_FAILED = "analysis_failed"
    REMEDIATING = "remediating"
    REMEDIATION_FAILED = "remediation_failed"
    RESOLVED = "resolved"

class Incident(BaseModel):
    id: str
    alert: Alert
    status: IncidentStatus = IncidentStatus.INVESTIGATING
    rca: RootCauseAnalysis | None = None
    remediation: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime | None = None
    resolved_at: datetime | None = None
    assigned_to: str | None = None
    notes: str | None = None
