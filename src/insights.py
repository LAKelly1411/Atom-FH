"""
Generates output/insights.html — analytics and trend overview.
All metrics are computed from the DB at pipeline time and embedded as JSON
in a self-contained static page rendered by Chart.js.
"""

import json
import logging
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .db import get_connection

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
OUTPUT_DIR    = Path(__file__).parent.parent / "output"

_SNAP = """
WITH latest AS (
    SELECT fhrs_id, MAX(pull_date) AS max_date
    FROM establishments GROUP BY fhrs_id
),
snap AS (
    SELECT e.*
    FROM establishments e
    JOIN latest ON e.fhrs_id = latest.fhrs_id AND e.pull_date = latest.max_date
)
"""


def _compute(conn) -> dict:
    # Current snapshot rating distribution — FHRS 0-5 only
    fhrs_dist = conn.execute(f"""
        {_SNAP}
        SELECT rating_value, COUNT(*) AS count
        FROM snap
        WHERE scheme_type = 'FHRS' AND rating_value IN ('0','1','2','3','4','5')
        GROUP BY rating_value
        ORDER BY CAST(rating_value AS INT)
    """).fetchall()

    # FHIS distribution
    fhis_dist = conn.execute(f"""
        {_SNAP}
        SELECT rating_value, COUNT(*) AS count
        FROM snap
        WHERE scheme_type = 'FHIS' AND rating_value IS NOT NULL AND rating_value != ''
        GROUP BY rating_value
    """).fetchall()

    # Weekly trend (last 90d): FHRS upgrades / downgrades + reinspections
    weekly = conn.execute("""
        SELECT
            strftime('%Y-W%W', change_date) AS week,
            MIN(change_date)               AS week_start,
            SUM(CASE WHEN change_type='rating_change'
                      AND old_rating IN ('0','1','2','3','4','5')
                      AND new_rating IN ('0','1','2','3','4','5')
                      AND CAST(new_rating AS INT) > CAST(old_rating AS INT)
                 THEN 1 ELSE 0 END)        AS upgrades,
            SUM(CASE WHEN change_type='rating_change'
                      AND old_rating IN ('0','1','2','3','4','5')
                      AND new_rating IN ('0','1','2','3','4','5')
                      AND CAST(new_rating AS INT) < CAST(old_rating AS INT)
                 THEN 1 ELSE 0 END)        AS downgrades,
            SUM(CASE WHEN change_type IN ('reinspection','score_change')
                 THEN 1 ELSE 0 END)        AS reinspections
        FROM change_log
        WHERE change_date >= date('now', '-90 days')
        GROUP BY week
        ORDER BY week
    """).fetchall()

    # Day-of-week activity pattern (last 90d, inspection events only)
    dow = conn.execute("""
        SELECT
            CAST(strftime('%w', change_date) AS INT) AS dow,
            COUNT(*) AS total
        FROM change_log
        WHERE change_date >= date('now', '-90 days')
          AND change_type IN ('rating_change', 'reinspection')
        GROUP BY dow
        ORDER BY dow
    """).fetchall()

    # Authority leaderboard — sorted worst first (lowest avg FHRS rating)
    authorities = conn.execute(f"""
        {_SNAP}
        SELECT
            local_authority_name,
            scheme_type,
            COUNT(*) AS total,
            SUM(CASE WHEN rating_value IN ('0','1')
                          OR rating_value = 'Improvement Required'
                     THEN 1 ELSE 0 END) AS low_rated,
            ROUND(AVG(CASE WHEN rating_value IN ('0','1','2','3','4','5')
                           THEN CAST(rating_value AS REAL) END), 2) AS avg_rating,
            ROUND(100.0 * SUM(CASE WHEN rating_value = 'Pass' THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(*), 0), 1) AS pass_pct
        FROM snap
        GROUP BY local_authority_name
        ORDER BY avg_rating ASC NULLS LAST, low_rated DESC
    """).fetchall()

    # Business type risk — top 15 by low-rating count (min 5 premises, at least 1 low)
    btype = conn.execute(f"""
        {_SNAP}
        SELECT
            business_type,
            COUNT(*) AS total,
            SUM(CASE WHEN rating_value IN ('0','1')
                          OR rating_value = 'Improvement Required'
                     THEN 1 ELSE 0 END) AS low_rated
        FROM snap
        WHERE business_type IS NOT NULL AND business_type != ''
        GROUP BY business_type
        HAVING COUNT(*) >= 5 AND low_rated > 0
        ORDER BY low_rated DESC, total DESC
        LIMIT 15
    """).fetchall()

    # Sub-score averages by FHRS authority (penalty scores — lower = better)
    scores = conn.execute(f"""
        {_SNAP}
        SELECT
            local_authority_name,
            ROUND(AVG(hygiene_score),    1) AS avg_hygiene,
            ROUND(AVG(structural_score), 1) AS avg_structural,
            ROUND(AVG(management_score), 1) AS avg_management,
            COUNT(*) AS total
        FROM snap
        WHERE scheme_type = 'FHRS' AND hygiene_score IS NOT NULL
        GROUP BY local_authority_name
        ORDER BY local_authority_name
    """).fetchall()

    # Summary totals
    totals = conn.execute(f"""
        {_SNAP}
        SELECT
            COUNT(*)  AS total_premises,
            COUNT(DISTINCT local_authority_name) AS total_las,
            SUM(CASE WHEN rating_value IN ('0','1')
                          OR rating_value = 'Improvement Required'
                     THEN 1 ELSE 0 END) AS total_low_rated
        FROM snap
    """).fetchone()

    inspections_30d = conn.execute("""
        SELECT COUNT(*) AS n FROM change_log
        WHERE change_date >= date('now', '-30 days')
          AND change_type IN ('rating_change', 'reinspection')
    """).fetchone()

    first_date = conn.execute(
        "SELECT MIN(change_date) AS first FROM change_log WHERE change_date > '2020-01-01'"
    ).fetchone()

    return {
        "fhrs_dist":       [dict(r) for r in fhrs_dist],
        "fhis_dist":       [dict(r) for r in fhis_dist],
        "weekly":          [dict(r) for r in weekly],
        "dow":             [dict(r) for r in dow],
        "authorities":     [dict(r) for r in authorities],
        "btype":           [dict(r) for r in btype],
        "scores":          [dict(r) for r in scores],
        "total_premises":  totals["total_premises"]  if totals else 0,
        "total_las":       totals["total_las"]       if totals else 0,
        "total_low_rated": totals["total_low_rated"] if totals else 0,
        "inspections_30d": inspections_30d["n"]      if inspections_30d else 0,
        "first_date":      first_date["first"]       if first_date else None,
    }


def render_insights(run_date: str) -> str:
    conn = get_connection()
    data = _compute(conn)
    conn.close()

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)

    def _commify(n):
        return f"{n:,}" if isinstance(n, int) else n

    env.filters["commify"] = _commify

    return env.get_template("insights.html").render(
        run_date=run_date,
        total_premises=  data["total_premises"],
        total_las=       data["total_las"],
        total_low_rated= data["total_low_rated"],
        inspections_30d= data["inspections_30d"],
        first_date=      data["first_date"],
        fhrs_dist_json=  json.dumps(data["fhrs_dist"]),
        fhis_dist_json=  json.dumps(data["fhis_dist"]),
        weekly_json=     json.dumps(data["weekly"]),
        dow_json=        json.dumps(data["dow"]),
        authorities_json=json.dumps(data["authorities"]),
        btype_json=      json.dumps(data["btype"]),
        scores_json=     json.dumps(data["scores"]),
    )


def save_insights_html(html: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "insights.html"
    path.write_text(html, encoding="utf-8")
    logger.info("Insights page saved to %s", path)
    return path


def run_insights_feed(run_date: str) -> Path:
    return save_insights_html(render_insights(run_date))
