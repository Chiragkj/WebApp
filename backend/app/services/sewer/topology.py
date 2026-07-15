"""Deterministic sewer topology normalization and audit.

This first phase does not silently move source geometry. It creates normalized
assets, measures endpoint-to-manhole distances in EPSG:32643, accepts only
high-confidence endpoint matches, and records unresolved topology as issues.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MANHOLE_CATEGORIES = ("manhole", "sewer manhole", "sewage manhole")
PIPE_CATEGORIES = ("sewage line", "sewer line", "sewer pipe", "sewage pipe")
AUTO_CONNECT_TOLERANCE_M = 1.0
REVIEW_TOLERANCE_M = 5.0
ENGINEERING_SRID = 32643


def _category_sql(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


async def build_topology_audit(db: AsyncSession, dataset_id: uuid.UUID) -> dict[str, Any]:
    """Normalize sewer assets and generate endpoint-connectivity findings."""
    # Re-running is intentionally idempotent for the dataset.
    await db.execute(
        text("DELETE FROM sewer_topology_issues WHERE dataset_id = :dataset_id"),
        {"dataset_id": dataset_id},
    )

    manhole_result = await db.execute(
        text(
            f"""
            INSERT INTO sewer_manholes (
                id, asset_id, dataset_id, source_feature_id,
                top_level_m, bottom_level_m, invert_level_m, depth_m, diameter_m,
                condition, pipe_type, topology_status, confidence_score,
                engineering_verified, source_attributes, geom, created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                'MH-' || upper(substr(replace(f.id::text, '-', ''), 1, 10)),
                f.dataset_id,
                f.id,
                NULLIF(COALESCE(f.attributes->>'Top_Level', f.attributes->>'top_level'), '')::double precision,
                NULLIF(COALESCE(f.attributes->>'Bottom_Level', f.attributes->>'bottom_level'), '')::double precision,
                NULLIF(COALESCE(f.attributes->>'Invert_Level', f.attributes->>'invert_level'), '')::double precision,
                NULLIF(COALESCE(f.attributes->>'Depth', f.attributes->>'depth'), '')::double precision,
                NULLIF(COALESCE(f.attributes->>'Diameter', f.attributes->>'diameter'), '')::double precision,
                COALESCE(f.attributes->>'Condition', f.attributes->>'condition'),
                COALESCE(f.attributes->>'Pipe_Type', f.attributes->>'pipe_type'),
                'normalized', 1.0, false, f.attributes,
                ST_PointOnSurface(f.geom), now(), now()
            FROM features f
            WHERE f.dataset_id = :dataset_id
              AND lower(trim(COALESCE(f.category, ''))) IN ({_category_sql(MANHOLE_CATEGORIES)})
            ON CONFLICT (dataset_id, source_feature_id) DO UPDATE SET
                source_attributes = EXCLUDED.source_attributes,
                geom = EXCLUDED.geom,
                updated_at = now()
            RETURNING id
            """
        ),
        {"dataset_id": dataset_id},
    )
    manholes_touched = len(manhole_result.fetchall())

    pipe_result = await db.execute(
        text(
            f"""
            INSERT INTO sewer_pipes (
                id, asset_id, dataset_id, source_feature_id, length_m,
                material, pipe_type, diameter_m, source_attributes, geom,
                topology_status, topology_confidence, flow_direction,
                engineering_verified, created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                'PIPE-' || upper(substr(replace(f.id::text, '-', ''), 1, 10)),
                f.dataset_id,
                f.id,
                ST_Length(ST_Transform(f.geom, {ENGINEERING_SRID})),
                COALESCE(f.attributes->>'Material', f.attributes->>'material'),
                COALESCE(f.attributes->>'Pipe_Type', f.attributes->>'pipe_type'),
                NULLIF(COALESCE(f.attributes->>'Diameter', f.attributes->>'diameter'), '')::double precision,
                f.attributes, f.geom,
                'pending_match', 0.0, 'unresolved', false, now(), now()
            FROM features f
            WHERE f.dataset_id = :dataset_id
              AND lower(trim(COALESCE(f.category, ''))) IN ({_category_sql(PIPE_CATEGORIES)})
              AND GeometryType(f.geom) IN ('LINESTRING', 'MULTILINESTRING')
            ON CONFLICT (dataset_id, source_feature_id) DO UPDATE SET
                length_m = EXCLUDED.length_m,
                source_attributes = EXCLUDED.source_attributes,
                geom = EXCLUDED.geom,
                topology_status = 'pending_match',
                topology_confidence = 0.0,
                upstream_manhole_id = NULL,
                downstream_manhole_id = NULL,
                updated_at = now()
            RETURNING id
            """
        ),
        {"dataset_id": dataset_id},
    )
    pipes_touched = len(pipe_result.fetchall())

    # Find the unique nearest manhole for each endpoint. Distances are metric.
    await db.execute(
        text(
            f"""
            WITH endpoint_candidates AS (
                SELECT
                    p.id AS pipe_id,
                    endpoint.endpoint_name,
                    nearest.manhole_id,
                    nearest.distance_m
                FROM sewer_pipes p
                CROSS JOIN LATERAL (
                    VALUES
                        ('start', ST_StartPoint(ST_LineMerge(p.geom))),
                        ('end', ST_EndPoint(ST_LineMerge(p.geom)))
                ) AS endpoint(endpoint_name, endpoint_geom)
                LEFT JOIN LATERAL (
                    SELECT
                        m.id AS manhole_id,
                        ST_Distance(
                            ST_Transform(endpoint.endpoint_geom, {ENGINEERING_SRID}),
                            ST_Transform(m.geom, {ENGINEERING_SRID})
                        ) AS distance_m
                    FROM sewer_manholes m
                    WHERE m.dataset_id = p.dataset_id
                    ORDER BY ST_Transform(endpoint.endpoint_geom, {ENGINEERING_SRID})
                             <-> ST_Transform(m.geom, {ENGINEERING_SRID})
                    LIMIT 1
                ) nearest ON true
                WHERE p.dataset_id = :dataset_id
            ), pivoted AS (
                SELECT
                    pipe_id,
                    max(manhole_id) FILTER (WHERE endpoint_name = 'start') AS start_manhole_id,
                    max(distance_m) FILTER (WHERE endpoint_name = 'start') AS start_distance_m,
                    max(manhole_id) FILTER (WHERE endpoint_name = 'end') AS end_manhole_id,
                    max(distance_m) FILTER (WHERE endpoint_name = 'end') AS end_distance_m
                FROM endpoint_candidates
                GROUP BY pipe_id
            )
            UPDATE sewer_pipes p
            SET
                upstream_manhole_id = CASE
                    WHEN x.start_distance_m <= :auto_tolerance THEN x.start_manhole_id
                    ELSE NULL END,
                downstream_manhole_id = CASE
                    WHEN x.end_distance_m <= :auto_tolerance THEN x.end_manhole_id
                    ELSE NULL END,
                start_snap_distance_m = x.start_distance_m,
                end_snap_distance_m = x.end_distance_m,
                topology_status = CASE
                    WHEN x.start_distance_m <= :auto_tolerance
                     AND x.end_distance_m <= :auto_tolerance
                     AND x.start_manhole_id IS DISTINCT FROM x.end_manhole_id
                        THEN 'connected_high_confidence'
                    WHEN x.start_distance_m <= :review_tolerance
                      OR x.end_distance_m <= :review_tolerance
                        THEN 'engineering_review'
                    ELSE 'floating'
                END,
                topology_confidence = CASE
                    WHEN x.start_distance_m <= :auto_tolerance
                     AND x.end_distance_m <= :auto_tolerance
                     AND x.start_manhole_id IS DISTINCT FROM x.end_manhole_id THEN 0.95
                    WHEN x.start_distance_m <= :review_tolerance
                      OR x.end_distance_m <= :review_tolerance THEN 0.50
                    ELSE 0.10
                END,
                updated_at = now()
            FROM pivoted x
            WHERE p.id = x.pipe_id
            """
        ),
        {
            "dataset_id": dataset_id,
            "auto_tolerance": AUTO_CONNECT_TOLERANCE_M,
            "review_tolerance": REVIEW_TOLERANCE_M,
        },
    )

    # Record endpoint issues without modifying source geometry.
    await db.execute(
        text(
            """
            INSERT INTO sewer_topology_issues (
                id, dataset_id, pipe_id, issue_type, severity, status,
                title, description, confidence_score, details, geom,
                created_at, updated_at
            )
            SELECT
                gen_random_uuid(), p.dataset_id, p.id,
                CASE WHEN endpoint.distance_m IS NULL OR endpoint.distance_m > :review_tolerance
                     THEN 'floating_endpoint' ELSE 'ambiguous_endpoint' END,
                CASE WHEN endpoint.distance_m IS NULL OR endpoint.distance_m > :review_tolerance
                     THEN 'high' ELSE 'medium' END,
                'open',
                initcap(endpoint.endpoint_name) || ' endpoint is not safely connected',
                'Nearest manhole distance exceeds the automatic connection tolerance.',
                CASE WHEN endpoint.distance_m IS NULL THEN 1.0 ELSE 0.8 END,
                jsonb_build_object(
                    'endpoint', endpoint.endpoint_name,
                    'nearest_manhole_distance_m', endpoint.distance_m,
                    'automatic_tolerance_m', :auto_tolerance,
                    'review_tolerance_m', :review_tolerance
                ),
                endpoint.endpoint_geom,
                now(), now()
            FROM sewer_pipes p
            CROSS JOIN LATERAL (
                VALUES
                    ('start', p.start_snap_distance_m, ST_StartPoint(ST_LineMerge(p.geom))),
                    ('end', p.end_snap_distance_m, ST_EndPoint(ST_LineMerge(p.geom)))
            ) AS endpoint(endpoint_name, distance_m, endpoint_geom)
            WHERE p.dataset_id = :dataset_id
              AND (endpoint.distance_m IS NULL OR endpoint.distance_m > :auto_tolerance)
            """
        ),
        {
            "dataset_id": dataset_id,
            "auto_tolerance": AUTO_CONNECT_TOLERANCE_M,
            "review_tolerance": REVIEW_TOLERANCE_M,
        },
    )

    await db.commit()
    return await topology_summary(db, dataset_id, manholes_touched, pipes_touched)


async def topology_summary(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    manholes_touched: int | None = None,
    pipes_touched: int | None = None,
) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM sewer_manholes WHERE dataset_id = :dataset_id) AS manholes,
                    (SELECT count(*) FROM sewer_pipes WHERE dataset_id = :dataset_id) AS pipes,
                    (SELECT count(*) FROM sewer_pipes WHERE dataset_id = :dataset_id
                        AND topology_status = 'connected_high_confidence') AS connected_high_confidence,
                    (SELECT count(*) FROM sewer_pipes WHERE dataset_id = :dataset_id
                        AND topology_status = 'engineering_review') AS engineering_review,
                    (SELECT count(*) FROM sewer_pipes WHERE dataset_id = :dataset_id
                        AND topology_status = 'floating') AS floating,
                    (SELECT count(*) FROM sewer_topology_issues WHERE dataset_id = :dataset_id
                        AND status = 'open') AS open_issues
                """
            ),
            {"dataset_id": dataset_id},
        )
    ).mappings().one()
    result = dict(row)
    result.update(
        {
            "dataset_id": str(dataset_id),
            "engineering_srid": ENGINEERING_SRID,
            "automatic_connection_tolerance_m": AUTO_CONNECT_TOLERANCE_M,
            "review_tolerance_m": REVIEW_TOLERANCE_M,
        }
    )
    if manholes_touched is not None:
        result["manholes_normalized_this_run"] = manholes_touched
    if pipes_touched is not None:
        result["pipes_normalized_this_run"] = pipes_touched
    return result
