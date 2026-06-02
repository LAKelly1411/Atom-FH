"""
Milestone 3: Daily Feed Generation
Renders the change log into a PA-branded HTML digest.

Sections (in priority order):
  1. Zero-rated / Improvement Required (most newsworthy)
  2. Significant drops (2+ stars)
  3. New inspections (other rating changes)
  4. Improvements
  5. New establishments
  6. Re-inspections / score changes (optional — collapsed by default)

The digest also carries a full current-snapshot view so newsrooms can
browse all monitored establishments filtered by rating or local authority.
"""

import logging
import os
import smtplib
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .detect import _is_low_rating, _rating_numeric

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
OUTPUT_DIR = Path(__file__).parent.parent / "output"


# ── Address helpers ───────────────────────────────────────────────────────────

def _build_address(row) -> str:
    """Join address_line1-4 into a single display string."""
    parts = [
        row.get("address_line1") or "",
        row.get("address_line2") or "",
        row.get("address_line3") or "",
        row.get("address_line4") or "",
    ]
    return ", ".join(p for p in parts if p.strip())


# ── Change classification ─────────────────────────────────────────────────────

def _rating_drop(change: dict) -> int | None:
    """Return the numeric drop in stars (positive = worse). FHRS only."""
    old = _rating_numeric(change.get("old_rating"))
    new = _rating_numeric(change.get("new_rating"))
    if old is not None and new is not None:
        return old - new
    return None


def _classify_changes(changes: list[dict]) -> dict:
    """
    Sort changes into editorial buckets for the email template.
    Returns dict with keys: zero_rated, significant_drops, new_inspections,
    improvements, new_establishments, reinspections, score_changes.
    """
    buckets = {
        "zero_rated": [],
        "significant_drops": [],
        "new_inspections": [],
        "improvements": [],
        "new_establishments": [],
        "reinspections": [],
        "score_changes": [],
    }

    for c in changes:
        change_type = c.get("change_type")
        scheme = c.get("scheme_type", "FHRS")
        new_rating = (c.get("new_rating") or "").strip()
        old_rating = (c.get("old_rating") or "").strip()

        if change_type == "new_establishment":
            buckets["new_establishments"].append(c)
            continue

        if change_type == "reinspection":
            buckets["reinspections"].append(c)
            continue

        if change_type == "score_change":
            buckets["score_changes"].append(c)
            continue

        # rating_change
        if change_type == "rating_change":
            if _is_low_rating(new_rating, scheme):
                buckets["zero_rated"].append(c)
            else:
                drop = _rating_drop(c)
                if drop is not None:
                    if drop >= 2:
                        buckets["significant_drops"].append(c)
                    elif drop > 0:
                        buckets["new_inspections"].append(c)
                    elif drop < 0:
                        buckets["improvements"].append(c)
                    else:
                        buckets["new_inspections"].append(c)
                else:
                    # FHIS or non-numeric
                    if (scheme == "FHIS" and
                            new_rating.lower() != "improvement required" and
                            old_rating.lower() == "improvement required"):
                        buckets["improvements"].append(c)
                    else:
                        buckets["new_inspections"].append(c)

    return buckets


def _fsa_url(fhrs_id: str) -> str:
    return f"https://ratings.food.gov.uk/business/{fhrs_id}"


def _format_rating(rating: str | None, scheme: str) -> str:
    if not rating or rating in ("", "None"):
        return "Unknown"
    if scheme == "FHIS":
        return rating
    try:
        n = int(rating)
        return "★" * n + "☆" * (5 - n) if 0 <= n <= 5 else rating
    except (ValueError, TypeError):
        return rating


def _score_colour(score: int | None) -> str:
    """Return a CSS hex colour for an FHRS penalty score (lower = better)."""
    if score is None:
        return "#aaa"
    if score == 0:
        return "#1a7a4a"
    if score <= 5:
        return "#27ae60"
    if score <= 10:
        return "#f39c12"
    if score <= 15:
        return "#e67e22"
    if score <= 20:
        return "#e74c3c"
    return "#c0392b"


def _score_label(score: int | None) -> str:
    """Return a plain-English label for an FHRS penalty score."""
    if score is None:
        return ""
    if score == 0:
        return "Very good"
    if score <= 5:
        return "Good"
    if score <= 10:
        return "Generally satisfactory"
    if score <= 15:
        return "Improvement necessary"
    if score <= 20:
        return "Urgent improvement needed"
    return "Urgent / Major non-compliance"


