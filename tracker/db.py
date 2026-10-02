"""
SQLite schema + helpers for the affiliate click tracker.

Design notes (from the tracking research):
- click_id is OURS (ULID): sortable, unique, URL-safe. Never trust a network ID as join key.
- UNIQUE(click_id, txid, status) gives free idempotency for postback replays.
- Every inbound postback is logged raw (matched or not) for forensics.
- Geo/device captured at click time (cannot be recomputed from a stale IP later).
"""
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = os.environ.get("TRACKER_DB", str(Path(__file__).resolve().parent / "tracker.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  network TEXT,
  offer_url TEXT NOT NULL,
  default_payout REAL DEFAULT 0,
  currency TEXT DEFAULT 'GBP',
  status_map_json TEXT DEFAULT '{}',
  created_ts INTEGER
);

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT UNIQUE NOT NULL,          -- used in /r/{token}
  name TEXT NOT NULL,
  source TEXT,                          -- tiktok / pinterest / seo / email ...
  offer_id INTEGER REFERENCES offers(id),
  creative_id TEXT,
  created_ts INTEGER
);

CREATE TABLE IF NOT EXISTS clicks (
  click_id     TEXT PRIMARY KEY,
  ts           INTEGER NOT NULL,
  campaign_id  INTEGER,
  source       TEXT,
  creative_id  TEXT,
  placement    TEXT,
  keyword      TEXT,
  sub3         TEXT,
  sub4         TEXT,
  sub5         TEXT,
  ip           TEXT,
  ua           TEXT,
  ua_hash      TEXT,
  referrer     TEXT,
  country      TEXT,
  device_type  TEXT,
  os           TEXT,
  browser      TEXT,
  offer_id     INTEGER,
  target_url   TEXT,
  ext_click_id TEXT,
  is_bot       INTEGER DEFAULT 0,
  is_dupe      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_clicks_ts       ON clicks(ts);
CREATE INDEX IF NOT EXISTS ix_clicks_campaign ON clicks(campaign_id, ts);
CREATE INDEX IF NOT EXISTS ix_clicks_creative ON clicks(creative_id, ts);
CREATE INDEX IF NOT EXISTS ix_clicks_uahash   ON clicks(ua_hash, ts);

CREATE TABLE IF NOT EXISTS conversions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  click_id     TEXT REFERENCES clicks(click_id),
  txid         TEXT,
  ts           INTEGER NOT NULL,
  received_ts  INTEGER NOT NULL,
  status       TEXT,
  conv_type    TEXT,
  payout       REAL DEFAULT 0,
  payout_cur   TEXT,
  payout_gbp   REAL,
  offer_id     INTEGER,
  network      TEXT,
  raw_query    TEXT,
  ip           TEXT,
  UNIQUE(click_id, txid, status)
);
CREATE INDEX IF NOT EXISTS ix_conv_click ON conversions(click_id);
CREATE INDEX IF NOT EXISTS ix_conv_ts    ON conversions(ts);

CREATE TABLE IF NOT EXISTS postback_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        INTEGER,
  remote_ip TEXT,
  query     TEXT,
  matched   INTEGER,
  reason    TEXT
);
CREATE INDEX IF NOT EXISTS ix_pbevents_ts ON postback_events(ts);

CREATE TABLE IF NOT EXISTS click_blacklist (
  ip       TEXT,
  ua_hash  TEXT,
  reason   TEXT,
  added_ts INTEGER
);

CREATE VIEW IF NOT EXISTS v_conv AS
SELECT c.source, c.campaign_id, c.creative_id, c.placement, c.keyword,
       COUNT(DISTINCT c.click_id) AS clicks,
       COUNT(v.id)                AS conversions,
       COALESCE(SUM(v.payout),0)  AS revenue,
       SUM(CASE WHEN v.conv_type='rejected' THEN 1 ELSE 0 END) AS rejects
FROM clicks c
LEFT JOIN conversions v
       ON v.click_id = c.click_id AND v.conv_type NOT IN ('rejected')
WHERE c.is_bot = 0
GROUP BY c.source, c.campaign_id, c.creative_id, c.placement, c.keyword;
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def now_ms() -> int:
    return int(time.time() * 1000)
