from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# 日志模式按运行介质选择。本机磁盘用 WAL（默认，读写并发好）；
# U 盘等可移动介质由启动脚本改成 DELETE + FULL，见下面 _journal_mode() 的说明。
_VALID_JOURNAL = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
_VALID_SYNCHRONOUS = {"OFF", "NORMAL", "FULL", "EXTRA"}


def _journal_mode() -> str:
    """回滚日志还是 WAL。

    WAL 依赖 .sqlite3-wal / .sqlite3-shm 两个旁挂文件和共享内存锁。本机 NTFS 上
    这没问题；但 U 盘常见的 FAT32/exFAT 对文件锁的支持很弱，写入延迟也高，
    这时 WAL 反而比传统的回滚日志更容易在拔盘时把库写坏。便携版启动脚本会把它
    设成 DELETE。两个值都留在白名单里，避免环境变量被写进 PRAGMA。
    """
    mode = os.environ.get("CONTRACTDB_JOURNAL_MODE", "WAL").strip().upper()
    return mode if mode in _VALID_JOURNAL else "WAL"


def _synchronous() -> str | None:
    """落盘强度。便携版设 FULL：写完就落盘，宁可慢一点也不要丢已确认的账。"""
    value = os.environ.get("CONTRACTDB_SYNCHRONOUS", "").strip().upper()
    return value if value in _VALID_SYNCHRONOUS else None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(root: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(root / "contracts.sqlite3", timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    # 从 WAL 切回 DELETE 时 SQLite 会先做一次 checkpoint，所以旧库能直接转换。
    connection.execute(f"PRAGMA journal_mode = {_journal_mode()}")
    synchronous = _synchronous()
    if synchronous:
        connection.execute(f"PRAGMA synchronous = {synchronous}")
    return connection


@contextmanager
def transaction(root: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
  active INTEGER NOT NULL DEFAULT 1,
  session_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS import_types (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS subtypes (
  id INTEGER PRIMARY KEY,
  type_id INTEGER NOT NULL REFERENCES import_types(id),
  name TEXT NOT NULL,
  UNIQUE (type_id, name)
);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id),
  name TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS projects_unique_name ON projects(COALESCE(company_id, 0), name);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  original_filename TEXT NOT NULL,
  stored_filename TEXT NOT NULL UNIQUE,
  sha256 TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  file_size INTEGER NOT NULL,
  title TEXT,
  contract_number TEXT,
  signed_date TEXT,
  company_id INTEGER REFERENCES companies(id),
  type_id INTEGER REFERENCES import_types(id),
  subtype_id INTEGER REFERENCES subtypes(id),
  project_id INTEGER REFERENCES projects(id),
  notes TEXT,
  amount_minor INTEGER,
  payable_minor INTEGER,
  currency TEXT NOT NULL DEFAULT 'CNY',
  review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending', 'verified')),
  extraction_status TEXT NOT NULL DEFAULT 'queued' CHECK (extraction_status IN ('queued', 'processing', 'ready', 'error')),
  extraction_error TEXT,
  candidates_json TEXT NOT NULL DEFAULT '{}',
  extracted_text TEXT NOT NULL DEFAULT '',
  source_note TEXT,
  created_by INTEGER REFERENCES users(id),
  updated_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  archived_at TEXT
);
CREATE INDEX IF NOT EXISTS documents_filter_idx ON documents(company_id, type_id, subtype_id, project_id, review_status, archived_at);
CREATE INDEX IF NOT EXISTS documents_sha_idx ON documents(sha256);
CREATE UNIQUE INDEX IF NOT EXISTS documents_active_sha_unique ON documents(sha256) WHERE archived_at IS NULL;
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY,
  document_id TEXT REFERENCES documents(id),
  user_id INTEGER REFERENCES users(id),
  event TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_document_idx ON audit_events(document_id, created_at);
CREATE TABLE IF NOT EXISTS finance_records (
  id INTEGER PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id),
  kind TEXT NOT NULL CHECK(kind IN ('receipt', 'invoice', 'payment')),
  amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
  occurred_on TEXT NOT NULL,
  reference TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  entry_token TEXT NOT NULL UNIQUE,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  voided_at TEXT,
  void_reason TEXT
);
CREATE INDEX IF NOT EXISTS finance_document_idx ON finance_records(document_id, kind, voided_at);
CREATE TABLE IF NOT EXISTS payment_confirmations (
  id INTEGER PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id),
  currency TEXT NOT NULL,
  amount_minor INTEGER,
  received_minor INTEGER NOT NULL,
  unreceived_minor INTEGER,
  payable_minor INTEGER,
  paid_minor INTEGER NOT NULL,
  unpaid_minor INTEGER,
  invoiced_minor INTEGER NOT NULL,
  uninvoiced_minor INTEGER,
  note TEXT NOT NULL DEFAULT '',
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS payment_confirmation_document_idx ON payment_confirmations(document_id, id);
"""


def init_db(root: Path) -> None:
    # 不在这里单独设 journal_mode：connect() 已按运行介质设好，
    # 这里再写死 WAL 会把便携版的 DELETE 覆盖掉。
    connection = connect(root)
    try:
        connection.executescript(SCHEMA)
        existing = {row[1] for row in connection.execute('PRAGMA table_info(documents)')}
        for column, definition in {'amount_minor': 'INTEGER', 'payable_minor': 'INTEGER', 'currency': "TEXT NOT NULL DEFAULT 'CNY'"}.items():
            if column not in existing:
                connection.execute(f'ALTER TABLE documents ADD COLUMN {column} {definition}')
        finance_schema = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'finance_records'").fetchone()[0]
        if "'payment'" not in finance_schema:
            connection.execute("ALTER TABLE finance_records RENAME TO finance_records_old")
            new_table = SCHEMA.split('CREATE TABLE IF NOT EXISTS finance_records (')[1].split(';')[0]
            connection.execute('CREATE TABLE finance_records (' + new_table)
            connection.execute('INSERT INTO finance_records SELECT * FROM finance_records_old')
            connection.execute('DROP TABLE finance_records_old')
            connection.execute('CREATE INDEX finance_document_idx ON finance_records(document_id, kind, voided_at)')
        if not connection.execute("SELECT 1 FROM import_types LIMIT 1").fetchone():
            connection.execute("INSERT INTO import_types(name) VALUES ('合同')")
            connection.execute("INSERT INTO subtypes(type_id, name) SELECT id, '开发费' FROM import_types WHERE name = '合同'")
        connection.commit()
    finally:
        connection.close()


def audit(
    connection: sqlite3.Connection,
    event: str,
    *,
    document_id: str | None = None,
    user_id: int | None = None,
    details: dict | None = None,
) -> None:
    connection.execute(
        "INSERT INTO audit_events(document_id, user_id, event, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (document_id, user_id, event, json.dumps(details or {}, ensure_ascii=False), now()),
    )


def choices(connection: sqlite3.Connection) -> dict:
    return {
        "companies": connection.execute("SELECT * FROM companies ORDER BY name").fetchall(),
        "types": connection.execute("SELECT * FROM import_types ORDER BY name").fetchall(),
        "subtypes": connection.execute("SELECT * FROM subtypes ORDER BY type_id, name").fetchall(),
        "projects": connection.execute("SELECT * FROM projects ORDER BY name").fetchall(),
    }


def document_query(filters: dict[str, str], *, archived: bool = False) -> tuple[str, list]:
    sql = """
      SELECT d.*, c.name AS company_name, t.name AS type_name,
             s.name AS subtype_name, p.name AS project_name,
             u.username AS creator_name,
             COALESCE(f.received_minor, 0) AS received_minor,
             COALESCE(f.invoiced_minor, 0) AS invoiced_minor,
             COALESCE(f.paid_minor, 0) AS paid_minor,
             conf.id AS confirmation_id,
             conf.currency AS confirmed_currency,
             conf.amount_minor AS confirmed_amount_minor,
             conf.received_minor AS confirmed_received_minor,
             conf.unreceived_minor AS confirmed_unreceived_minor,
             conf.payable_minor AS confirmed_payable_minor,
             conf.paid_minor AS confirmed_paid_minor,
             conf.unpaid_minor AS confirmed_unpaid_minor,
             conf.invoiced_minor AS confirmed_invoiced_minor,
             conf.uninvoiced_minor AS confirmed_uninvoiced_minor,
             conf.note AS confirmed_note,
             conf.created_at AS confirmed_at,
             conf.created_by AS confirmed_by
      FROM documents d
      LEFT JOIN companies c ON c.id = d.company_id
      LEFT JOIN import_types t ON t.id = d.type_id
      LEFT JOIN subtypes s ON s.id = d.subtype_id
      LEFT JOIN projects p ON p.id = d.project_id
      LEFT JOIN users u ON u.id = d.created_by
      LEFT JOIN (
        SELECT document_id,
          SUM(CASE WHEN kind = 'receipt' THEN amount_minor ELSE 0 END) AS received_minor,
          SUM(CASE WHEN kind = 'invoice' THEN amount_minor ELSE 0 END) AS invoiced_minor,
          SUM(CASE WHEN kind = 'payment' THEN amount_minor ELSE 0 END) AS paid_minor
        FROM finance_records WHERE voided_at IS NULL GROUP BY document_id
      ) f ON f.document_id = d.id
      LEFT JOIN (
        SELECT document_id, MAX(id) AS confirmation_id FROM payment_confirmations GROUP BY document_id
      ) cl ON cl.document_id = d.id
      LEFT JOIN payment_confirmations conf ON conf.id = cl.confirmation_id
      WHERE d.archived_at IS """ + ("NOT NULL" if archived else "NULL")
    params: list = []
    for key, column in (
        ("company", "d.company_id"),
        ("type", "d.type_id"),
        ("subtype", "d.subtype_id"),
        ("project", "d.project_id"),
    ):
        if filters.get(key):
            sql += f" AND {column} = ?"
            params.append(int(filters[key]))
    if filters.get("status") in {"pending", "verified"}:
        sql += " AND d.review_status = ?"
        params.append(filters["status"])
    if filters.get('currency'):
        sql += ' AND d.currency = ?'
        params.append(filters['currency'])
    if filters.get('finance') == 'payable':
        sql += ' AND d.payable_minor IS NOT NULL AND d.payable_minor > COALESCE(f.paid_minor, 0)'
    elif filters.get('finance') == 'unpaid':
        sql += ' AND d.amount_minor IS NOT NULL AND d.amount_minor > COALESCE(f.received_minor, 0)'
    elif filters.get('finance') == 'uninvoiced':
        sql += ' AND d.amount_minor IS NOT NULL AND d.amount_minor > COALESCE(f.invoiced_minor, 0)'
    elif filters.get('finance') == 'amount_missing':
        sql += ' AND d.amount_minor IS NULL'
    elif filters.get('finance') == 'company_missing':
        sql += ' AND d.company_id IS NULL'
    elif filters.get('finance') == 'payable_missing':
        sql += ' AND d.payable_minor IS NULL'
    elif filters.get('finance') == 'exception':
        sql += ''' AND ((d.amount_minor IS NOT NULL AND (COALESCE(f.received_minor, 0) > d.amount_minor OR COALESCE(f.invoiced_minor, 0) > d.amount_minor))
                    OR (d.payable_minor IS NOT NULL AND COALESCE(f.paid_minor, 0) > d.payable_minor))'''
    elif filters.get('finance') == 'unconfirmed':
        sql += """ AND (conf.id IS NULL
                    OR conf.currency IS NOT d.currency
                    OR conf.amount_minor IS NOT d.amount_minor
                    OR conf.received_minor IS NOT COALESCE(f.received_minor, 0)
                    OR conf.unreceived_minor IS NOT (CASE WHEN d.amount_minor IS NULL THEN NULL ELSE MAX(d.amount_minor - COALESCE(f.received_minor, 0), 0) END)
                    OR conf.payable_minor IS NOT d.payable_minor
                    OR conf.paid_minor IS NOT COALESCE(f.paid_minor, 0)
                    OR conf.unpaid_minor IS NOT (CASE WHEN d.payable_minor IS NULL THEN NULL ELSE MAX(d.payable_minor - COALESCE(f.paid_minor, 0), 0) END)
                    OR conf.invoiced_minor IS NOT COALESCE(f.invoiced_minor, 0)
                    OR conf.uninvoiced_minor IS NOT (CASE WHEN d.amount_minor IS NULL THEN NULL ELSE MAX(d.amount_minor - COALESCE(f.invoiced_minor, 0), 0) END))"""
    elif filters.get('finance') == 'confirmed':
        sql += """ AND conf.id IS NOT NULL
                    AND conf.currency IS d.currency
                    AND conf.amount_minor IS d.amount_minor
                    AND conf.received_minor IS COALESCE(f.received_minor, 0)
                    AND conf.unreceived_minor IS (CASE WHEN d.amount_minor IS NULL THEN NULL ELSE MAX(d.amount_minor - COALESCE(f.received_minor, 0), 0) END)
                    AND conf.payable_minor IS d.payable_minor
                    AND conf.paid_minor IS COALESCE(f.paid_minor, 0)
                    AND conf.unpaid_minor IS (CASE WHEN d.payable_minor IS NULL THEN NULL ELSE MAX(d.payable_minor - COALESCE(f.paid_minor, 0), 0) END)
                    AND conf.invoiced_minor IS COALESCE(f.invoiced_minor, 0)
                    AND conf.uninvoiced_minor IS (CASE WHEN d.amount_minor IS NULL THEN NULL ELSE MAX(d.amount_minor - COALESCE(f.invoiced_minor, 0), 0) END)"""
    if filters.get("from"):
        sql += " AND d.signed_date >= ?"
        params.append(filters["from"])
    if filters.get("to"):
        sql += " AND d.signed_date <= ?"
        params.append(filters["to"])
    if filters.get("q"):
        needle = "%" + filters["q"].strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += """ AND (d.title LIKE ? ESCAPE '\\' OR d.contract_number LIKE ? ESCAPE '\\'
                    OR d.original_filename LIKE ? ESCAPE '\\' OR c.name LIKE ? ESCAPE '\\'
                    OR p.name LIKE ? ESCAPE '\\' OR d.extracted_text LIKE ? ESCAPE '\\')"""
        params.extend([needle] * 6)
    orders = {
        'updated': 'd.updated_at DESC',
        'receivable': 'd.currency, d.amount_minor DESC',
        'payable': 'd.currency, d.payable_minor DESC',
        'unreceived': 'd.currency, CASE WHEN d.amount_minor IS NULL THEN NULL ELSE MAX(d.amount_minor - COALESCE(f.received_minor, 0), 0) END DESC',
        'unpaid': 'd.currency, CASE WHEN d.payable_minor IS NULL THEN NULL ELSE MAX(d.payable_minor - COALESCE(f.paid_minor, 0), 0) END DESC',
    }
    sql += ' ORDER BY ' + orders.get(filters.get('sort'), 'd.created_at DESC') + ', d.id DESC'
    return sql, params


def document_detail_query() -> str:
    """Single-document query with finance totals and the latest payment confirmation."""
    return """
      SELECT d.*, c.name AS company_name, t.name AS type_name,
             s.name AS subtype_name, p.name AS project_name,
             u.username AS creator_name,
             COALESCE(f.received_minor, 0) AS received_minor,
             COALESCE(f.invoiced_minor, 0) AS invoiced_minor,
             COALESCE(f.paid_minor, 0) AS paid_minor,
             conf.id AS confirmation_id,
             conf.currency AS confirmed_currency,
             conf.amount_minor AS confirmed_amount_minor,
             conf.received_minor AS confirmed_received_minor,
             conf.unreceived_minor AS confirmed_unreceived_minor,
             conf.payable_minor AS confirmed_payable_minor,
             conf.paid_minor AS confirmed_paid_minor,
             conf.unpaid_minor AS confirmed_unpaid_minor,
             conf.invoiced_minor AS confirmed_invoiced_minor,
             conf.uninvoiced_minor AS confirmed_uninvoiced_minor,
             conf.note AS confirmed_note,
             conf.created_at AS confirmed_at,
             conf.created_by AS confirmed_by
      FROM documents d
      LEFT JOIN companies c ON c.id = d.company_id
      LEFT JOIN import_types t ON t.id = d.type_id
      LEFT JOIN subtypes s ON s.id = d.subtype_id
      LEFT JOIN projects p ON p.id = d.project_id
      LEFT JOIN users u ON u.id = d.created_by
      LEFT JOIN (
        SELECT document_id,
          SUM(CASE WHEN kind = 'receipt' THEN amount_minor ELSE 0 END) AS received_minor,
          SUM(CASE WHEN kind = 'invoice' THEN amount_minor ELSE 0 END) AS invoiced_minor,
          SUM(CASE WHEN kind = 'payment' THEN amount_minor ELSE 0 END) AS paid_minor
        FROM finance_records WHERE voided_at IS NULL GROUP BY document_id
      ) f ON f.document_id = d.id
      LEFT JOIN (
        SELECT document_id, MAX(id) AS confirmation_id FROM payment_confirmations GROUP BY document_id
      ) cl ON cl.document_id = d.id
      LEFT JOIN payment_confirmations conf ON conf.id = cl.confirmation_id
      WHERE d.id = ?
    """
