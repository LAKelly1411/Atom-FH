"""
Generates output/notifications.html — the persistent alert inbox page.
Called after run_alerts() so the DB already has today's alerts written.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .alerts import get_historical_alerts, load_rules
from .db import get_connection, get_changes_window

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
OUTPUT_DIR    = Path(__file__).parent.parent / "output"


def _format_date(date_str: str | None) -> str:
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%-d %B %Y")
    except (ValueError, AttributeError):
        return date_str[:10] if date_str else ""


def _parse_triggered(raw_data: str) -> list[dict]:
    try:
        return json.loads(raw_data) if raw_data else []
    except (json.JSONDecodeError, TypeError):
        return []


def _rule_type_label(rule_type: str) -> str:
    return {
        "zero_rating":   "Zero rating",
        "threshold":     "Rating drop",
        "business_type": "Sensitive premises",
        "watchlist":     "Watchlist",
    }.get(rule_type, rule_type)


def _rule_scope(rule: dict) -> str:
    parts = []
    if rule.get("regions"):
        parts.append(", ".join(rule["regions"]))
    if rule.get("authorities"):
        parts.append(", ".join(rule["authorities"]))
    if rule.get("min_drop"):
        parts.append(f"drop ≥ {rule['min_drop']} stars")
    if rule.get("business_type_keywords"):
        parts.append(", ".join(rule["business_type_keywords"]))
    return "  ·  ".join(parts) if parts else "All monitored areas"


def render_notifications(run_date: str) -> str:
    historical = get_historical_alerts(days=90)
    rules = load_rules()

    # Embed last 30 days of change data for client-side custom rule evaluation.
    # Exclude first-run days (where everything is new_establishment with no old_rating)
    # so the embedded dataset is useful signal, not baseline noise.
    conn = get_connection()
    window_rows = get_changes_window(conn, days=30)
    conn.close()
    change_data_for_js = []
    for r in window_rows:
        d = dict(r)
        # Only include rating-meaningful changes
        if d.get("change_type") == "new_establishment" and not d.get("new_rating"):
            continue
        change_data_for_js.append({
            "fhrs_id":              d.get("fhrs_id"),
            "change_date":          d.get("change_date"),
            "change_type":          d.get("change_type"),
            "business_name":        d.get("business_name"),
            "business_type":        d.get("business_type"),
            "address":              d.get("address") or "",
            "postcode":             d.get("postcode"),
            "local_authority_name": d.get("local_authority_name"),
            "region":               d.get("region"),
            "scheme_type":          d.get("scheme_type"),
            "old_rating":           d.get("old_rating"),
            "new_rating":           d.get("new_rating"),
            "old_rating_date":      d.get("old_rating_date"),
            "new_rating_date":      d.get("new_rating_date"),
        })

    authorities = sorted({d["local_authority_name"] for d in change_data_for_js
                          if d.get("local_authority_name")})
    business_types = sorted({d["business_type"] for d in change_data_for_js
                              if d.get("business_type")})
    change_data_json = json.dumps(change_data_for_js)

    # Augment historical alerts
    for a in historical:
        a["_date_display"]  = _format_date(a["flag_date"])
        a["_triggered"]     = _parse_triggered(a.get("raw_data"))
        a["_count"]         = len(a["_triggered"])
        # Detect rule type from raw_data businesses (for badge colour)
        matching_rule = next((r for r in rules if r["name"] == a["detector_name"]), {})
        a["_rule_type"]      = matching_rule.get("type", "")
        a["_rule_type_label"] = _rule_type_label(a["_rule_type"])

    # Augment rule configs
    for r in rules:
        r["_type_label"] = _rule_type_label(r["type"])
        r["_scope"]      = _rule_scope(r)
        # Find most recent alert for this rule
        recent = next((a for a in historical if a["detector_name"] == r["name"]), None)
        r["_last_fired"]  = _format_date(recent["flag_date"]) if recent else None
        r["_total_fired"] = sum(1 for a in historical if a["detector_name"] == r["name"])

    total_alerts = len(historical)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    template = env.get_template("notifications.html")
    return template.render(
        run_date=run_date,
        run_date_display=_format_date(run_date + "T00:00:00") or run_date,
        alerts=historical,
        rules=rules,
        total_alerts=total_alerts,
        change_data_json=change_data_json,
        authorities=authorities,
        business_types=business_types,
    )


def save_notifications_html(html: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "notifications.html"
    path.write_text(html, encoding="utf-8")
    logger.info("Notifications page saved to %s", path)
    return path


def run_notifications_feed(run_date: str) -> Path:
    html = render_notifications(run_date)
    return save_notifications_html(html)
