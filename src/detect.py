"""
Milestone 2: Change Detection
Diffs today's pull against the most recent previous pull for each authority.
Produces a JSON change log — the atomic unit consumed by all three layers.

Change types:
  new_establishment   — FHRS ID not seen before
  rating_change       — same ID, different rating value
  reinspection        — same ID, same rating, new rating date
  score_change        — same ID, same rating, same date, sub-scores shifted
"""

import json
import logging
from collections import defaultdict
from datetime import date
from pathlib import Path

from .db import (get_connection, get_establishments_for_date,
                 get_changes_for_date, insert_change, get_latest_pull_date)

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent / "output"


# ── Rating utilities ──────────────────────────────────────────────────────────

FHRS_NUMERIC = {"0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}

def _rating_numeric(rating: str | None) -> int | None:
    """Convert a rating string to int where possible (FHRS only)."""
    if rating is None:
        return None
    return FHRS_NUMERIC.get(str(rating).strip())


def _is_low_rating(rating: str | None, scheme: str) -> bool:
    """Return True for newsworthy low/fail ratings."""
    if not rating:
        return False
    if scheme == "FHIS":
        return rating.strip().lower() in ("improvement required",)
    num = _rating_numeric(rating)
    return num is not None and num <= 1


def _build_address(row) -> str:
    parts = [
        row["address_line1"],
        row["address_line2"],
        row["address_line3"],
        row["address_line4"],
    ]
    return ", ".join(p for p in parts if p)


# ── Core diff logic ───────────────────────────────────────────────────────────

def _diff_authority(conn, la_id: int, today_date: str,
                    prev_date: str) -> list[dict]:
    """Compare today's and yesterday's records for one authority."""
    today_rows = get_establishments_for_date(conn, la_id, today_date)
    prev_rows = get_establishments_for_date(conn, la_id, prev_date)

    today_by_id = {r["fhrs_id"]: r for r in today_rows}
    prev_by_id = {r["fhrs_id"]: r for r in prev_rows}

    changes = []

    for fhrs_id, today in today_by_id.items():
        base = {
            "change_date": today_date,
            "fhrs_id": fhrs_id,
            "business_name": today["business_name"],
            "business_type": today["business_type"],
            "address": _build_address(today),
            "postcode": today["postcode"],
            "local_authority_id": today["local_authority_id"],
            "local_authority_name": today["local_authority_name"],
            "scheme_type": today["scheme_type"],
            "region": today["region"],
            "old_rating": None,
            "new_rating": today["rating_value"],
            "old_rating_date": None,
            "new_rating_date": today["rating_date"],
            "old_hygiene": None,
            "new_hygiene": today["hygiene_score"],
            "old_structural": None,
            "new_structural": today["structural_score"],
            "old_management": None,
            "new_management": today["management_score"],
        }

        if fhrs_id not in prev_by_id:
            # Brand-new establishment
            changes.append({**base, "change_type": "new_establishment"})
            continue

        prev = prev_by_id[fhrs_id]
        base["old_rating"] = prev["rating_value"]
        base["old_rating_date"] = prev["rating_date"]
        base["old_hygiene"] = prev["hygiene_score"]
        base["old_structural"] = prev["structural_score"]
        base["old_management"] = prev["management_score"]

        today_rating = (today["rating_value"] or "").strip()
        prev_rating = (prev["rating_value"] or "").strip()
        today_date_val = (today["rating_date"] or "").strip()
        prev_date_val = (prev["rating_date"] or "").strip()

        if today_rating != prev_rating:
            changes.append({**base, "change_type": "rating_change"})
        elif today_date_val != prev_date_val:
            changes.append({**base, "change_type": "reinspection"})
        elif (today["hygiene_score"] != prev["hygiene_score"] or
              today["structural_score"] != prev["structural_score"] or
              today["management_score"] != prev["management_score"]):
            changes.append({**base, "change_type": "score_change"})

    return changes


# ── Public API ────────────────────────────────────────────────────────────────

def run_change_detection(today_date: str | None = None) -> list[dict]:
    """
    Diff today's pull against the most recent previous pull for every authority.
    Writes changes to the DB and returns the full change list as dicts.
    """
    if today_date is None:
        today_date = date.today().isoformat()

    conn = get_connection()
    all_changes = []

    # Get all authorities that have data for today
    rows = conn.execute("""
        SELECT DISTINCT local_authority_id, local_authority_name
        FROM establishments
        WHERE pull_date = ?
        ORDER BY local_authority_name
    """, (today_date,)).fetchall()

    if not rows:
        logger.warning("No establishments found for %s — has ingestion run?",
                       today_date)
        conn.close()
        return []

    for row in rows:
        la_id = row["local_authority_id"]
        la_name = row["local_authority_name"]

        # Find most recent previous pull for this authority
        prev_row = conn.execute("""
            SELECT MAX(pull_date) as prev_date FROM establishments
            WHERE local_authority_id = ? AND pull_date < ?
        """, (la_id, today_date)).fetchone()

        prev_date = prev_row["prev_date"] if prev_row else None

        if not prev_date:
            # First ever pull — log all as new establishments
            logger.info("%s: first pull, marking all as new establishments",
                        la_name)
            today_rows = get_establishments_for_date(conn, la_id, today_date)
            for r in today_rows:
                change = {
                    "change_date": today_date,
                    "fhrs_id": r["fhrs_id"],
                    "change_type": "new_establishment",
                    "business_name": r["business_name"],
                    "business_type": r["business_type"],
                    "address": _build_address(r),
                    "postcode": r["postcode"],
                    "local_authority_id": r["local_authority_id"],
                    "local_authority_name": r["local_authority_name"],
                    "scheme_type": r["scheme_type"],
                    "region": r["region"],
                    "old_rating": None,
                    "new_rating": r["rating_value"],
                    "old_rating_date": None,
                    "new_rating_date": r["rating_date"],
                    "old_hygiene": None,
                    "new_hygiene": r["hygiene_score"],
                    "old_structural": None,
                    "new_structural": r["structural_score"],
                    "old_management": None,
                    "new_management": r["management_score"],
                }
                all_changes.append(change)
            logger.info("%s: %s new establishments on first pull", la_name,
                        len(today_rows))
            continue

        logger.info("Diffing %s: %s vs %s", la_name, today_date, prev_date)
        changes = _diff_authority(conn, la_id, today_date, prev_date)
        logger.info("%s: %s changes detected", la_name, len(changes))
        all_changes.extend(changes)

    # Persist to DB (skip if changes for this date already exist)
    existing = get_changes_for_date(conn, today_date)
    if not existing:
        for change in all_changes:
            insert_change(conn, change)
        conn.commit()
        logger.info("Persisted %s change records for %s",
                    len(all_changes), today_date)
    else:
        logger.info("Change log for %s already exists (%s records), skipping",
                    today_date, len(existing))

    conn.close()

    # Write JSON to output for inspection / downstream use
    _write_change_log_json(all_changes, today_date)

    return all_changes


def _write_change_log_json(changes: list[dict], change_date: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"change_log_{change_date}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(changes, f, indent=2, default=str)
    logger.info("Change log written to %s", path)


def summarise_changes(changes: list[dict]) -> dict:
    """
    Return a summary dict useful for email subject lines and logging.
    """
    summary = defaultdict(int)
    for c in changes:
        summary[c["change_type"]] += 1
        rating = (c.get("new_rating") or "").strip()
        scheme = c.get("scheme_type", "FHRS")
        if _is_low_rating(rating, scheme):
            summary["low_rating_count"] += 1

    return dict(summary)
