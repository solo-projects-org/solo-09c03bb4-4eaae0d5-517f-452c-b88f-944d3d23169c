"""SQLite 持久化层。

表设计要点：
- holding_file：每期完整持仓（期初/期末各一行，重复上传整表替换并触发重算）。
- announcement_batch：公告整表版本。status=active 的只有一份；
  换报校验/折算失败时新批次记为 failed，原 active 批次不动，原表仍可查。
- result_batch / result_lot：每次生效的（持仓或公告）变动产生一个结果批次，
  result_lot 按期初来源逐行保存折算后的代码、股数与折算路径。
"""

from __future__ import annotations

import sqlite3
from flask import current_app, g

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS holding_file (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    period      TEXT NOT NULL CHECK (period IN ('begin', 'end')),
    as_of_date  TEXT NOT NULL,
    filename    TEXT NOT NULL,
    row_count   INTEGER NOT NULL,
    is_current  INTEGER NOT NULL DEFAULT 1,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_holding_current ON holding_file(period, is_current);

CREATE TABLE IF NOT EXISTS holding_row (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id  INTEGER NOT NULL REFERENCES holding_file(id),
    code     TEXT NOT NULL,
    shares   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_holding_row_file ON holding_row(file_id);

CREATE TABLE IF NOT EXISTS announcement_batch (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    status         TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'failed')),
    filename       TEXT NOT NULL,
    row_count      INTEGER NOT NULL,
    note           TEXT,
    uploaded_at    TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_at  TEXT
);

CREATE TABLE IF NOT EXISTS announcement (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       INTEGER NOT NULL REFERENCES announcement_batch(id),
    effective_date TEXT NOT NULL,
    old_code       TEXT NOT NULL,
    new_code       TEXT NOT NULL,
    ratio          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_announcement_batch ON announcement(batch_id);

CREATE TABLE IF NOT EXISTS result_batch (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_batch_id INTEGER NOT NULL REFERENCES announcement_batch(id),
    begin_file_id        INTEGER NOT NULL REFERENCES holding_file(id),
    end_file_id          INTEGER REFERENCES holding_file(id),
    begin_date           TEXT NOT NULL,
    end_date             TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    trigger_note         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS result_lot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    result_batch_id INTEGER NOT NULL REFERENCES result_batch(id),
    origin_code     TEXT NOT NULL,
    end_code        TEXT NOT NULL,
    adjusted_shares TEXT NOT NULL,
    path_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_result_lot_batch ON result_lot(result_batch_id);
CREATE INDEX IF NOT EXISTS idx_result_lot_code ON result_lot(result_batch_id, end_code);

-- 回查（按公告生效日 + 旧代码追溯期初来源）的准备状态，每个结果批次一行。
-- 首次回查时可准备该批查询数据；失败仅记 failed，原表照查并可重试。
-- 记录持久化在数据库中，服务重启后仍可直接查询。
CREATE TABLE IF NOT EXISTS lookup_prep (
    result_batch_id INTEGER PRIMARY KEY REFERENCES result_batch(id),
    status          TEXT NOT NULL CHECK (status IN ('ready', 'failed')),
    indexed_sources INTEGER NOT NULL DEFAULT 0,
    indexed_steps   INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    prepared_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 回查索引：把 result_lot.path_json 中每一步 (生效日, 旧代码 -> 新代码)
-- 展开成行，命中即定位到经过该公告的期初来源明细；只保存命中所需的来源明细。
CREATE TABLE IF NOT EXISTS lookup_index (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    result_batch_id INTEGER NOT NULL REFERENCES result_batch(id),
    lot_id          INTEGER NOT NULL REFERENCES result_lot(id),
    effective_date  TEXT NOT NULL,
    old_code        TEXT NOT NULL,
    new_code        TEXT NOT NULL,
    step_order      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lookup_query
    ON lookup_index(result_batch_id, effective_date, old_code);
CREATE INDEX IF NOT EXISTS idx_lookup_lot ON lookup_index(lot_id);

-- 预置一份空的 active 公告批次，保证任何时候都有可引用的有效公告表；
-- 首次上传公告时整表替换它。
INSERT INTO announcement_batch (status, filename, row_count, note)
SELECT 'active', '(系统初始空表)', 0, '系统初始化，尚未上传公告'
WHERE NOT EXISTS (SELECT 1 FROM announcement_batch);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db(app.config["DATABASE"])
