"""
Milestone 1: Ingestion
Queries the FSA FHRS/FHIS API for each configured local authority
and stores every establishment record with today's pull date.
"""

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

import requests
import yaml

from .db import (get_connection, init_db, insert_establishment,
                 upsert_authority, get_latest_pull_date)

logger = logging.getLogger(__name__)

FSA_BASE = "https://api.ratings.food.gov.uk"
FSA_HEADERS = {
    "x-api-version": "2",
    "Accept": "application/json",
}
PAGE_SIZE = 500
CONFIG_PATH = Path(__file__).parent.parent / "config" / "local_authorities.yaml"


# ── Authority resolution ──────────────────────────────────────────────────────

def fetch_all_authorities() -> list[dict]:
    """Return every local authority from the FSA API."""
    resp = requests.get(f"{FSA_BASE}/authorities/basic",
                        headers=FSA_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("authorities", [])


def resolve_authority_ids(save: bool = True) -> dict[str, int]:
    """
    Match configured authority names against the FSA API.
    Returns {name: id} mapping and optionally writes IDs back to config.
    """
    config = _load_config()
    all_authorities = fetch_all_authorities()
    api_by_name = {a["Name"].strip(): a["LocalAuthorityId"]
                   for a in all_authorities}

    resolved = {}
    unmatched = []

    for entry in config["authorities"]:
        name = entry["name"].strip()
        la_id = api_by_name.get(name)
        if la_id:
            entry["id"] = la_id
            resolved[name] = la_id
            logger.info("Resolved: %s → ID %s", name, la_id)
        else:
            # Try case-insensitive fallback
            match = next(
                (a for a in all_authorities
                 if a["Name"].strip().lower() == name.lower()),
                None
            )
            if match:
                la_id = match["LocalAuthorityId"]
                entry["id"] = la_id
                entry["name"] = match["Name"].strip()  # normalise to API name
                resolved[entry["name"]] = la_id
                logger.info("Resolved (case-insensitive): %s → ID %s",
                            entry["name"], la_id)
            else:
                unmatched.append(name)
                logger.warning("Could not resolve authority: %s", name)

    if unmatched:
        logger.warning(
            "Unmatched authorities — check spelling against FSA API:\n%s\n"
            "Run `python run.py --list-authorities` to see all valid names.",
            "\n".join(f"  • {n}" for n in unmatched)
        )

    if save and resolved:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        logger.info("Saved resolved IDs to %s", CONFIG_PATH)

    return resolved


def list_all_authority_names() -> None:
    """Print every FSA authority name — useful for finding exact spellings."""
    authorities = fetch_all_authorities()
    for a in sorted(authorities, key=lambda x: x["Name"]):
        print(f"  [{a['LocalAuthorityId']:>5}]  {a['Name']:<50}  {a.get('SchemeType', '')}")


# ── Ingestion ─────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _fetch_establishments_for_authority(la_id: int) -> list[dict]:
    """Paginate through all establishments for a given local authority ID."""
    establishments = []
    page = 1

    while True:
        params = {
            "localAuthorityId": la_id,
            "pageSize": PAGE_SIZE,
            "pageNumber": page,
        }
        try:
            resp = requests.get(
                f"{FSA_BASE}/establishments",
                headers=FSA_HEADERS,
                params=params,
                timeout=60,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("API error for LA %s page %s: %s", la_id, page, exc)
            break

        data = resp.json()
        page_records = data.get("establishments", [])
        establishments.extend(page_records)

        meta = data.get("meta", {})
        total = meta.get("totalCount", 0)
        total_pages = meta.get("totalPages", 1)
        logger.debug("LA %s page %s/%s: %s/%s records", la_id, page,
                     total_pages, len(establishments), total)

        if page >= total_pages or not page_records:
            break

        page += 1
        time.sleep(0.5)  # polite rate-limiting

    return establishments


def _parse_establishment(raw: dict, la_config: dict, pull_date: str) -> dict:
    """Map FSA API record to our DB schema."""
    scores = raw.get("scores") or {}
    address_parts = [
        raw.get("addressLine1", ""),
        raw.get("addressLine2", ""),
        raw.get("addressLine3", ""),
        raw.get("addressLine4", ""),
    ]
    # Build a clean single-line address for display
    address_display = ", ".join(p for p in address_parts if p)

    return {
        "fhrs_id": str(raw.get("FHRSID", "")),
        "pull_date": pull_date,
        "business_name": raw.get("BusinessName", ""),
        "business_type": raw.get("BusinessType", ""),
        "address_line1": raw.get("addressLine1", ""),
        "address_line2": raw.get("addressLine2", ""),
        "address_line3": raw.get("addressLine3", ""),
        "address_line4": raw.get("addressLine4", ""),
        "postcode": raw.get("PostCode", ""),
        "local_authority_id": la_config["id"],
        "local_authority_name": la_config["name"],
        "scheme_type": la_config.get("scheme", "FHRS"),
        "region": la_config.get("region", ""),
        "rating_value": str(raw.get("RatingValue", "")),
        "rating_date": raw.get("RatingDate", ""),
        "hygiene_score": scores.get("Hygiene"),
        "structural_score": scores.get("Structural"),
        "management_score": scores.get("ConfidenceInManagement"),
        "new_rating_pending": 1 if raw.get("NewRatingPending") else 0,
        "raw_json": json.dumps(raw),
    }


def run_ingestion(pull_date: str | None = None) -> dict[str, int]:
    """
    Main ingestion entrypoint.
    Pulls all configured local authorities and stores results in SQLite.
    Returns summary {authority_name: record_count}.
    """
    if pull_date is None:
        pull_date = date.today().isoformat()

    init_db()
    config = _load_config()
    conn = get_connection()
    summary = {}

    for la in config["authorities"]:
        if not la.get("id"):
            logger.warning("Skipping %s — no ID resolved. "
                           "Run `python run.py --resolve-authorities` first.",
                           la["name"])
            continue

        logger.info("Fetching %s (ID %s)...", la["name"], la["id"])
        raw_records = _fetch_establishments_for_authority(la["id"])
        count = 0

        for raw in raw_records:
            record = _parse_establishment(raw, la, pull_date)
            insert_establishment(conn, record)
            count += 1

        # Store authority metadata
        upsert_authority(
            conn,
            la_id=la["id"],
            name=la["name"],
            scheme_type=la.get("scheme", "FHRS"),
            region=la.get("region", ""),
            resolved_date=datetime.utcnow().isoformat(),
        )

        conn.commit()
        summary[la["name"]] = count
        logger.info("Stored %s records for %s", count, la["name"])

    conn.close()
    return summary
