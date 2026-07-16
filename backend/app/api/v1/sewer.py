"""Sewer Connectivity Engine API.

Mounted under:

    /api/v1/sewer

Phase 1 exposes deterministic topology normalization, topology summaries,
and topology-review findings. It does not silently modify source geometries.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from app.services.sewer.network_validator import validate_sewer_network
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_any
from app.db.session import get_db
from app.models import Dataset
from app.schemas.sewer import (
    SewerTopologyBuildRequest,
    SewerTopologyIssueOut,
    SewerTopologySummary,
)
from app.services.sewer.topology import build_topology_audit, topology_summary
from app.services.sewer.graph_queries import (
    get_connected_component,
    get_direct_connections,
    get_downstream_network,
    get_graph_summary,
    get_upstream_network,
)
router = APIRouter()


async def _require_dataset(
    dataset_id: uuid.UUID,
    db: AsyncSession,
) -> Dataset:
    """Return the dataset or raise a clean API-level 404."""

    result = await db.execute(
        select(Dataset).where(Dataset.id == dataset_id)
    )
    dataset = result.scalar_one_or_none()

    if dataset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dataset {dataset_id} was not found.",
        )

    return dataset

@router.post(
    "/datasets/{dataset_id}/validate",
    dependencies=[Depends(require_any)],
    summary="Validate normalized sewer network",
)
async def validate_dataset_sewer_network(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run deterministic QA/QC rules against the normalized sewer graph."""

    await _require_dataset(dataset_id, db)

    try:
        return await validate_sewer_network(
            db,
            dataset_id,
        )
    except Exception as exc:
        await db.rollback()

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sewer network validation failed: {exc}",
        ) from exc

