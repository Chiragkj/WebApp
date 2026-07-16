"""Deterministic sewer topology normalization and audit.

This phase does not silently move source geometry. It creates normalized
assets, measures endpoint-to-manhole distances in EPSG:32643, accepts only
high-confidence endpoint matches, and records unresolved topology as issues.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


MANHOLE_CATEGORIES = (
    "manhole",
    "sewer manhole",
    "sewage manhole",
)

PIPE_CATEGORIES = (
    "sewage line",
    "sewer line",
    "sewer pipe",
    "sewage pipe",
)

AUTO_CONNECT_TOLERANCE_M = 1.0
REVIEW_TOLERANCE_M = 5.0
ENGINEERING_SRID = 32643


def _category_sql(values: tuple[str, ...]) -> str:
    """Create a safely controlled SQL category list.

    The values are application constants, not user-provided values.
    """

    return ", ".join(f"'{value}'" for value in values)


def _metric_attribute_sql(*attribute_keys: str) -> str:
    """Return SQL that safely converts a JSON attribute into metres.

    Supported formats include:

    - 3 feet
    - 3 foot
    - 3 ft
    - 300 mm
    - 25 cm
    - 6 inches
    - 6 inch
    - 6 in
    - 0.9 m
    - plain numeric values

    Invalid or empty values become NULL. Raw values are still retained in
    source_attributes.
    """

    if not attribute_keys:
        raise ValueError("At least one attribute key must be supplied.")

    candidates = ", ".join(
        f"f.attributes->>'{key}'"
        for key in attribute_keys
    )

    raw_value = f"trim(COALESCE({candidates}, ''))"

    numeric_value = (
        "NULLIF("
        f"substring({raw_value} from '[-+]?[0-9]+(?:[.][0-9]+)?'), "
        "''"
        ")::double precision"
    )

    return f"""
        CASE
            WHEN {raw_value} = '' THEN NULL

            WHEN lower({raw_value}) IN (
                'null',
                'none',
                'n/a',
                'na',
                '-',
                '--',
                'unknown'
            )
                THEN NULL

            WHEN lower({raw_value}) ~
                 '(millimetre|millimetres|millimeter|millimeters|mm)'
                THEN {numeric_value} * 0.001

            WHEN lower({raw_value}) ~
                 '(centimetre|centimetres|centimeter|centimeters|cm)'
                THEN {numeric_value} * 0.01

            WHEN lower({raw_value}) ~
                 '(feet|foot|ft)'
                THEN {numeric_value} * 0.3048

            WHEN lower({raw_value}) ~
                 '(inches|inch)'
                THEN {numeric_value} * 0.0254

            WHEN lower({raw_value}) ~
                 '(^|[^a-z])in([^a-z]|$)'
                THEN {numeric_value} * 0.0254

            WHEN lower({raw_value}) ~
                 '(metre|metres|meter|meters)'
                THEN {numeric_value}

            WHEN lower({raw_value}) ~
                 '(^|[^a-z])m([^a-z]|$)'
                THEN {numeric_value}

            WHEN {raw_value} ~ '[-+]?[0-9]'
                THEN {numeric_value}

            ELSE NULL
        END
    """


async def build_topology_audit(
    db: AsyncSession,
    dataset_id: uuid.UUID,
) -> dict[str, Any]:
    """Normalize sewer assets and generate endpoint-connectivity findings."""

    # Re-running is intentionally idempotent for the dataset.
    await db.execute(
        text(
            """
            DELETE FROM sewer_topology_issues
            WHERE dataset_id = :dataset_id
            """
        ),
        {"dataset_id": dataset_id},
    )

    manhole_result = await db.execute(
        text(
            f"""
            INSERT INTO sewer_manholes (
                id,
                asset_id,
                dataset_id,
                source_feature_id,

                top_level_m,
                bottom_level_m,
                invert_level_m,
                depth_m,
                diameter_m,

                condition,
                pipe_type,

                incoming_pipe_count,
                outgoing_pipe_count,

                topology_status,
                confidence_score,
                engineering_verified,

                source_attributes,
                geom,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                'MH-' || upper(
                    substr(
                        replace(f.id::text, '-', ''),
                        1,
                        10
                    )
                ),
                f.dataset_id,
                f.id,

                {_metric_attribute_sql("Top_Level", "top_level")},
                {_metric_attribute_sql("Bottom_Level", "bottom_level")},
                {_metric_attribute_sql("Invert_Level", "invert_level")},
                {_metric_attribute_sql("Depth", "depth")},
                {_metric_attribute_sql("Diameter", "diameter")},

                COALESCE(
                    f.attributes->>'Condition',
                    f.attributes->>'condition'
                ),
                COALESCE(
                    f.attributes->>'Pipe_Type',
                    f.attributes->>'pipe_type'
                ),

                0,
                0,

                'normalized',
                1.0,
                false,

                f.attributes,
                ST_PointOnSurface(f.geom),
                now(),
                now()
            FROM features f
            WHERE f.dataset_id = :dataset_id
              AND lower(
                    trim(
                        COALESCE(f.category, '')
                    )
                  ) IN ({_category_sql(MANHOLE_CATEGORIES)})
            ON CONFLICT (dataset_id, source_feature_id)
            DO UPDATE SET
                top_level_m = EXCLUDED.top_level_m,
                bottom_level_m = EXCLUDED.bottom_level_m,
                invert_level_m = EXCLUDED.invert_level_m,
                depth_m = EXCLUDED.depth_m,
                diameter_m = EXCLUDED.diameter_m,
                condition = EXCLUDED.condition,
                pipe_type = EXCLUDED.pipe_type,
                incoming_pipe_count = 0,
                outgoing_pipe_count = 0,
                topology_status = 'normalized',
                confidence_score = 1.0,
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
                id,
                asset_id,
                dataset_id,
                source_feature_id,

                length_m,
                material,
                pipe_type,
                diameter_m,

                source_attributes,
                geom,

                topology_status,
                topology_confidence,
                flow_direction,
                engineering_verified,

                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                'PIPE-' || upper(
                    substr(
                        replace(f.id::text, '-', ''),
                        1,
                        10
                    )
                ),
                f.dataset_id,
                f.id,

                ST_Length(
                    ST_Transform(
                        f.geom,
                        {ENGINEERING_SRID}
                    )
                ),

                COALESCE(
                    f.attributes->>'Material',
                    f.attributes->>'material'
                ),

                COALESCE(
                    f.attributes->>'Pipe_Type',
                    f.attributes->>'pipe_type'
                ),

                {_metric_attribute_sql(
                    "Diameter",
                    "diameter",
                    "Pipe_Dia",
                    "pipe_dia",
                )},

                f.attributes,
                f.geom,

                'pending_match',
                0.0,
                'unresolved',
                false,

                now(),
                now()
            FROM features f
            WHERE f.dataset_id = :dataset_id
              AND lower(
                    trim(
                        COALESCE(f.category, '')
                    )
                  ) IN ({_category_sql(PIPE_CATEGORIES)})
              AND GeometryType(f.geom) IN (
                    'LINESTRING',
                    'MULTILINESTRING'
                  )
            ON CONFLICT (dataset_id, source_feature_id)
            DO UPDATE SET
                length_m = EXCLUDED.length_m,
                material = EXCLUDED.material,
                pipe_type = EXCLUDED.pipe_type,
                diameter_m = EXCLUDED.diameter_m,

                source_attributes = EXCLUDED.source_attributes,
                geom = EXCLUDED.geom,

                topology_status = 'pending_match',
                topology_confidence = 0.0,
                flow_direction = 'unresolved',

                upstream_manhole_id = NULL,
                downstream_manhole_id = NULL,

                start_snap_distance_m = NULL,
                end_snap_distance_m = NULL,

                updated_at = now()
            RETURNING id
            """
        ),
        {"dataset_id": dataset_id},
    )

    pipes_touched = len(pipe_result.fetchall())

    # Find the nearest manhole for the start and end of every pipe.
    # All distance calculations are performed in EPSG:32643.
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
                        (
                            'start',
                            ST_StartPoint(
                                ST_LineMerge(p.geom)
                            )
                        ),
                        (
                            'end',
                            ST_EndPoint(
                                ST_LineMerge(p.geom)
                            )
                        )
                ) AS endpoint(
                    endpoint_name,
                    endpoint_geom
                )

                LEFT JOIN LATERAL (
                    SELECT
                        m.id AS manhole_id,

                        ST_Distance(
                            ST_Transform(
                                endpoint.endpoint_geom,
                                {ENGINEERING_SRID}
                            ),
                            ST_Transform(
                                m.geom,
                                {ENGINEERING_SRID}
                            )
                        ) AS distance_m

                    FROM sewer_manholes m

                    WHERE m.dataset_id = p.dataset_id

                    ORDER BY
                        ST_Transform(
                            endpoint.endpoint_geom,
                            {ENGINEERING_SRID}
                        )
                        <->
                        ST_Transform(
                            m.geom,
                            {ENGINEERING_SRID}
                        )

                    LIMIT 1
                ) nearest ON true

                WHERE p.dataset_id = :dataset_id
            ),

            pivoted AS (
                SELECT
                    pipe_id,

                    (
                        array_agg(manhole_id)
                        FILTER (
                            WHERE endpoint_name = 'start'
                        )
                    )[1] AS start_manhole_id,

                    max(distance_m)
                        FILTER (
                            WHERE endpoint_name = 'start'
                        ) AS start_distance_m,

                    (
                        array_agg(manhole_id)
                        FILTER (
                            WHERE endpoint_name = 'end'
                        )
                    )[1] AS end_manhole_id,

                    max(distance_m)
                        FILTER (
                            WHERE endpoint_name = 'end'
                        ) AS end_distance_m

                FROM endpoint_candidates
                GROUP BY pipe_id
            )

            UPDATE sewer_pipes p
            SET
                upstream_manhole_id =
                    CASE
                        WHEN x.start_distance_m <= :auto_tolerance
                            THEN x.start_manhole_id
                        ELSE NULL
                    END,

                downstream_manhole_id =
                    CASE
                        WHEN x.end_distance_m <= :auto_tolerance
                            THEN x.end_manhole_id
                        ELSE NULL
                    END,

                start_snap_distance_m = x.start_distance_m,
                end_snap_distance_m = x.end_distance_m,

                topology_status =
                    CASE
                        WHEN
                            x.start_distance_m <= :auto_tolerance
                            AND x.end_distance_m <= :auto_tolerance
                            AND x.start_manhole_id
                                IS DISTINCT FROM
                                x.end_manhole_id
                        THEN 'connected_high_confidence'

                        WHEN
                            x.start_distance_m <= :review_tolerance
                            OR x.end_distance_m <= :review_tolerance
                        THEN 'engineering_review'

                        ELSE 'floating'
                    END,

                topology_confidence =
                    CASE
                        WHEN
                            x.start_distance_m <= :auto_tolerance
                            AND x.end_distance_m <= :auto_tolerance
                            AND x.start_manhole_id
                                IS DISTINCT FROM
                                x.end_manhole_id
                        THEN 0.95

                        WHEN
                            x.start_distance_m <= :review_tolerance
                            OR x.end_distance_m <= :review_tolerance
                        THEN 0.50

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

    # Record unresolved endpoints without moving source geometry.
    await db.execute(
        text(
            """
            INSERT INTO sewer_topology_issues (
                id,
                dataset_id,
                pipe_id,
                issue_type,
                severity,
                status,
                title,
                description,
                confidence_score,
                details,
                geom,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                p.dataset_id,
                p.id,

                CASE
                    WHEN
                        endpoint.distance_m IS NULL
                        OR endpoint.distance_m > CAST(:review_tolerance AS double precision)
                    THEN 'floating_endpoint'
                    ELSE 'ambiguous_endpoint'
                END,

                CASE
                    WHEN
                        endpoint.distance_m IS NULL
                        OR endpoint.distance_m > CAST(:review_tolerance AS double precision)
                    THEN 'high'
                    ELSE 'medium'
                END,

                'open',

                initcap(endpoint.endpoint_name)
                    || ' endpoint is not safely connected',

                'Nearest manhole distance exceeds the automatic '
                    || 'connection tolerance.',

                CASE
                    WHEN endpoint.distance_m IS NULL
                        THEN 1.0
                    ELSE 0.8
                END,

                jsonb_build_object(
                    'endpoint',
                    endpoint.endpoint_name,

                    'nearest_manhole_distance_m',
                    endpoint.distance_m,

                    'automatic_tolerance_m',
                    CAST(:auto_tolerance AS double precision),

                    'review_tolerance_m',
                    CAST(:review_tolerance AS double precision)
                ),
                endpoint.endpoint_geom,

                now(),
                now()

            FROM sewer_pipes p

            CROSS JOIN LATERAL (
                VALUES
                    (
                        'start',
                        p.start_snap_distance_m,
                        ST_StartPoint(
                            ST_LineMerge(p.geom)
                        )
                    ),
                    (
                        'end',
                        p.end_snap_distance_m,
                        ST_EndPoint(
                            ST_LineMerge(p.geom)
                        )
                    )
            ) AS endpoint(
                endpoint_name,
                distance_m,
                endpoint_geom
            )

            WHERE p.dataset_id = :dataset_id
              AND (
                    endpoint.distance_m IS NULL
                    OR endpoint.distance_m > CAST(:auto_tolerance AS double precision)
                  )
            """
        ),
        {
            "dataset_id": dataset_id,
            "auto_tolerance": AUTO_CONNECT_TOLERANCE_M,
            "review_tolerance": REVIEW_TOLERANCE_M,
        },
    )

    # Refresh incoming and outgoing pipe counts after matching.
    await db.execute(
        text(
            """
            UPDATE sewer_manholes m
            SET
                incoming_pipe_count = counts.incoming_count,
                outgoing_pipe_count = counts.outgoing_count,
                updated_at = now()
            FROM (
                SELECT
                    manhole.id AS manhole_id,

                    count(pipe.id)
                        FILTER (
                            WHERE pipe.downstream_manhole_id = manhole.id
                        ) AS incoming_count,

                    count(pipe.id)
                        FILTER (
                            WHERE pipe.upstream_manhole_id = manhole.id
                        ) AS outgoing_count

                FROM sewer_manholes manhole

                LEFT JOIN sewer_pipes pipe
                    ON pipe.dataset_id = manhole.dataset_id
                   AND (
                        pipe.upstream_manhole_id = manhole.id
                        OR pipe.downstream_manhole_id = manhole.id
                   )

                WHERE manhole.dataset_id = :dataset_id

                GROUP BY manhole.id
            ) counts
            WHERE m.id = counts.manhole_id
            """
        ),
        {"dataset_id": dataset_id},
    )

    await db.commit()

    return await topology_summary(
        db,
        dataset_id,
        manholes_touched,
        pipes_touched,
    )


async def topology_summary(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    manholes_touched: int | None = None,
    pipes_touched: int | None = None,
) -> dict[str, Any]:
    """Return topology build and validation counts."""

    row = (
        await db.execute(
            text(
                """
                SELECT
                    (
                        SELECT count(*)
                        FROM sewer_manholes
                        WHERE dataset_id = :dataset_id
                    ) AS manholes,

                    (
                        SELECT count(*)
                        FROM sewer_pipes
                        WHERE dataset_id = :dataset_id
                    ) AS pipes,

                    (
                        SELECT count(*)
                        FROM sewer_pipes
                        WHERE dataset_id = :dataset_id
                          AND topology_status =
                              'connected_high_confidence'
                    ) AS connected_high_confidence,

                    (
                        SELECT count(*)
                        FROM sewer_pipes
                        WHERE dataset_id = :dataset_id
                          AND topology_status =
                              'engineering_review'
                    ) AS engineering_review,

                    (
                        SELECT count(*)
                        FROM sewer_pipes
                        WHERE dataset_id = :dataset_id
                          AND topology_status = 'floating'
                    ) AS floating,

                    (
                        SELECT count(*)
                        FROM sewer_topology_issues
                        WHERE dataset_id = :dataset_id
                          AND status = 'open'
                    ) AS open_issues
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
            "automatic_connection_tolerance_m": (
                AUTO_CONNECT_TOLERANCE_M
            ),
            "review_tolerance_m": REVIEW_TOLERANCE_M,
        }
    )

    if manholes_touched is not None:
        result["manholes_normalized_this_run"] = (
            manholes_touched
        )

    if pipes_touched is not None:
        result["pipes_normalized_this_run"] = (
            pipes_touched
        )

    return result