def _augment_scores(c: dict) -> None:
    """Add score display helpers to a change or establishment dict."""
    for prefix, max_val in (("new_hygiene", 25), ("new_structural", 25), ("new_management", 30)):
        val = c.get(prefix)
        if val is None:
            # Also try without prefix (for establishment records)
            plain = prefix.replace("new_", "")  # hygiene_score etc handled separately
        c[f"_{prefix}_colour"] = _score_colour(val)
        c[f"_{prefix}_label"]  = _score_label(val)
        c[f"_{prefix}_max"]    = max_val

    # Establishment snapshot uses different field names
    for field, max_val in (("hygiene_score", 25), ("structural_score", 25), ("management_score", 30)):
        val = c.get(field)
        c[f"_{field}_colour"] = _score_colour(val)
        c[f"_{field}_label"]  = _score_label(val)
        c[f"_{field}_max"]    = max_val


def _format_date(date_str: str | None) -> str:
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%-d %B %Y")
    except (ValueError, AttributeError):
        return date_str[:10] if date_str else ""


# ── Truncation helpers ────────────────────────────────────────────────────────

def _interleave_truncate(items: list[dict], limit: int,
                         key: str = "local_authority_name") -> list[dict]:
    """
    Return up to `limit` items sampled round-robin across `key` groups.
    Prevents any single group dominating when data is alphabetically sorted.
    """
    if len(items) <= limit:
        return items

    from collections import defaultdict
    from itertools import zip_longest

    by_group: dict[str, list] = defaultdict(list)
    for item in items:
        by_group[item.get(key, "")].append(item)

    result = []
    for round_items in zip_longest(*by_group.values()):
        for item in round_items:
            if item is not None:
                result.append(item)
                if len(result) >= limit:
                    return result
    return result


# ── Template rendering ────────────────────────────────────────────────────────

