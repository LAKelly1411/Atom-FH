"""
Database setup and helper functions.
Uses SQLite — accumulates full history of every pull, never overwrites.
"""

import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "fhrs.db"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS authorities (
            local_authority_id  INTEGER PRIMARY KEY,
            name                TEXT NOT NULL,
            scheme_type         TEXT NOT NULL,
            region              TEXT,
            last_resolved       TEXT
        );

        CREATE TABLE IF NOT EXISTS establishments (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            fhrs_id                 TEXT NOT NULL,
            pull_date               TEXT NOT NULL,
            business_name           TEXT,
            business_type           TEXT,
            address_line1           TEXT,
            address_line2           TEXT,
            address_line3           TEXT,
            address_line4           TEXT,
            postcode                TEXT,
            local_authority_id      INTEGER,
            local_authority_name    TEXT,
            scheme_type             TEXT,
            region                  TEXT,
            rating_value            TEXT,
            rating_date             TEXT,
            hygiene_score           INTEGER,
            structural_score        INTEGER,
            management_score        INTEGER,
            new_rating_pending      INTEGER DEFAULT 0,
            raw_json                TEXT,
            UNIQUE(fhrs_id, pull_date)
        );

        CREATE TABLE IF NOT EXISTS change_log (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            change_date             TEXT NOT NULL,
            fhrs_id                 TEXT NOT NULL,
            change_type             TEXT NOT NULL,
            business_name           TEXT,
            business_type           TEXT,
            address                 TEXT,
            postcode                TEXT,
            local_authority_id      INTEGER,
            local_authority_name    TEXT,
            scheme_type             TEXT,
            region                  TEXT,
            old_rating              TEXT,
            new_rating              TEXT,
            old_rating_date         TEXT,
            new_rating_date         TEXT,
            old_hygiene             INTEGER,
            new_hygiene             INTEGER,
            old_structural          INTEGER,
            new_structural          INTEGER,
            old_management          INTEGER,
            new_management          INTEGER
        );

        CREATE TABLE IF NOT EXISTS anomaly_flags (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            flag_date           TEXT NOT NULL,
            detector_name       TEXT NOT NULL,
            headline            TEXT,
            plain_english       TEXT,
            raw_data            TEXT,
            confidence          TEXT,
            dismissed           INTEGER DEFAULT 0,
            dismissed_at        TEXT,
            dismissed_note      TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_est_fhrs_id   ON establishments(fhrs_id);
        CREATE INDEX IF NOT EXISTS idx_est_pull_date ON establishments(pull_date);
        CREATE INDEX IF NOT EXISTS idx_est_la        ON establishments(local_authority_id);
        CREATE INDEX IF NOT EXISTS idx_change_date   ON change_log(change_date);
        CREATE INDEX IF NOT EXISTS idx_change_la     ON change_log(local_authority_id);
        CREATE INDEX IF NOT EXISTS idx_anomaly_date  ON anomaly_flags(flag_date);
    """)
    conn.commit()
    conn.close()


def upsert_authority(conn: sqlite3.Connection, la_id: int, name: str,
                     scheme_type: str, region: str, resolved_date: str) -> None:
    conn.execute("""
        INSERT INTO authorities (local_authority_id, name, scheme_type, region, last_resolved)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(local_authority_id) DO UPDATE SET
            name=excluded.name,
            scheme_type=excluded.scheme_type,
            region=excluded.region,
            last_resolved=excluded.last_resolved
    """, (la_id, name, scheme_type, region, resolved_date))


def insert_establishment(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute("""
        INSERT OR IGNORE INTO establishments (
            fhrs_id, pull_date, business_name, business_type,
            address_line1, address_line2, address_line3, address_line4,
            postcode, local_authority_id, local_authority_name,
            scheme_type, region, rating_value, rating_date,
            hygiene_score, structural_score, management_score,
            new_rating_pending, raw_json
        ) VALUES (
            :fhrs_id, :pull_date, :business_name, :business_type,
            :address_line1, :address_line2, :address_line3, :address_line4,
            :postcode, :local_authority_id, :local_authority_name,
            :scheme_type, :region, :rating_value, :rating_date,
            :hygiene_score, :structural_score, :management_score,
            :new_rating_pending, :raw_json
        )
    """, record)


def get_latest_pull_date(conn: sqlite3.Connection, la_id: int) -> str | None:
    row = conn.execute("""
        SELECT MAX(pull_date) as max_date FROM establishments
        WHERE local_authority_id = ?
    """, (la_id,)).fetchone()
    return row["max_date"] if row else None


def get_establishments_for_date(conn: sqlite3.Connection, la_id: int,
                                 pull_date: str) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT * FROM establishments
        WHERE local_authority_id = ? AND pull_date = ?
    """, (la_id, pull_date)).fetchall()


def insert_change(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute("""
        INSERT INTO change_log (
            change_date, fhrs_id, change_type, business_name, business_type,
            address, postcode, local_authority_id, local_authority_name,
            scheme_type, region, old_rating, new_rating, old_rating_date,
            new_rating_date, old_hygiene, new_hygiene, old_structural,
            new_structural, old_management, new_management
        ) VALUES (
            :change_date, :fhrs_id, :change_type, :business_name, :business_type,
            :address, :postcode, :local_authority_id, :local_authority_name,
            :scheme_type, :region, :old_rating, :new_rating, :old_rating_date,
            :new_rating_date, :old_hygiene, :new_hygiene, :old_structural,
            :new_structural, :old_management, :new_management
        )
    """, record)


def get_changes_for_date(conn: sqlite3.Connection, change_date: str) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT * FROM change_log WHERE change_date = ?
        ORDER BY region, local_authority_name, change_type, business_name
    """, (change_date,)).fetchall()


def get_changes_window(conn: sqlite3.Connection, days: int = 90) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT * FROM change_log
        WHERE change_date >= date('now', ?)
        ORDER BY change_date DESC
    """, (f"-{days} days",)).fetchall()
