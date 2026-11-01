from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from .config import DATA, Settings, timestamp

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS instruments(
 id TEXT PRIMARY KEY, symbol TEXT NOT NULL, market TEXT NOT NULL, name TEXT NOT NULL,
 asset_class TEXT NOT NULL, group_name TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
 watched INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'unknown',
 risk_flag TEXT NOT NULL DEFAULT 'unknown', verified_at TEXT, source_url TEXT,
 rules_json TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}', UNIQUE(market,symbol));
CREATE TABLE IF NOT EXISTS bars(
 instrument_id TEXT NOT NULL REFERENCES instruments(id), timeframe TEXT NOT NULL,
 ts TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
 close REAL NOT NULL, volume REAL NOT NULL, amount REAL, adjustment TEXT NOT NULL,
 source TEXT NOT NULL, source_url TEXT NOT NULL, fetched_at TEXT NOT NULL,
 quality TEXT NOT NULL DEFAULT 'valid', metadata_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(instrument_id,timeframe,ts));
CREATE TABLE IF NOT EXISTS market_quotes(
 instrument_id TEXT PRIMARY KEY REFERENCES instruments(id), payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_context(
 id TEXT PRIMARY KEY,category TEXT NOT NULL,instrument_id TEXT,payload_json TEXT NOT NULL,
 source TEXT NOT NULL,source_url TEXT NOT NULL,as_of TEXT NOT NULL,fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_events(
 id INTEGER PRIMARY KEY, instrument_id TEXT NOT NULL REFERENCES instruments(id),
 event_type TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL,
 source_url TEXT NOT NULL, verified_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calendar(day TEXT PRIMARY KEY, source TEXT NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, logical_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 status TEXT NOT NULL, started_at TEXT, finished_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 error TEXT, result_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_logs(
 id INTEGER PRIMARY KEY, job_id TEXT REFERENCES jobs(id), ts TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports(
 id TEXT PRIMARY KEY, logical_key TEXT NOT NULL UNIQUE, job_id TEXT REFERENCES jobs(id),
 mode TEXT NOT NULL, report_date TEXT NOT NULL, generated_at TEXT NOT NULL,
 payload_json TEXT NOT NULL, markdown_path TEXT NOT NULL, html_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidates(
 report_id TEXT NOT NULL REFERENCES reports(id), instrument_id TEXT NOT NULL REFERENCES instruments(id),
 payload_json TEXT NOT NULL, PRIMARY KEY(report_id,instrument_id));
CREATE TABLE IF NOT EXISTS accounts(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, initial_cash TEXT NOT NULL, cash TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(
 id TEXT PRIMARY KEY, client_key TEXT NOT NULL UNIQUE, account_id TEXT NOT NULL REFERENCES accounts(id),
 instrument_id TEXT NOT NULL REFERENCES instruments(id), side TEXT NOT NULL,
 quantity INTEGER NOT NULL, limit_price TEXT NOT NULL, stop_loss TEXT,
 status TEXT NOT NULL, created_at TEXT NOT NULL, filled_at TEXT, fill_price TEXT,
 fees TEXT, cost TEXT, source_ts TEXT, reason TEXT, rules_json TEXT NOT NULL, fees_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lots(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
 instrument_id TEXT NOT NULL REFERENCES instruments(id), quantity INTEGER NOT NULL,
 cost TEXT NOT NULL, acquired_day TEXT NOT NULL, settlement_days INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(
 id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), order_id TEXT UNIQUE REFERENCES orders(id),
 ts TEXT NOT NULL, delta TEXT NOT NULL, balance TEXT NOT NULL, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equity_snapshots(
 account_id TEXT NOT NULL REFERENCES accounts(id), ts TEXT NOT NULL, equity TEXT NOT NULL,
 PRIMARY KEY(account_id,ts));
CREATE TABLE IF NOT EXISTS backtest_runs(
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, params_json TEXT NOT NULL, result_json TEXT NOT NULL,
 data_hash TEXT NOT NULL, strategy_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(
 id INTEGER PRIMARY KEY, ts TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_bars_time ON bars(instrument_id,timeframe,ts DESC);
"""


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=str)


@contextmanager
def connection():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / "stockai.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def query(sql: str, args=()) -> list[dict]:
    with connection() as c:
        return [dict(row) for row in c.execute(sql, args)]


def one(sql: str, args=()) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args=()):
    with connection() as c:
        return c.execute(sql, args).rowcount


def settings() -> Settings:
    row = one("SELECT value FROM settings WHERE key='app'")
    return Settings.model_validate_json(row["value"]) if row else Settings()


def save_settings(value: Settings):
    execute("INSERT INTO settings(key,value) VALUES('app',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value.model_dump_json(),))


def notify(message: str, level="info"):
    execute("INSERT INTO notifications(ts,level,message) VALUES(?,?,?)", (timestamp(), level, message[:2000]))


def initialize():
    with connection() as c:
        c.executescript(SCHEMA)
        c.execute("INSERT OR IGNORE INTO migrations VALUES(1,?)", (timestamp(),))
        c.execute("INSERT OR IGNORE INTO settings VALUES('app',?)", (Settings().model_dump_json(),))
    from .instruments import seed
    seed()
