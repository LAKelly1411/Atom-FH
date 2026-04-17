"""
Milestone 4/5: Alert Rule Engine
Evaluates configured alert rules against today's change log and writes
triggered alerts to the anomaly_flags table.

Rule types:
  zero_rating   — any premises gets rating 0 or "Improvement Required"
  threshold     — rating drops by N or more stars
  business_type — premises type matches keywords (school, hospital, etc.)
  watchlist     — specific FHRS IDs to monitor closely
"""

import json
import logging
from datetime import date
from pathlib import Path

import yaml

from .db import get_connection
from .detect import _is_low_rating, _rating_numeric

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "alert_rules.yaml"


def load_rules() -> list[dict]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f).get("rules", [])


def _matches_scope(change: dict, rule: dict) -> bool:
    """Return True if the change falls within the rule's region/authority scope."""
    regions     = rule.get("regions", [])
    authorities = rule.get("authorities", [])
    if regions and change.get("region") not in regions:
        return False
    if authorities and change.get("local_authority_name") not in authorities:
        return False
    return True


def _evaluate_rule(rule: dict, changes: list[dict]) -> list[dict]:
    """Return all changes that trigger this rule."""
    rule_type = rule["type"]
    triggered = []

    for c in changes:
        if not _matches_scope(c, rule):
            continue

        if rule_type == "zero_rating":
            rating = (c.get("new_rating") or "").strip()
            scheme = c.get("scheme_type", "FHRS")
            if _is_low_rating(rating, scheme):
                triggered.append(c)

        elif rule_type == "threshold":
            min_drop = rule.get("min_drop", 2)
            old_n = _rating_numeric(c.get("old_rating"))
            new_n = _rating_numeric(c.get("new_rating"))
            if old_n is not None and new_n is not None and (old_n - new_n) >= min_drop:
                triggered.append(c)

        elif rule_type == "business_type":
            keywords = [k.lower() for k in rule.get("business_type_keywords", [])]
            btype = (c.get("business_type") or "").lower()
            if any(kw in btype for kw in keywords):
                triggered.append(c)

        elif rule_type == "watchlist":
            ids = [str(i) for i in rule.get("fhrs_ids", [])]
            if ids and c.get("fhrs_id") in ids:
                triggered.append(c)

    return triggered


def evaluate_rules(changes: list[dict], run_date: str | None = None) -> list[dict]:
    """
    Evaluate all active rules against `changes`.
    Returns list of alert dicts (not yet persisted).
    """
    if run_date is None:
        run_date = date.today().isoformat()

    alerts = []
    for rule in load_rules():
        if not rule.get("active", False):
            continue

        triggered = _evaluate_rule(rule, changes)
        if not triggered:
            continue

        n = len(triggered)
        if n == 1:
            headline = f"{rule['name']}: {triggered[0]['business_name']}"
        else:
            headline = f"{rule['name']}: {n} premises affected"

        alerts.append({
            "flag_date":      run_date,
            "detector_name":  rule["name"],
            "rule_type":      rule["type"],
            "headline":       headline,
            "plain_english":  rule.get("description", ""),
            "raw_data":       json.dumps(triggered, default=str),
            "confidence":     "high",
            "count":          n,
            "triggered":      triggered,
        })

    return alerts


def run_alerts(changes: list[dict], run_date: str | None = None) -> list[dict]:
    """
    Main alert entry point.
    Evaluates rules, persists new alerts to DB, returns alert list.
    Idempotent — skips write if alerts for this date already exist.
    """
    if run_date is None:
        run_date = date.today().isoformat()

    alerts = evaluate_rules(changes, run_date)

    conn = get_connection()
    existing = conn.execute(
        "SELECT COUNT(*) FROM anomaly_flags WHERE flag_date = ?", (run_date,)
    ).fetchone()[0]

    if not existing:
        for a in alerts:
            conn.execute("""
                INSERT INTO anomaly_flags
                  (flag_date, detector_name, headline, plain_english, raw_data, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (a["flag_date"], a["detector_name"], a["headline"],
                  a["plain_english"], a["raw_data"], a["confidence"]))
        conn.commit()
        logger.info("Persisted %s alert(s) for %s", len(alerts), run_date)
    else:
        logger.info("Alerts for %s already exist (%s), skipping write",
                    run_date, existing)
    conn.close()

    return alerts


def get_historical_alerts(days: int = 90) -> list[dict]:
    """Return all alerts from the DB within the last `days` days, newest first."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, flag_date, detector_name, headline, plain_english,
               raw_data, confidence, dismissed, dismissed_note
        FROM anomaly_flags
        WHERE flag_date >= date('now', ?)
        ORDER BY flag_date DESC, id DESC
    """, (f"-{days} days",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
