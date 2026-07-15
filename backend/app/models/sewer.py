"""Normalized sewer-network domain models.

The generic ``features`` table remains the immutable source of imported GIS
content.  These tables add engineering-ready assets and topology findings while
preserving lineage back to the source feature and dataset.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._mixins import created_at_col, updated_at_col, uuid_pk


class SewerManhole(Base):
    __tablename__ = "sewer_manholes"
    __table_args__ = (
        UniqueConstraint("dataset_id", "source_feature_id", name="uq_sewer_manhole_source"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_feature_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("features.id", ondelete="CASCADE"), nullable=False, index=True
    )

    top_level_m: Mapped[float | None] = mapped_column(Float)
    bottom_level_m: Mapped[float | None] = mapped_column(Float)
    invert_level_m: Mapped[float | None] = mapped_column(Float)
    depth_m: Mapped[float | None] = mapped_column(Float)
    diameter_m: Mapped[float | None] = mapped_column(Float)
    condition: Mapped[str | None] = mapped_column(String(128))
    pipe_type: Mapped[str | None] = mapped_column(String(128))

    incoming_pipe_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outgoing_pipe_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    topology_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unprocessed", index=True)
    engineering_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source_attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    geom = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class SewerPipe(Base):
    __tablename__ = "sewer_pipes"
    __table_args__ = (
        UniqueConstraint("dataset_id", "source_feature_id", name="uq_sewer_pipe_source"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_feature_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("features.id", ondelete="CASCADE"), nullable=False, index=True
    )

    upstream_manhole_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sewer_manholes.id", ondelete="SET NULL"), index=True
    )
    downstream_manhole_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sewer_manholes.id", ondelete="SET NULL"), index=True
    )

    material: Mapped[str | None] = mapped_column(String(128))
    pipe_type: Mapped[str | None] = mapped_column(String(128))
    diameter_m: Mapped[float | None] = mapped_column(Float)
    roughness_n: Mapped[float | None] = mapped_column(Float)
    length_m: Mapped[float | None] = mapped_column(Float)
    upstream_invert_m: Mapped[float | None] = mapped_column(Float)
    downstream_invert_m: Mapped[float | None] = mapped_column(Float)
    slope: Mapped[float | None] = mapped_column(Float)
    capacity_lps: Mapped[float | None] = mapped_column(Float)
    actual_flow_lps: Mapped[float | None] = mapped_column(Float)
    capacity_utilization_pct: Mapped[float | None] = mapped_column(Float)

    start_snap_distance_m: Mapped[float | None] = mapped_column(Float)
    end_snap_distance_m: Mapped[float | None] = mapped_column(Float)
    flow_direction: Mapped[str] = mapped_column(String(32), nullable=False, default="unresolved")
    topology_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unprocessed", index=True)
    topology_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    engineering_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    geom = mapped_column(Geometry("GEOMETRY", srid=4326, spatial_index=False), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class SewerTopologyIssue(Base):
    __tablename__ = "sewer_topology_issues"

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pipe_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sewer_pipes.id", ondelete="CASCADE"), index=True
    )
    manhole_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sewer_manholes.id", ondelete="CASCADE"), index=True
    )
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    geom = mapped_column(Geometry("GEOMETRY", srid=4326, spatial_index=False), nullable=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
