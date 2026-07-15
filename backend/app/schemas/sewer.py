"""Pydantic schemas for the sewer connectivity engine."""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class SewerTopologyBuildRequest(BaseModel):
    """Optional controls for a topology-audit run.

    Phase 1 currently uses conservative fixed tolerances in the deterministic
    engine.  The request model is kept explicit so future approved engineering
    settings can be introduced without breaking the API contract.
    """

    force_rebuild: bool = Field(default=False)


class SewerTopologySummary(BaseModel):
    dataset_id: uuid.UUID
    engineering_srid: int
    automatic_connection_tolerance_m: float
    review_tolerance_m: float
    manholes: int
    pipes: int
    connected_high_confidence: int
    engineering_review: int
    floating: int
    open_issues: int
    manholes_normalized_this_run: int | None = None
    pipes_normalized_this_run: int | None = None


class SewerTopologyIssueOut(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    pipe_id: uuid.UUID | None
    manhole_id: uuid.UUID | None
    issue_type: str
    severity: str
    status: str
    title: str
    description: str | None
    confidence_score: float
    details: dict[str, Any]
    geometry: dict[str, Any] | None = None