def render_digest(changes: list[dict], run_date: str | None = None) -> str:
    """Render the daily digest as an HTML string."""
    if run_date is None:
        run_date = date.today().isoformat()

    # Detect first-run scenario (for the banner only — all data is still shown).
    non_new = [c for c in changes if c.get("change_type") != "new_establishment"]
    all_new = len(non_new) == 0 and len(changes) > 100
    baseline_count = len(changes) if all_new else 0

    SECTION_PAGE_SIZE = 100
    ALL_DATA_LIMIT = 2000   # cards rendered in the "all data" view

    buckets = _classify_changes(changes)

    # ── Alert count for the notifications badge ───────────────────────────────
    from .alerts import get_historical_alerts
    today_alerts = [a for a in get_historical_alerts(days=1)
                    if a["flag_date"] == run_date]
    alert_count = len(today_alerts)

    # ── Fetch current snapshot + 90-day window ───────────────────────────────
    from .db import get_connection, get_current_establishments, get_changes_window
    conn = get_connection()
    current_rows  = get_current_establishments(conn)
    window_rows   = get_changes_window(conn, days=90)
    conn.close()

    # Window changes for historical date-range browsing
    window_changes = [dict(r) for r in window_rows]
    window_buckets = _classify_changes(window_changes)
    window_section_totals = {k: len(v) for k, v in window_buckets.items()}

    # Build address lookup: fhrs_id → display address (from current snapshot)
    address_lookup: dict[str, str] = {}
    for r in current_rows:
        r_dict = dict(r)
        addr = _build_address(r_dict)
        address_lookup[r_dict["fhrs_id"]] = addr

    def _augment_change(c: dict) -> None:
        c["_fsa_url"] = _fsa_url(c["fhrs_id"])
        c["_new_rating_display"] = _format_rating(
            c.get("new_rating"), c.get("scheme_type", "FHRS"))
        c["_old_rating_display"] = _format_rating(
            c.get("old_rating"), c.get("scheme_type", "FHRS"))
        c["_rating_date_display"] = _format_date(c.get("new_rating_date"))
        c["_old_rating_date_display"] = _format_date(c.get("old_rating_date"))
        c["_drop"] = _rating_drop(c)
        c["_address"] = (c.get("address") or "").strip() or \
                        address_lookup.get(c["fhrs_id"], "")
        _augment_scores(c)

    # Augment today's buckets
    for bucket in buckets.values():
        for c in bucket:
            _augment_change(c)

    # Augment window buckets (historical browsing)
    for bucket in window_buckets.values():
        for c in bucket:
            _augment_change(c)

    # Real totals before truncation (shown in section headers)
    section_totals = {k: len(v) for k, v in buckets.items()}

    # Authority list for today's filter dropdowns (only LAs with changes today)
    authorities = sorted({c.get("local_authority_name", "")
                          for c in changes if c.get("local_authority_name")})

    # Coverage string — all monitored LAs from the current snapshot, not just today's
    all_monitored = sorted({dict(r)["local_authority_name"] for r in current_rows
                            if dict(r).get("local_authority_name")})
    coverage = ", ".join(all_monitored) if all_monitored else "all monitored areas"

    # Truncate rendered buckets
    WINDOW_PAGE_SIZE = 300   # per section across 90-day window
    buckets        = {k: _interleave_truncate(v, SECTION_PAGE_SIZE) for k, v in buckets.items()}
    window_buckets = {k: _interleave_truncate(v, WINDOW_PAGE_SIZE)  for k, v in window_buckets.items()}

    # Date range for the calendar controls
    window_dates = sorted({c["change_date"] for c in window_changes})

    total_changes = sum(v for k, v in section_totals.items()
                        if k not in ("reinspections", "score_changes"))
    has_news = total_changes > 0

    run_date_display = _format_date(run_date + "T00:00:00") or run_date

    # ── Build "all data" snapshot list ───────────────────────────────────────
    all_establishments = []
    all_authorities_set = set()
    for r in current_rows:
        d = dict(r)
        d["_address"] = _build_address(d)
        d["_rating_display"] = _format_rating(
            d.get("rating_value"), d.get("scheme_type", "FHRS"))
        d["_rating_date_display"] = _format_date(d.get("rating_date"))
        d["_fsa_url"] = _fsa_url(d["fhrs_id"])
        _augment_scores(d)
        all_establishments.append(d)
        if d.get("local_authority_name"):
            all_authorities_set.add(d["local_authority_name"])

    all_authorities = sorted(all_authorities_set)
    total_establishments = len(all_establishments)
    all_establishments = _interleave_truncate(all_establishments, ALL_DATA_LIMIT)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    template = env.get_template("email_digest.html")

    return template.render(
        run_date=run_date,
        run_date_display=run_date_display,
        coverage=coverage,
        has_news=has_news,
        total_changes=total_changes,
        # Today's buckets (default view)
        buckets=buckets,
        section_totals=section_totals,
        section_page_size=SECTION_PAGE_SIZE,
        authorities=authorities,
        all_changes=changes,
        is_first_run=all_new,
        baseline_count=baseline_count,
        # 90-day window (date-range browsing)
        window_buckets=window_buckets,
        window_section_totals=window_section_totals,
        window_page_size=WINDOW_PAGE_SIZE,
        window_dates=window_dates,
        # Alert badge
        alert_count=alert_count,
        # All-data view
        all_establishments=all_establishments,
        all_authorities=all_authorities,
        total_establishments=total_establishments,
        all_data_limit=ALL_DATA_LIMIT,
    )


def save_digest_html(html: str, run_date: str | None = None) -> Path:
    """Write the rendered digest to the output folder."""
    if run_date is None:
        run_date = date.today().isoformat()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"digest_{run_date}.html"
    path.write_text(html, encoding="utf-8")
    logger.info("Digest saved to %s", path)
    return path


def send_digest_email(html: str, recipients: list[str],
                      run_date: str | None = None,
                      subject_override: str | None = None) -> bool:
    """
    Send the digest via SMTP.
    Reads credentials from environment variables:
      SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM
    Returns True on success.
    """
    if run_date is None:
        run_date = date.today().isoformat()

    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)

    if not smtp_host:
        logger.warning("SMTP_HOST not set — skipping email send")
        return False

    subject = subject_override or f"PA Atomic | Food Hygiene Digest — {run_date}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            if smtp_user:
                server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, recipients, msg.as_string())
        logger.info("Digest sent to %s", recipients)
        return True
    except smtplib.SMTPException as exc:
        logger.error("Failed to send digest: %s", exc)
        return False


def run_feed(changes: list[dict], recipients: list[str] | None = None,
             run_date: str | None = None) -> Path:
    """
    Main feed entrypoint.
    Renders digest, saves HTML, and optionally sends email.
    Returns path to the saved HTML file.
    """
    if run_date is None:
        run_date = date.today().isoformat()

    html = render_digest(changes, run_date)
    path = save_digest_html(html, run_date)

    if recipients:
        send_digest_email(html, recipients, run_date)

    return path
