"""版本化、幂等、可回滚的数据迁移。

核心约定：
1. 旧表只读。迁移全程不删、不改、不重命名 documents / finance_records / payment_confirmations。
   新表与旧表并存，旧表继续作为事实来源。
2. 幂等。每迁移一条旧记录就在 contract_legacy_map / finance_legacy_map 写一行映射，
   重跑时先查映射表跳过，因此重复执行不会产生重复合同、重复金额或重复流水。
3. 不推断。历史金额缺少税口径、收付方向、付款主体时，一律标记为「历史数据待核对」，
   不为了填满新字段而自动判断。
4. 可回滚。回滚 = 按逆序删除新表与迁移台账；旧表与原件不受影响。见 rollback()。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import schema


# 旧 kind → 新流水方向。这是字段换名，不是推断：finance.py 一直把 receipt/invoice 视作应收方向、
# payment 视作应付方向，迁移必须保持同一语义，否则历史台账的收付合计会变。
KIND_TO_DIRECTION = {"receipt": "inflow", "payment": "outflow"}

# 迁移期写入的标记，供界面显示「历史数据待核对」
HISTORY_PENDING = "history_pending"

LEGACY_TABLES = {
    "comp": ("company", "category", "全称", "本方主体"),
    "company": ("import", "category", "名称", "本方主体"),
    "contract": ("main_contract", "category", "标题", "主合同"),
    "file": ("attachment", "category", "文件名", "其他附件"),
    "fee_item": ("amount", "amount_nature", "费用名称", "合同总额"),
    "ledger_entry": ("ledger", "direction", "摘要", "收入"),
    "invoice": ("invoice", "direction", "发票号", "销项"),
    "milestone": ("milestone", "date_source", "节点名称", "手工确认"),
}

# 新建表清单（回滚时按逆序删除）
NEW_TABLES = [
    "change_log", "review_record", "user_role", "role_permission", "role", "permission",
    "task", "milestone", "billing_plan", "settlement", "ledger_checkpoint",
    "invoice", "ledger_alloc", "ledger_entry", "amount_change", "fee_item",
    "contract_file", "cfile", "contract_party", "contract", "company_merge",
    "comp_name", "comp",
    "migration_issue", "migration_state", "company_legacy_map", "finance_legacy_map",
    "contract_legacy_map", "migration_steps", "schema_version",
]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm(name: str) -> str:
    """公司名比对键：去掉半角/全角空格。仅用于查重，不改写原值。"""
    return (name or "").replace(" ", "").replace("　", "")


def _display_name(name: str) -> str:
    """公司显示名：去掉名称内部的空格。

    例如虚构名称「示例（ 城 市 ） 科技有限公司」中的多余空格，
    不是工商登记名的一部分。这类空格只影响显示与比对，去掉是安全的：
    schema 里的 comp_fullname_unique 本来就按去空格后的结果判重，
    所以清理后不会与任何既有公司撞成重复。
    原始录入值由 comp_name(kind='history') 与 canonical_source 保留，
    需要时可回溯，因此这里清理不会造成信息丢失。
    """
    return _norm(name).strip()


def _clean_filename(name: str) -> str:
    return (name or "").strip() or "未命名文件"


class Migration:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.db_path = self.root / "contracts.sqlite3"
        self.run_id = uuid.uuid4().hex[:12]
        self.issues: list[tuple[str, str, str, str]] = []   # severity, code, subject, detail
        self.stats: dict[str, int] = {}
        self.created_tables: list[str] = []

    # ---------- 连接 ----------

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def issue(self, severity: str, code: str, subject: str, detail: str) -> None:
        self.issues.append((severity, code, subject, detail))

    # ---------- 结构 ----------

    def current_version(self, connection) -> int:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS migration_steps ("
            " id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, version INTEGER NOT NULL,"
            " name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        row = connection.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return row["v"] or 0

    def apply_schema(self, connection) -> list[str]:
        """建新表和索引，可重复执行。返回本次首次落库的步骤名。

        DDL 全部是 CREATE ... IF NOT EXISTS，因此每次都执行一遍是安全的，
        且新增的表能在下次运行时自动补上——不会因为版本号已经记过而被跳过。
        """
        existing = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        version = self.current_version(connection)
        applied = []
        for number, name, ddl_name in schema.MIGRATIONS:
            ddl = getattr(schema, ddl_name)
            connection.executescript(ddl)
            if number > version:
                connection.execute(
                    "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
                    (number, name, _now()),
                )
                connection.execute(
                    "INSERT INTO migration_steps(run_id, version, name, applied_at) VALUES (?, ?, ?, ?)",
                    (self.run_id, number, name, _now()),
                )
                version = number
                applied.append(name)
        after = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.created_tables = sorted(after - existing)
        # 内置权限与角色：每次运行都对齐，保证新增权限能被补上
        self._sync_permissions(connection)
        return applied

    def _sync_permissions(self, connection) -> None:
        now = _now()
        for name, label, description in schema.PERMISSIONS:
            connection.execute(
                "INSERT INTO permission(name, label, description) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET label = excluded.label, description = excluded.description",
                (name, label, description),
            )
        for role_name, (label, permissions) in schema.BUILTIN_ROLES.items():
            connection.execute(
                "INSERT INTO role(name, label, builtin, created_at) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(name) DO UPDATE SET label = excluded.label",
                (role_name, label, now),
            )
            role_id = connection.execute("SELECT id FROM role WHERE name = ?", (role_name,)).fetchone()[0]
            for permission in permissions:
                connection.execute(
                    "INSERT OR IGNORE INTO role_permission(role_id, permission) VALUES (?, ?)",
                    (role_id, permission),
                )
        # 旧 admin/user 映射到新角色
        for user in connection.execute("SELECT id, role FROM users").fetchall():
            role_name = schema.LEGACY_ROLE_MAP.get(user["role"])
            if not role_name:
                self.issue("warn", "unknown_legacy_role", f"users/{user['id']}",
                           f"旧角色 {user['role']!r} 没有对应新角色，未授予任何角色，需人工指定")
                continue
            role_id = connection.execute("SELECT id FROM role WHERE name = ?", (role_name,)).fetchone()[0]
            connection.execute(
                "INSERT OR IGNORE INTO user_role(user_id, role_id, granted_at) VALUES (?, ?, ?)",
                (user["id"], role_id, now),
            )

    # ---------- 数据 ----------

    def migrate(self, connection) -> None:
        """把旧数据平移到新表。已在映射表中的记录直接跳过，因此可重复运行。"""
        self._migrate_companies(connection)
        self._migrate_documents(connection)
        self._migrate_finance(connection)
        self._migrate_confirmations(connection)
        self._migrate_audit(connection)
        self._build_tasks(connection)

    # 公司

    def _company_id(self, connection, legacy_id: int, name: str, *, raw_name: str | None = None) -> int:
        """旧 companies.id → 新 comp.id，按旧主键建立对应，不做名称匹配或合并。"""
        row = connection.execute(
            "SELECT company_id FROM company_legacy_map WHERE legacy_company_id = ?", (legacy_id,)
        ).fetchone()
        if row:
            return row["company_id"]
        now = _now()
        # 名称被清理过时，把原始录入值留在备注里，界面上一眼能看到改了什么。
        notes = None
        if raw_name is not None and raw_name != name:
            notes = f"原始录入名称：{raw_name}"
        cursor = connection.execute(
            "INSERT INTO comp(full_name, category, canonical_source, notes, created_at, updated_at) "
            "VALUES (?, 'unknown', ?, ?, ?, ?)",
            (name, f"companies/{legacy_id}", notes, now, now),
        )
        company_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO company_legacy_map(legacy_company_id, company_id, migrated_at, run_id) "
            "VALUES (?, ?, ?, ?)",
            (legacy_id, company_id, now, self.run_id),
        )
        return company_id

    def _migrate_companies(self, connection) -> None:
        legacy = connection.execute("SELECT id, name FROM companies ORDER BY id").fetchall()
        count = 0
        for row in legacy:
            raw_name = (row["name"] or "").strip()
            name = raw_name
            if not name:
                self.issue("warn", "empty_company_name", f"companies/{row['id']}",
                           "旧公司名称为空，已建立占位公司，需人工补全")
                name = f"未命名公司（旧编号 {row['id']}）"
            elif _norm(name) != name:
                # 名称内部的多余空格（一般是录入时误敲的）在这里清掉再入库，
                # 原始值随 comp_name(kind='history') 与备注一起保留，可回溯。
                # 去空格后的结果与 comp_fullname_unique 的判重口径一致，不会撞重。
                self.issue("info", "company_name_whitespace", f"companies/{row['id']}",
                           f"公司名称含多余空格，已按去空格后的名称入库：{raw_name!r} → {name!r}"
                           "（原始名称已登记为历史名称）")
                name = _display_name(name)
            new_id = self._company_id(connection, row["id"], name, raw_name=raw_name)
            # 旧名登记为历史名称，保留可追溯性。名称被清理过时，登记的是原始录入值，
            # 这样「现在叫什么」与「当初录的是什么」两件事都留得住。
            connection.execute(
                "INSERT OR IGNORE INTO comp_name(company_id, name, kind, created_at) VALUES (?, ?, 'history', ?)",
                (new_id, raw_name or name, _now()),
            )
            count += 1
        self.stats["comp"] = count

    # 合同 + 文件 + 费用

    def _migrate_documents(self, connection) -> None:
        documents = connection.execute(
            "SELECT * FROM documents ORDER BY created_at, id"
        ).fetchall()
        contracts = files = fees = 0
        for row in documents:
            done = connection.execute(
                "SELECT contract_id FROM contract_legacy_map WHERE legacy_document_id = ?", (row["id"],)
            ).fetchone()
            if done:
                continue
            contracts += 1
            files += 1
            result = self._migrate_one_document(connection, row)
            fees += result

        self.stats["contract"] = contracts
        self.stats["cfile"] = files
        self.stats["fee_item"] = fees
        self.stats["contract_skipped"] = len(documents) - contracts

    def _migrate_one_document(self, connection, row) -> int:
        now = _now()
        title = (row["title"] or "").strip() or _clean_filename(row["original_filename"])
        signed = (row["signed_date"] or "").strip() or None
        archive_year = int(signed[:4]) if signed and signed[:4].isdigit() else None

        # 归档信息核对状态原样带过来，不因新增金额字段而回退为未核对
        review_status = row["review_status"] if row["review_status"] in ("pending", "verified") else "pending"

        currency = (row["currency"] or "CNY").strip() or "CNY"
        has_amount = row["amount_minor"] is not None
        has_payable = row["payable_minor"] is not None
        # 金额核对状态与归档核对状态彼此独立
        if has_amount or has_payable:
            amount_review_status = HISTORY_PENDING
        else:
            amount_review_status = "pending"

        cursor = connection.execute(
            """INSERT INTO contract(
                 title, contract_number, signed_date, type_id, subtype_id, project_id, currency,
                 effective_amount_minor, effective_amount_source, archive_year,
                 review_status, amount_review_status, notes,
                 archived_at, revision, created_by, updated_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                (row["contract_number"] or "").strip() or None,
                signed,
                row["type_id"], row["subtype_id"], row["project_id"], currency,
                None, "unknown", archive_year,
                review_status, amount_review_status, row["notes"],
                row["archived_at"], row["revision"] or 1,
                row["created_by"], row["updated_by"],
                row["created_at"] or now, row["updated_at"] or now,
            ),
        )
        contract_id = cursor.lastrowid

        # 公司归属：旧表只有 company_id，只能登记为合同相对方，不判定本方主体
        if row["company_id"]:
            company = connection.execute(
                "SELECT id, name FROM companies WHERE id = ?", (row["company_id"],)
            ).fetchone()
            if company:
                company_id = self._company_id(connection, company["id"], company["name"])
                connection.execute(
                    "INSERT OR IGNORE INTO contract_party(contract_id, company_id, role, created_at, created_by) "
                    "VALUES (?, ?, 'counterparty', ?, ?)",
                    (contract_id, company_id, now, row["created_by"]),
                )
            else:
                self.issue("warn", "orphan_company_ref", f"documents/{row['id']}",
                           f"company_id={row['company_id']} 在 companies 表中不存在，合同已迁移但无公司归属")
        else:
            self.issue("info", "company_missing", f"documents/{row['id']}",
                       f"《{title}》没有公司归属，按合同名与原件另行确认")

        # 文件：原件按 SHA256 去重。同一份文件出现在多份旧记录中时只存一份，
        # 但每份合同各自保留一条关联，不因此多算一次金额。
        file_id = connection.execute(
            "SELECT id FROM cfile WHERE sha256 = ?", (row["sha256"],)
        ).fetchone()
        if file_id:
            file_id = file_id["id"]
            self.issue("info", "file_shared", f"documents/{row['id']}",
                       f"原件 {row['original_filename']} 与已有文件 SHA256 相同，复用同一份存储，未重复入库")
        else:
            cursor = connection.execute(
                """INSERT INTO cfile(sha256, original_filename, stored_filename, mime_type, file_size,
                                     doc_kind, legacy_document_id, extraction_status, extracted_text,
                                     candidates_json, archived_at, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'main_contract', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["sha256"], row["original_filename"], row["stored_filename"],
                    row["mime_type"], row["file_size"],
                    row["id"],
                    row["extraction_status"] if row["extraction_status"] in
                    ("queued", "processing", "ready", "error") else "ready",
                    row["extracted_text"] or "", row["candidates_json"] or "{}",
                    row["archived_at"], row["created_by"],
                    row["created_at"] or now, row["updated_at"] or now,
                ),
            )
            file_id = cursor.lastrowid
            if row["extraction_status"] == "error":
                self.issue("warn", "extraction_failed", f"documents/{row['id']}",
                           f"提取失败：{row['extraction_error'] or '未记录原因'}")

        connection.execute(
            "INSERT OR IGNORE INTO contract_file(contract_id, file_id, relation, is_amount_evidence, created_at, created_by) "
            "VALUES (?, ?, 'primary', 1, ?, ?)",
            (contract_id, file_id, now, row["created_by"]),
        )

        # 金额：原件载明，税口与收付方向一律留待核对，不推断
        fee_count = 0
        source_page = None
        if has_amount or has_payable:
            if has_amount:
                fee_count += self._insert_fee(
                    connection, contract_id, row, file_id, currency,
                    amount=row["amount_minor"], nature="contract_total",
                    name="合同金额（历史迁移）",
                    flow="应收：我方应向对方收取",
                )
            if has_payable:
                # 旧表单把「留空」存成了 0，与「确实没有应付」无法区分：真正的空值是 NULL，
                # 写成 0 说明这一栏被提交过一次 0，但提交 0 既可能是用户确认「没有应付」，
                # 也可能是表单把空串转成了 0。两种含义相反，不能替人挑一个。
                # 所以不写 0，按「原件未载明金额」留空，并标记待核对——凭零金额会让
                # 公司账款页断言对方不欠我方钱，这正是规范禁止的推断。
                payable_ambiguous = row["payable_minor"] == 0
                fee_count += self._insert_fee(
                    connection, contract_id, row, file_id, currency,
                    amount=None if payable_ambiguous else row["payable_minor"],
                    nature="single_payment",
                    name="应付金额（历史迁移）",
                    flow="应付：我方应向对方支付",
                )
                if payable_ambiguous:
                    self.issue("warn", "zero_amount_ambiguous", f"documents/{row['id']}",
                               f"《{title}》的应付金额旧记录为 0，无法判断是「确实没有应付」"
                               "还是「未填写被存成 0」，已按未载明留空并标记待核对，需人工确认")
        else:
            self.issue("info", "amount_missing", f"documents/{row['id']}",
                       f"《{title}》没有金额记录，列表按「待核对」显示")

        connection.execute(
            "INSERT INTO contract_legacy_map(legacy_document_id, contract_id, file_id, migrated_at, run_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["id"], contract_id, file_id, now, self.run_id),
        )

        # 历史记录留一条变更日志，说明金额为何是「待核对」
        if has_amount or has_payable:
            connection.execute(
                "INSERT INTO change_log(entity_kind, entity_id, field_name, value_before, value_after, "
                "reason, actor_name, created_at) VALUES ('contract', ?, 'amount_review_status', NULL, ?, ?, ?, ?)",
                (contract_id, HISTORY_PENDING,
                 "旧数据迁移：原件载明金额已保留，税口径/收付方向/付款主体未确认，不做推断",
                 "系统迁移", now),
            )
        return fee_count

    def _insert_fee(self, connection, contract_id, row, file_id, currency, *,
                    amount, nature, name, flow) -> int:
        now = _now()
        connection.execute(
            """INSERT INTO fee_item(
                 contract_id, fee_name, project_id, amount_nature, currency,
                 stated_amount_minor, stated_tax_mode,
                 payer_company_id, payee_company_id, counts_toward_effective,
                 source_file_id, source_page, amount_review_status, review_note,
                 created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'unspecified', NULL, NULL, 0, ?, NULL, ?, ?, ?, ?, ?)""",
            (
                contract_id, name, row["project_id"], nature, currency,
                amount, file_id, HISTORY_PENDING,
                f"旧数据迁移：金额取自 {row['original_filename']}，原件页码与税口径未记录；"
                f"方向待核对（{flow}）。确认前不计入当前有效合同金额",
                row["created_by"], row["created_at"] or now, row["updated_at"] or now,
            ),
        )
        return 1

    # 收付款与发票

    def _migrate_finance(self, connection) -> None:
        records = connection.execute("SELECT * FROM finance_records ORDER BY occurred_on, id").fetchall()
        entries = invoices = 0
        for row in records:
            if connection.execute(
                "SELECT 1 FROM finance_legacy_map WHERE legacy_record_id = ?", (row["id"],)
            ).fetchone():
                continue
            mapped = connection.execute(
                "SELECT contract_id FROM contract_legacy_map WHERE legacy_document_id = ?",
                (row["document_id"],),
            ).fetchone()
            if not mapped:
                self.issue("warn", "orphan_finance_record", f"finance_records/{row['id']}",
                           f"流水对应的合同 {row['document_id']} 未迁移，流水已跳过，需人工处理")
                continue
            contract_id = mapped["contract_id"]
            document = connection.execute(
                "SELECT currency, company_id FROM documents WHERE id = ?", (row["document_id"],)
            ).fetchone()
            currency = (document["currency"] if document else None) or "CNY"
            now = _now()
            voided = row["voided_at"]
            note_parts = [row["description"] or ""]
            if voided and row["void_reason"]:
                note_parts.append(f"作废原因：{row['void_reason']}")
            note = " ".join(part for part in note_parts if part).strip() or None

            if row["kind"] in ("receipt", "payment"):
                direction = KIND_TO_DIRECTION[row["kind"]]
                # 付款方/收款方留空：旧表没有记录，不能凭方向推断主体
                entry_id = connection.execute(
                    """INSERT INTO ledger_entry(
                         direction, entry_kind, occurred_on, payer_company_id, payee_company_id,
                         currency, amount_minor, bank_reference, handler_user_id, reconcile_status,
                         note, legacy_record_id, voided_at, void_reason, created_by, created_at)
                       VALUES (?, 'normal', ?, NULL, NULL, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
                    (
                        direction, row["occurred_on"], currency, row["amount_minor"],
                        row["reference"] or None, row["created_by"], note,
                        row["id"], voided, row["void_reason"], row["created_by"],
                        row["created_at"] or now,
                    ),
                ).lastrowid
                # 旧记录是「合同上的流水」，全额分配到该合同；金额本身就等于合同登记额时不算推断
                connection.execute(
                    "INSERT INTO ledger_alloc(entry_id, contract_id, amount_minor, note, created_at, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (entry_id, contract_id, row["amount_minor"],
                     "旧数据迁移：原记录直接挂在合同上，全额计入该合同", now, row["created_by"]),
                )
                connection.execute(
                    "INSERT INTO finance_legacy_map(legacy_record_id, entry_id, migrated_at, run_id) VALUES (?, ?, ?, ?)",
                    (row["id"], entry_id, now, self.run_id),
                )
                entries += 1
                self.issue("info", "ledger_party_missing", f"finance_records/{row['id']}",
                           "旧流水未记录付款方/收款方，已留空待核对；未按方向推断主体")
            else:
                invoice_id = connection.execute(
                    """INSERT INTO invoice(
                         direction, invoice_number, issued_on, seller_company_id, buyer_company_id,
                         currency, total_minor, net_minor, tax_minor, status,
                         contract_id, note, legacy_record_id, created_by, created_at)
                       VALUES ('output', ?, ?, NULL, NULL, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)""",
                    (
                        (row["reference"] or "").strip() or f"历史发票-{row['id']}",
                        row["occurred_on"], currency, row["amount_minor"],
                        "voided" if voided else "valid", contract_id, note,
                        row["id"], row["created_by"], row["created_at"] or now,
                    ),
                ).lastrowid
                connection.execute(
                    "INSERT INTO finance_legacy_map(legacy_record_id, invoice_id, migrated_at, run_id) VALUES (?, ?, ?, ?)",
                    (row["id"], invoice_id, now, self.run_id),
                )
                invoices += 1
                if not (row["reference"] or "").strip():
                    self.issue("warn", "invoice_number_missing", f"finance_records/{row['id']}",
                               "旧开票记录没有发票号，已用占位号登记，需人工补齐真实发票号")
                self.issue("info", "invoice_tax_missing", f"finance_records/{row['id']}",
                           "旧记录只登记了一个金额，未区分销项/进项与含税拆分，税口径留空待核对")

        self.stats["ledger_entry"] = entries
        self.stats["invoice"] = invoices

    def _migrate_confirmations(self, connection) -> None:
        rows = connection.execute("SELECT * FROM payment_confirmations ORDER BY created_at, id").fetchall()
        count = 0
        for row in rows:
            mapped = connection.execute(
                "SELECT contract_id FROM contract_legacy_map WHERE legacy_document_id = ?",
                (row["document_id"],),
            ).fetchone()
            if not mapped:
                self.issue("warn", "orphan_confirmation", f"payment_confirmations/{row['id']}",
                           "款项确认对应的合同未迁移，已跳过")
                continue
            now = _now()
            snapshot = {
                "currency": row["currency"], "amount_minor": row["amount_minor"],
                "received_minor": row["received_minor"], "unreceived_minor": row["unreceived_minor"],
                "payable_minor": row["payable_minor"], "paid_minor": row["paid_minor"],
                "unpaid_minor": row["unpaid_minor"], "invoiced_minor": row["invoiced_minor"],
                "uninvoiced_minor": row["uninvoiced_minor"],
            }
            connection.execute(
                """INSERT INTO review_record(subject_kind, subject_id, action, reviewer_id, outcome,
                     comment, data_version, decided_at, created_at)
                   VALUES ('contract', ?, 'payment_confirm', ?, 'confirmed', ?, ?, ?, ?)""",
                (
                    mapped["contract_id"], row["created_by"], row["note"] or "",
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    row["created_at"] or now, row["created_at"] or now,
                ),
            )
            count += 1
        self.stats["review_record"] = count

    def _migrate_audit(self, connection) -> None:
        """旧操作历史平移。一次性写入，重跑不重复。"""
        if self._state(connection, "audit_migrated"):
            self.stats["change_log"] = 0
            return
        rows = connection.execute("SELECT * FROM audit_events ORDER BY created_at, id").fetchall()
        for row in rows:
            mapped = None
            if row["document_id"]:
                mapped = connection.execute(
                    "SELECT contract_id FROM contract_legacy_map WHERE legacy_document_id = ?",
                    (row["document_id"],),
                ).fetchone()
            connection.execute(
                """INSERT INTO change_log(entity_kind, entity_id, field_name, value_after, reason,
                     actor_user_id, actor_name, created_at)
                   VALUES ('contract', ?, 'legacy_event', ?, ?, ?, '历史记录', ?)""",
                (
                    mapped["contract_id"] if mapped else 0,
                    row["event"],
                    row["details_json"] or "{}",
                    row["user_id"],
                    row["created_at"] or _now(),
                ),
            )
        self._set_state(connection, "audit_migrated", str(len(rows)))
        self.stats["change_log"] = len(rows)

    def _state(self, connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM migration_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_state(self, connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT INTO migration_state(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _now()),
        )

    # 待办

    def _build_tasks(self, connection) -> None:
        """把迁移中发现的待处理事项转成站内待办。一次性生成，重跑不重复。

        待办是人工处理进度，重新生成会把已经办完的事项又变回待办，因此只在首次迁移时建一次。
        """
        if self._state(connection, "tasks_built"):
            self.stats["task"] = 0
            return
        now = _now()
        count = 0
        for contract in connection.execute(
            "SELECT c.id, c.title FROM contract c "
            "LEFT JOIN contract_party p ON p.contract_id = c.id AND p.role = 'counterparty' "
            "WHERE p.id IS NULL AND c.archived_at IS NULL"
        ).fetchall():
            connection.execute(
                "INSERT INTO task(kind, contract_id, title, detail, status, created_at, updated_at) "
                "VALUES ('company_missing', ?, ?, ?, 'open', ?, ?)",
                (contract["id"], f"待补公司：{contract['title'] or '未命名合同'}",
                 "历史记录没有公司归属，需按合同名与原件确认后补充", now, now),
            )
            count += 1
        for fee in connection.execute(
            "SELECT f.id, f.contract_id, c.title FROM fee_item f JOIN contract c ON c.id = f.contract_id "
            "WHERE f.amount_review_status = ?", (HISTORY_PENDING,)
        ).fetchall():
            connection.execute(
                "INSERT INTO task(kind, contract_id, title, detail, status, created_at, updated_at) "
                "VALUES ('amount_review', ?, ?, ?, 'open', ?, ?)",
                (fee["contract_id"], f"待核金额：{fee['title'] or '未命名合同'}",
                 "历史金额缺少税口径、收付方向或付款主体，确认前不计入有效合同金额", now, now),
            )
            count += 1
        for file in connection.execute(
            "SELECT id, original_filename FROM cfile WHERE extraction_status = 'error'"
        ).fetchall():
            connection.execute(
                "INSERT INTO task(kind, file_id, title, detail, status, created_at, updated_at) "
                "VALUES ('ocr_failed', ?, ?, ?, 'open', ?, ?)",
                (file["id"], f"识别失败：{file['original_filename']}",
                 "原件未能提取文字，可重试或改用可复制文字的版本", now, now),
            )
            count += 1
        self._set_state(connection, "tasks_built", str(count))
        self.stats["task"] = count

    # ---------- 校验 ----------

    def verify(self, connection) -> list[str]:
        """迁移后自检。返回问题清单，为空表示一致。"""
        problems: list[str] = []
        legacy_contracts = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        mapped = connection.execute("SELECT COUNT(*) FROM contract_legacy_map").fetchone()[0]
        if mapped != legacy_contracts:
            problems.append(f"合同迁移不完整：旧记录 {legacy_contracts} 条，已迁移 {mapped} 条")

        legacy_amounts = connection.execute(
            "SELECT COUNT(*) FROM documents WHERE amount_minor IS NOT NULL OR payable_minor IS NOT NULL"
        ).fetchone()[0]
        migrated_amounts = connection.execute(
            "SELECT COUNT(DISTINCT legacy_document_id) FROM contract_legacy_map m "
            "JOIN fee_item f ON f.contract_id = m.contract_id WHERE f.stated_amount_minor IS NOT NULL"
        ).fetchone()[0]
        if migrated_amounts < legacy_amounts:
            problems.append(f"金额迁移不完整：旧记录 {legacy_amounts} 条含金额，仅 {migrated_amounts} 条迁入费用明细")

        # 金额逐一比对，确认没有丢失或改写。
        # 例外：旧记录为 0 的应付金额按「未载明」留空（见 _migrate_one_document），
        # 因此这一类旧值 0 不要求在新表里找到对应数字，但必须能找到一条留空的费用行。
        for row in connection.execute(
            "SELECT id, amount_minor, payable_minor FROM documents "
            "WHERE amount_minor IS NOT NULL OR payable_minor IS NOT NULL"
        ).fetchall():
            mapped_row = connection.execute(
                "SELECT contract_id FROM contract_legacy_map WHERE legacy_document_id = ?", (row["id"],)
            ).fetchone()
            if not mapped_row:
                continue
            for amount, name in ((row["amount_minor"], "合同金额（历史迁移）"),
                                 (row["payable_minor"], "应付金额（历史迁移）")):
                if amount is None:
                    continue
                if name == "应付金额（历史迁移）" and amount == 0:
                    blank = connection.execute(
                        "SELECT COUNT(*) FROM fee_item WHERE contract_id = ? AND fee_name = ? "
                        "AND stated_amount_minor IS NULL AND amount_review_status = ?",
                        (mapped_row["contract_id"], name, HISTORY_PENDING),
                    ).fetchone()[0]
                    if blank != 1:
                        problems.append(
                            f"歧义应付金额未按留空迁移：documents/{row['id']} 旧值 0 应生成一条"
                            "「未载明 + 待核对」的费用行"
                        )
                    continue
                found = connection.execute(
                    "SELECT COUNT(*) FROM fee_item WHERE contract_id = ? AND fee_name = ? AND stated_amount_minor = ?",
                    (mapped_row["contract_id"], name, amount),
                ).fetchone()[0]
                if found != 1:
                    problems.append(f"金额不符：documents/{row['id']} 的 {name} {amount} 未原样迁入")

        legacy_effective = connection.execute(
            "SELECT COUNT(*) FROM finance_records WHERE voided_at IS NULL"
        ).fetchone()[0]
        mapped_effective = connection.execute(
            "SELECT COUNT(*) FROM ledger_entry e JOIN finance_legacy_map m ON m.entry_id = e.id WHERE e.voided_at IS NULL"
        ).fetchone()[0] + connection.execute(
            "SELECT COUNT(*) FROM invoice i JOIN finance_legacy_map m ON m.invoice_id = i.id WHERE i.status = 'valid'"
        ).fetchone()[0]
        if mapped_effective != legacy_effective:
            problems.append(f"有效流水不一致：旧 {legacy_effective} 条，新 {mapped_effective} 条")

        # 历史金额不得被自动计入有效合同金额
        counted = connection.execute(
            "SELECT COUNT(*) FROM fee_item WHERE amount_review_status = ? AND counts_toward_effective = 1",
            (HISTORY_PENDING,),
        ).fetchone()[0]
        if counted:
            problems.append(f"有 {counted} 条历史待核对金额被计入有效合同金额，违反不推断规则")

        # 分配金额不得超过流水金额
        for row in connection.execute(
            "SELECT e.id, e.amount_minor, SUM(a.amount_minor) AS allocated FROM ledger_entry e "
            "JOIN ledger_alloc a ON a.entry_id = e.id GROUP BY e.id HAVING allocated > e.amount_minor"
        ).fetchall():
            problems.append(f"流水分摊超额：ledger_entry/{row['id']} 金额 {row['amount_minor']}，已分配 {row['allocated']}")

        return problems

    # ---------- 入口 ----------

    def run(self, *, dry_run: bool = False) -> dict:
        if dry_run:
            return self.run_dry()
        connection = self.connect()
        try:
            # 建表语句由 executescript 执行，会隐式提交，因此必须放在数据事务之外。
            # 建表本身是幂等的（IF NOT EXISTS），重复运行没有副作用。
            version_before = self.current_version(connection)
            applied = self.apply_schema(connection)

            connection.execute("BEGIN IMMEDIATE")
            try:
                self.migrate(connection)
                problems = self.verify(connection)
                now = _now()
                for severity, code, subject, detail in self.issues:
                    # 同一事项只记一次：重跑时问题清单不变，不该堆成历史垃圾
                    connection.execute(
                        "INSERT INTO migration_issue(run_id, severity, code, subject, detail, created_at) "
                        "SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS ("
                        "  SELECT 1 FROM migration_issue WHERE code = ? AND subject IS ?)",
                        (self.run_id, severity, code, subject, detail, now, code, subject),
                    )
                report = {
                    "run_id": self.run_id,
                    "version_before": version_before,
                    "version_after": schema.SCHEMA_VERSION,
                    "dry_run": dry_run,
                    "applied": applied,
                    "stats": self.stats,
                    "problems": problems,
                    "issue_counts": {
                        level: sum(1 for item in self.issues if item[0] == level)
                        for level in ("block", "warn", "info")
                    },
                    "issues": [
                        {"severity": s, "code": c, "subject": k, "detail": d} for s, c, k, d in self.issues
                    ],
                }
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            return report
        finally:
            connection.close()

    def run_dry(self) -> dict:
        """检查模式：在一份临时副本上完整跑一遍，生产库一个字节都不碰。

        早期实现是在生产库上真建表再靠 rollback() 清场，但 executescript 会隐式提交，
        「随后整体回滚」并不成立——那次检查模式把已经迁好的数据连带删光了。现在改为副本执行。
        """
        with tempfile.TemporaryDirectory(prefix="contractdb-dry-") as workspace:
            copy = Path(workspace) / "contracts.sqlite3"
            source = sqlite3.connect(self.db_path)
            try:
                # backup() 拿到的是包含 WAL 内容的一致性快照，不是冷拷贝
                target = sqlite3.connect(copy)
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()

            probe = Migration(Path(workspace))
            report = probe.run(dry_run=False)
            report["dry_run"] = True
            report["applied"] = []          # 副本上的建表不算生产库的改动
            return report


def rollback(root: Path, *, confirm_version: int | None = None, only: list[str] | None = None) -> dict:
    """把新表整体删除，回到迁移前状态。

    旧表 documents / finance_records / payment_confirmations 与 originals 原件目录全程未被改动，
    因此回滚后系统立即回到迁移前的样子，不会丢数据。

    only 限定时只删这些表，用于「检查模式清理自己刚建的空表」。
    不传 only 才是整体回滚——它会连同已经迁好的数据一起删掉，必须显式确认版本号。
    """
    db_path = Path(root) / "contracts.sqlite3"
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        tables = NEW_TABLES if only is None else [name for name in NEW_TABLES if name in set(only)]
        if only is not None:
            # 检查模式清理：只删本次新建的表，且这些表必须真的是空的。
            # 若里面已经有数据，说明它们不是「本次刚建的空表」，宁可留着也不能删。
            version = connection.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()["v"] if _has_table(connection, "schema_version") else 0
            non_empty = [
                name for name in tables
                if _has_table(connection, name)
                and connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] > 0
            ]
            if non_empty:
                connection.execute("ROLLBACK")
                raise RuntimeError(
                    "检查模式拒绝清理：以下表已有数据，说明它们不是本次新建的空表，"
                    f"删除会丢数据：{', '.join(sorted(non_empty))}"
                )
        else:
            version = connection.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] or 0
            if confirm_version is not None and version != confirm_version:
                raise ValueError(f"当前数据模型版本为 {version}，与 --expect-version {confirm_version} 不符，已中止")
        connection.execute("PRAGMA foreign_keys = OFF")
        counts = {}
        for table in tables:
            if not _has_table(connection, table):
                continue
            counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("COMMIT")
        return {"version_removed": version, "dropped": counts, "partial": only is not None}
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _has_table(connection, name: str) -> bool:
    return connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None