@router.get(
    "/datasets/{dataset_id}/graph-summary",
    dependencies=[Depends(require_any)],
    summary="Get sewer graph summary",
)
async def get_dataset_graph_summary(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require_dataset(dataset_id, db)

    return await get_graph_summary(
        db,
        dataset_id,
    )


@router.get(
    "/datasets/{dataset_id}/manholes/{manhole_id}/connections",
    dependencies=[Depends(require_any)],
    summary="Get direct manhole pipe connections",
)
async def get_manhole_connections(
    dataset_id: uuid.UUID,
    manhole_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require_dataset(dataset_id, db)

    result = await get_direct_connections(
        db,
        dataset_id,
        manhole_id,
    )

    if not result["found"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Normalized sewer manhole was not found.",
        )

    return result


@router.get(
    "/datasets/{dataset_id}/manholes/{manhole_id}/upstream",
    dependencies=[Depends(require_any)],
    summary="Trace upstream sewer network",
)
async def trace_manhole_upstream(
    dataset_id: uuid.UUID,
    manhole_id: uuid.UUID,
    max_depth: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require_dataset(dataset_id, db)

    return await get_upstream_network(
        db,
        dataset_id,
        manhole_id,
        max_depth=max_depth,
    )


@router.get(
    "/datasets/{dataset_id}/manholes/{manhole_id}/downstream",
    dependencies=[Depends(require_any)],
    summary="Trace downstream sewer network",
)
async def trace_manhole_downstream(
    dataset_id: uuid.UUID,
    manhole_id: uuid.UUID,
    max_depth: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require_dataset(dataset_id, db)

    return await get_downstream_network(
        db,
        dataset_id,
        manhole_id,
        max_depth=max_depth,
    )


@router.get(
    "/datasets/{dataset_id}/manholes/{manhole_id}/component",
    dependencies=[Depends(require_any)],
    summary="Get connected sewer component",
)
async def get_manhole_component(
    dataset_id: uuid.UUID,
    manhole_id: uuid.UUID,
    max_depth: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require_dataset(dataset_id, db)

    return await get_connected_component(
        db,
        dataset_id,
        manhole_id,
        max_depth=max_depth,
    )

@router.post(
    "/datasets/{dataset_id}/build-topology",
    response_model=SewerTopologySummary,
    dependencies=[Depends(require_any)],
    summary="Build or rebuild deterministic sewer topology",
)
async def build_dataset_topology(
    dataset_id: uuid.UUID,
    payload: SewerTopologyBuildRequest,
    db: AsyncSession = Depends(get_db),
) -> SewerTopologySummary:
    """Normalize manholes and sewage lines and run the topology audit.

    Current Phase 1 behaviour:

    - reads source Manhole and Sewage Line features,
    - preserves the source feature geometry,
    - calculates pipe lengths and snap distances in EPSG:32643,
    - automatically connects only endpoints within the conservative tolerance,
    - stores ambiguous and floating endpoints as topology issues.

    The ``force_rebuild`` property is accepted for forward compatibility.
    The current process is already idempotent and refreshes normalized rows.
    """

    await _require_dataset(dataset_id, db)

    try:
        result = await build_topology_audit(db, dataset_id)
        return SewerTopologySummary.model_validate(result)
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sewer topology build failed: {exc}",
        ) from exc


@router.get(
    "/datasets/{dataset_id}/topology-summary",
    response_model=SewerTopologySummary,
    dependencies=[Depends(require_any)],
    summary="Get sewer topology summary",
)
async def get_dataset_topology_summary(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SewerTopologySummary:
    """Return counts for normalized assets and unresolved topology."""

    await _require_dataset(dataset_id, db)

    try:
        result = await topology_summary(db, dataset_id)
        return SewerTopologySummary.model_validate(result)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unable to load sewer topology summary: {exc}",
        ) from exc


@router.get(
    "/datasets/{dataset_id}/issues",
    response_model=list[SewerTopologyIssueOut],
    dependencies=[Depends(require_any)],
    summary="List sewer topology issues",
)
async def list_dataset_topology_issues(
    dataset_id: uuid.UUID,
    issue_type: str | None = Query(default=None, max_length=64),
    severity: str | None = Query(default=None, pattern="^(low|medium|high)$"),
    issue_status: str | None = Query(
        default=None,
        alias="status",
        pattern="^(open|reviewing|resolved|rejected)$",
    ),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[SewerTopologyIssueOut]:
    """Return topology findings with optional filters."""

    await _require_dataset(dataset_id, db)

    conditions = ["dataset_id = :dataset_id"]
    params: dict[str, Any] = {
        "dataset_id": dataset_id,
        "limit": limit,
        "offset": offset,
    }

    if issue_type:
        conditions.append("issue_type = :issue_type")
        params["issue_type"] = issue_type

    if severity:
        conditions.append("severity = :severity")
        params["severity"] = severity

    if issue_status:
        conditions.append("status = :issue_status")
        params["issue_status"] = issue_status

    result = await db.execute(
        text(
            f"""
            SELECT
                id,
                dataset_id,
                pipe_id,
                manhole_id,
                issue_type,
                severity,
                status,
                title,
                description,
                confidence_score,
                details,
                CASE
                    WHEN geom IS NULL THEN NULL
                    ELSE ST_AsGeoJSON(geom)::json
                END AS geometry
            FROM sewer_topology_issues
            WHERE {" AND ".join(conditions)}
            ORDER BY
                CASE severity
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    ELSE 3
                END,
                created_at DESC
            LIMIT :limit
            OFFSET :offset
            """
        ),
        params,
    )

    rows = result.mappings().all()

    return [
        SewerTopologyIssueOut(
            id=row["id"],
            dataset_id=row["dataset_id"],
            pipe_id=row["pipe_id"],
            manhole_id=row["manhole_id"],
            issue_type=row["issue_type"],
            severity=row["severity"],
            status=row["status"],
            title=row["title"],
            description=row["description"],
            confidence_score=row["confidence_score"],
            details=row["details"] or {},
            geometry=_normalize_geometry(row["geometry"]),
        )
        for row in rows
    ]


@router.get(
    "/datasets/{dataset_id}/network",
    dependencies=[Depends(require_any)],
    summary="Get normalized sewer network as GeoJSON",
)
async def get_dataset_sewer_network(
    dataset_id: uuid.UUID,
    include_manholes: bool = Query(default=True),
    include_pipes: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return normalized manholes and pipes as a GeoJSON FeatureCollection."""

    await _require_dataset(dataset_id, db)

    features: list[dict[str, Any]] = []

    if include_manholes:
        manhole_result = await db.execute(
            text(
                """
                SELECT
                    id,
                    asset_id,
                    topology_status,
                    confidence_score,
                    engineering_verified,
                    top_level_m,
                    bottom_level_m,
                    invert_level_m,
                    depth_m,
                    diameter_m,
                    condition,
                    pipe_type,
                    incoming_pipe_count,
                    outgoing_pipe_count,
                    ST_AsGeoJSON(geom)::json AS geometry
                FROM sewer_manholes
                WHERE dataset_id = :dataset_id
                ORDER BY asset_id
                """
            ),
            {"dataset_id": dataset_id},
        )

        for row in manhole_result.mappings():
            features.append(
                {
                    "type": "Feature",
                    "id": str(row["id"]),
                    "geometry": _normalize_geometry(row["geometry"]),
                    "properties": {
                        "domain": "sewer",
                        "asset_type": "manhole",
                        "asset_id": row["asset_id"],
                        "topology_status": row["topology_status"],
                        "confidence_score": row["confidence_score"],
                        "engineering_verified": row["engineering_verified"],
                        "top_level_m": row["top_level_m"],
                        "bottom_level_m": row["bottom_level_m"],
                        "invert_level_m": row["invert_level_m"],
                        "depth_m": row["depth_m"],
                        "diameter_m": row["diameter_m"],
                        "condition": row["condition"],
                        "pipe_type": row["pipe_type"],
                        "incoming_pipe_count": row["incoming_pipe_count"],
                        "outgoing_pipe_count": row["outgoing_pipe_count"],
                    },
                }
            )

    if include_pipes:
        pipe_result = await db.execute(
            text(
                """
                SELECT
                    p.id,
                    p.asset_id,
                    p.upstream_manhole_id,
                    p.downstream_manhole_id,
                    upstream.asset_id AS upstream_manhole_asset_id,
                    downstream.asset_id AS downstream_manhole_asset_id,
                    p.material,
                    p.pipe_type,
                    p.diameter_m,
                    p.length_m,
                    p.slope,
                    p.capacity_lps,
                    p.actual_flow_lps,
                    p.capacity_utilization_pct,
                    p.start_snap_distance_m,
                    p.end_snap_distance_m,
                    p.flow_direction,
                    p.topology_status,
                    p.topology_confidence,
                    p.engineering_verified,
                    ST_AsGeoJSON(p.geom)::json AS geometry
                FROM sewer_pipes p
                LEFT JOIN sewer_manholes upstream
                    ON upstream.id = p.upstream_manhole_id
                LEFT JOIN sewer_manholes downstream
                    ON downstream.id = p.downstream_manhole_id
                WHERE p.dataset_id = :dataset_id
                ORDER BY p.asset_id
                """
            ),
            {"dataset_id": dataset_id},
        )

        for row in pipe_result.mappings():
            features.append(
                {
                    "type": "Feature",
                    "id": str(row["id"]),
                    "geometry": _normalize_geometry(row["geometry"]),
                    "properties": {
                        "domain": "sewer",
                        "asset_type": "pipe",
                        "asset_id": row["asset_id"],
                        "upstream_manhole_id": _uuid_text(
                            row["upstream_manhole_id"]
                        ),
                        "downstream_manhole_id": _uuid_text(
                            row["downstream_manhole_id"]
                        ),
                        "upstream_manhole_asset_id": row[
                            "upstream_manhole_asset_id"
                        ],
                        "downstream_manhole_asset_id": row[
                            "downstream_manhole_asset_id"
                        ],
                        "material": row["material"],
                        "pipe_type": row["pipe_type"],
                        "diameter_m": row["diameter_m"],
                        "length_m": row["length_m"],
                        "slope": row["slope"],
                        "capacity_lps": row["capacity_lps"],
                        "actual_flow_lps": row["actual_flow_lps"],
                        "capacity_utilization_pct": row[
                            "capacity_utilization_pct"
                        ],
                        "start_snap_distance_m": row[
                            "start_snap_distance_m"
                        ],
                        "end_snap_distance_m": row[
                            "end_snap_distance_m"
                        ],
                        "flow_direction": row["flow_direction"],
                        "topology_status": row["topology_status"],
                        "topology_confidence": row[
                            "topology_confidence"
                        ],
                        "engineering_verified": row[
                            "engineering_verified"
                        ],
                    },
                }
            )

    return {
        "type": "FeatureCollection",
        "dataset_id": str(dataset_id),
        "features": features,
    }


def _normalize_geometry(value: Any) -> dict[str, Any] | None:
    """Normalize asyncpg/PostGIS JSON output into a Python dictionary."""

    if value is None:
        return None

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        return json.loads(value)

    return dict(value)


def _uuid_text(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None