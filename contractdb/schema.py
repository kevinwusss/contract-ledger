"""新数据模型的表定义。

设计原则：
- 旧表（documents / finance_records / ...）在迁移后原封保留，作为事实来源，不删除、不改写。
- 新表与旧表共存，通过 contract_legacy_map / migration_steps 建立可追溯的对应关系。
- 回滚 = 删除新表，旧表不受影响。
"""

from __future__ import annotations

# 当前数据模型版本。每新增一个迁移步骤就 +1。
SCHEMA_VERSION = 1

# 迁移步骤：(版本号, 名称, DDL 列表)。DDL 必须可重复执行（IF NOT EXISTS / 由迁移器保证）。
MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "企业合同与账款数据模型", "MODEL_001"),
]


MODEL_001 = """
-- ============ 公司主数据 ============
CREATE TABLE IF NOT EXISTS comp (
  id INTEGER PRIMARY KEY,
  full_name TEXT NOT NULL,
  short_name TEXT,
  credit_code TEXT,
  category TEXT NOT NULL DEFAULT 'unknown'
    CHECK (category IN ('self', 'customer', 'supplier', 'other', 'unknown')),
  active INTEGER NOT NULL DEFAULT 1,
  notes TEXT,
  canonical_source TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- 公司全称唯一（按去除空白后的键），防止同义名称重复建号
CREATE UNIQUE INDEX IF NOT EXISTS comp_fullname_unique
  ON comp(replace(replace(full_name, ' ', ''), '　', ''));
CREATE INDEX IF NOT EXISTS comp_category_idx ON comp(category, active);

-- 别名与历史名称：搜索用，不参与自动合并
CREATE TABLE IF NOT EXISTS comp_name (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES comp(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'alias' CHECK (kind IN ('alias', 'history', 'short')),
  note TEXT,
  created_at TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS comp_name_company_idx ON comp_name(company_id);
CREATE UNIQUE INDEX IF NOT EXISTS comp_name_unique
  ON comp_name(company_id, replace(replace(name, ' ', ''), '　', ''));

-- 公司合并记录：必须展示受影响数量并留痕
CREATE TABLE IF NOT EXISTS company_merge (
  id INTEGER PRIMARY KEY,
  source_company_id INTEGER NOT NULL REFERENCES comp(id),
  target_company_id INTEGER NOT NULL REFERENCES comp(id),
  affected_contracts INTEGER NOT NULL DEFAULT 0,
  affected_projects INTEGER NOT NULL DEFAULT 0,
  affected_fee_items INTEGER NOT NULL DEFAULT 0,
  affected_ledger INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  performed_by INTEGER REFERENCES users(id),
  performed_at TEXT NOT NULL
);

-- ============ 合同档案 ============
CREATE TABLE IF NOT EXISTS contract (
  id INTEGER PRIMARY KEY,
  title TEXT,
  contract_number TEXT,
  signed_date TEXT,
  type_id INTEGER REFERENCES import_types(id),
  subtype_id INTEGER REFERENCES subtypes(id),
  project_id INTEGER REFERENCES projects(id),
  currency TEXT NOT NULL DEFAULT 'CNY',
  -- 当前有效合同金额（分）。由 fee_items 中计入有效金额的部分汇总而来，供列表快速展示。
  effective_amount_minor INTEGER,
  effective_amount_source TEXT NOT NULL DEFAULT 'unknown'
    CHECK (effective_amount_source IN ('unknown', 'fee_items', 'amendment')),
  archive_year INTEGER,
  review_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (review_status IN ('pending', 'verified')),
  amount_review_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (amount_review_status IN ('pending', 'verified', 'history_pending')),
  fulfillment_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK (fulfillment_status IN ('unknown', 'performing', 'completed', 'terminated')),
  notes TEXT,
  archived_at TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER REFERENCES users(id),
  updated_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contract_filter_idx
  ON contract(review_status, amount_review_status, archived_at, signed_date);
CREATE INDEX IF NOT EXISTS contract_number_idx ON contract(contract_number);

-- 合同参与方：一笔金额归属哪家公司，由这里决定，不由公司名匹配决定
CREATE TABLE IF NOT EXISTS contract_party (
  id INTEGER PRIMARY KEY,
  contract_id INTEGER NOT NULL REFERENCES contract(id),
  company_id INTEGER NOT NULL REFERENCES comp(id),
  role TEXT NOT NULL CHECK (role IN (
    'self',            -- 本方签约主体
    'counterparty',    -- 合同相对方
    'participant',     -- 其他参与方
    'payment_payer',   -- 实际付款义务承担方
    'payment_payee'    -- 实际收款方
  )),
  note TEXT,
  created_at TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS contract_party_contract_idx ON contract_party(contract_id, role);
CREATE INDEX IF NOT EXISTS contract_party_company_idx ON contract_party(company_id);
CREATE UNIQUE INDEX IF NOT EXISTS contract_party_unique
  ON contract_party(contract_id, company_id, role);

-- ============ 上传文件 ============
CREATE TABLE IF NOT EXISTS cfile (
  id INTEGER PRIMARY KEY,
  sha256 TEXT NOT NULL,
  original_filename TEXT NOT NULL,
  stored_filename TEXT,
  mime_type TEXT,
  file_size INTEGER,
  doc_kind TEXT NOT NULL DEFAULT 'main_contract' CHECK (doc_kind IN (
    'main_contract',      -- 主合同
    'amendment',          -- 补充协议
    'purchase_order',     -- 采购订单
    'tech_agreement',     -- 技术协议
    'acceptance',         -- 验收资料
    'registration',       -- 登记证明
    'invoice',            -- 发票
    'payment_voucher',    -- 付款凭证
    'attachment',         -- 其他附件
    'unknown'
  )),
  legacy_document_id TEXT,
  extraction_status TEXT NOT NULL DEFAULT 'queued',
  extracted_text TEXT NOT NULL DEFAULT '',
  candidates_json TEXT NOT NULL DEFAULT '{}',
  archived_at TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- 同一 SHA256 只存一份文件
CREATE UNIQUE INDEX IF NOT EXISTS cfile_sha_unique ON cfile(sha256);
CREATE INDEX IF NOT EXISTS cfile_kind_idx ON cfile(doc_kind, archived_at);

-- 合同 ↔ 文件：多对多。同一文件可关联到不同公司的合同，但不因此产生第二笔金额。
CREATE TABLE IF NOT EXISTS contract_file (
  id INTEGER PRIMARY KEY,
  contract_id INTEGER NOT NULL REFERENCES contract(id),
  file_id INTEGER NOT NULL REFERENCES cfile(id),
  relation TEXT NOT NULL DEFAULT 'primary' CHECK (relation IN (
    'primary',      -- 合同主记录
    'signed',       -- 正式签署版本
    'historical',   -- 历史版本
    'amendment',    -- 补充协议
    'order',        -- 相关订单
    'tech',         -- 技术附件
    'acceptance',   -- 验收资料
    'registration', -- 登记证明
    'evidence'      -- 其他证明材料
  )),
  is_amount_evidence INTEGER NOT NULL DEFAULT 0,
  amount_evidence_note TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS contract_file_contract_idx ON contract_file(contract_id, relation);
CREATE INDEX IF NOT EXISTS contract_file_file_idx ON contract_file(file_id);
CREATE UNIQUE INDEX IF NOT EXISTS contract_file_unique ON contract_file(contract_id, file_id, relation);

-- ============ 金额结构 ============
-- 每笔费用单独成行，税口径、承担方、收款方逐笔记录，不用一个税率套整份合同。
CREATE TABLE IF NOT EXISTS fee_item (
  id INTEGER PRIMARY KEY,
  contract_id INTEGER NOT NULL REFERENCES contract(id),
  fee_name TEXT NOT NULL,
  project_id INTEGER REFERENCES projects(id),
  fee_category TEXT,                -- 开发费 / 模具费 / 试验费 / 产品货款 / ...
  amount_nature TEXT NOT NULL DEFAULT 'contract_total' CHECK (amount_nature IN (
    'contract_total',  -- 合同总额
    'single_payment',  -- 单独支付
    'amortized',       -- 摊销
    'allocated',       -- 分摊
    'order_amount',    -- 订单金额
    'penalty',         -- 违约金
    'unit_price'       -- 单价
  )),
  currency TEXT NOT NULL DEFAULT 'CNY',
  -- 原件载明金额：只放原件上确实写着的那一个数
  stated_amount_minor INTEGER,
  stated_tax_mode TEXT NOT NULL DEFAULT 'unspecified'
    CHECK (stated_tax_mode IN ('inclusive', 'exclusive', 'exempt', 'unspecified')),
  -- 计算值：只有税率与计税方式都确认后才写入
  tax_inclusive_minor INTEGER,
  tax_exclusive_minor INTEGER,
  tax_amount_minor INTEGER,
  tax_rate TEXT,
  tax_note TEXT,
  computed_note TEXT,               -- 标注哪个字段是计算值
  payer_company_id INTEGER REFERENCES comp(id),
  payee_company_id INTEGER REFERENCES comp(id),
  counts_toward_effective INTEGER NOT NULL DEFAULT 1,
  source_file_id INTEGER REFERENCES cfile(id),
  source_page TEXT,
  amount_review_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (amount_review_status IN ('pending', 'verified', 'history_pending', 'anomaly')),
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  review_note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fee_item_contract_idx ON fee_item(contract_id, counts_toward_effective);
CREATE INDEX IF NOT EXISTS fee_item_review_idx ON fee_item(amount_review_status);
CREATE INDEX IF NOT EXISTS fee_item_party_idx ON fee_item(payer_company_id, payee_company_id);

-- 补充协议对金额的改变：保留原金额与变更依据
CREATE TABLE IF NOT EXISTS amount_change (
  id INTEGER PRIMARY KEY,
  contract_id INTEGER NOT NULL REFERENCES contract(id),
  fee_item_id INTEGER REFERENCES fee_item(id),
  change_kind TEXT NOT NULL CHECK (change_kind IN (
    'replace',   -- 替换原合同总额
    'increase',  -- 增加金额
    'decrease',  -- 减少金额
    'terms_only' -- 只修改其他条款
  )),
  amount_before_minor INTEGER,
  amount_after_minor INTEGER,
  currency TEXT NOT NULL DEFAULT 'CNY',
  effective_date TEXT,
  basis_file_id INTEGER REFERENCES cfile(id),
  reason TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS amount_change_contract_idx ON amount_change(contract_id, effective_date);

-- ============ 收付款 ============
CREATE TABLE IF NOT EXISTS ledger_entry (
  id INTEGER PRIMARY KEY,
  direction TEXT NOT NULL CHECK (direction IN ('inflow', 'outflow')),
  entry_kind TEXT NOT NULL DEFAULT 'normal' CHECK (entry_kind IN (
    'normal', 'refund', 'reversal', 'correction'
  )),
  occurred_on TEXT NOT NULL,
  payer_company_id INTEGER REFERENCES comp(id),
  payee_company_id INTEGER REFERENCES comp(id),
  currency TEXT NOT NULL DEFAULT 'CNY',
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  bank_reference TEXT,
  voucher_file_id INTEGER REFERENCES cfile(id),
  handler_user_id INTEGER REFERENCES users(id),
  reconcile_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (reconcile_status IN ('pending', 'reconciled', 'unmatched')),
  original_entry_id INTEGER REFERENCES ledger_entry(id),  -- 退款 / 冲销 / 更正关联的原记录
  note TEXT,
  legacy_record_id INTEGER,
  voided_at TEXT,
  void_reason TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_entry_date_idx ON ledger_entry(occurred_on, direction, currency);
CREATE INDEX IF NOT EXISTS ledger_entry_ref_idx ON ledger_entry(bank_reference);

-- 流水分摊到合同：一笔流水可分配给多份合同，合计不得超过流水金额。
CREATE TABLE IF NOT EXISTS ledger_alloc (
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES ledger_entry(id),
  contract_id INTEGER NOT NULL REFERENCES contract(id),
  fee_item_id INTEGER REFERENCES fee_item(id),
  milestone_id INTEGER,
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  note TEXT,
  created_at TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS ledger_alloc_entry_idx ON ledger_alloc(entry_id);
CREATE INDEX IF NOT EXISTS ledger_alloc_contract_idx ON ledger_alloc(contract_id);

-- ============ 发票 ============
CREATE TABLE IF NOT EXISTS invoice (
  id INTEGER PRIMARY KEY,
  direction TEXT NOT NULL CHECK (direction IN ('output', 'input')),  -- 销项 / 进项
  invoice_number TEXT NOT NULL,
  issued_on TEXT NOT NULL,
  seller_company_id INTEGER REFERENCES comp(id),
  buyer_company_id INTEGER REFERENCES comp(id),
  currency TEXT NOT NULL DEFAULT 'CNY',
  total_minor INTEGER NOT NULL,
  net_minor INTEGER,
  tax_minor INTEGER,
  tax_rate TEXT,
  status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'voided', 'red')),
  original_invoice_id INTEGER REFERENCES invoice(id),  -- 红冲 / 更正关联
  contract_id INTEGER REFERENCES contract(id),
  fee_item_id INTEGER REFERENCES fee_item(id),
  attachment_file_id INTEGER REFERENCES cfile(id),
  note TEXT,
  legacy_record_id INTEGER,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS invoice_number_idx ON invoice(invoice_number, direction);
CREATE INDEX IF NOT EXISTS invoice_contract_idx ON invoice(contract_id, status);

-- ============ 核对与结算 ============
CREATE TABLE IF NOT EXISTS ledger_checkpoint (
  id INTEGER PRIMARY KEY,
  scope_kind TEXT NOT NULL DEFAULT 'global' CHECK (scope_kind IN ('global', 'company', 'contract')),
  scope_id INTEGER,
  currency TEXT,
  reconciled_through TEXT NOT NULL,
  completeness TEXT NOT NULL DEFAULT 'partial'
    CHECK (completeness IN ('partial', 'complete')),
  note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_checkpoint_scope_idx ON ledger_checkpoint(scope_kind, scope_id);

CREATE TABLE IF NOT EXISTS settlement (
  id INTEGER PRIMARY KEY,
  contract_id INTEGER NOT NULL REFERENCES contract(id),
  fee_item_id INTEGER REFERENCES fee_item(id),
  name TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  due_date TEXT,
  condition_met_at TEXT,
  milestone_id INTEGER,
  note TEXT,
  created_at TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS settlement_contract_idx ON settlement(contract_id, due_date);

CREATE TABLE IF NOT EXISTS billing_plan (
  id INTEGER PRIMARY KEY,
  contract_id INTEGER NOT NULL REFERENCES contract(id),
  fee_item_id INTEGER REFERENCES fee_item(id),
  name TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  due_date TEXT,
  condition_met_at TEXT,
  milestone_id INTEGER,
  note TEXT,
  created_at TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS billing_plan_contract_idx ON billing_plan(contract_id, due_date);

-- ============ 履约节点 ============
CREATE TABLE IF NOT EXISTS milestone (
  id INTEGER PRIMARY KEY,
  contract_id INTEGER NOT NULL REFERENCES contract(id),
  name TEXT NOT NULL,
  owner_user_id INTEGER REFERENCES users(id),
  planned_date TEXT,
  actual_date TEXT,
  trigger_condition TEXT,
  date_source TEXT NOT NULL DEFAULT 'manual' CHECK (date_source IN ('manual', 'computed')),
  formula TEXT,                     -- 例如「验收日期后30天」
  formula_basis_milestone_id INTEGER REFERENCES milestone(id),
  amount_minor INTEGER,
  currency TEXT,
  basis_file_id INTEGER REFERENCES cfile(id),
  proof_file_id INTEGER REFERENCES cfile(id),
  completed_at TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS milestone_contract_idx ON milestone(contract_id, planned_date);

CREATE TABLE IF NOT EXISTS task (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN (
    'company_missing',   -- 待补公司
    'amount_review',     -- 待核金额
    'ocr_failed',        -- OCR 失败
    'duplicate_suspect', -- 疑似重复
    'finance_confirm',   -- 待财务确认
    'milestone_due'      -- 节点即将到期
  )),
  contract_id INTEGER REFERENCES contract(id),
  file_id INTEGER REFERENCES cfile(id),
  company_id INTEGER REFERENCES comp(id),
  title TEXT NOT NULL,
  detail TEXT,
  due_date TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'snoozed')),
  snooze_until TEXT,
  snooze_reason TEXT,
  assignee_user_id INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_status_idx ON task(status, kind, due_date);

-- ============ 权限与复核 ============
CREATE TABLE IF NOT EXISTS permission (
  name TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  description TEXT
);

CREATE TABLE IF NOT EXISTS role (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  builtin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_permission (
  role_id INTEGER NOT NULL REFERENCES role(id),
  permission TEXT NOT NULL REFERENCES permission(name),
  PRIMARY KEY (role_id, permission)
);

CREATE TABLE IF NOT EXISTS user_role (
  user_id INTEGER NOT NULL REFERENCES users(id),
  role_id INTEGER NOT NULL REFERENCES role(id),
  granted_by INTEGER REFERENCES users(id),
  granted_at TEXT NOT NULL,
  PRIMARY KEY (user_id, role_id)
);

CREATE TABLE IF NOT EXISTS review_record (
  id INTEGER PRIMARY KEY,
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('contract', 'fee_item', 'ledger_entry')),
  subject_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  submitted_by INTEGER REFERENCES users(id),
  submitted_at TEXT,
  reviewer_id INTEGER REFERENCES users(id),
  outcome TEXT,
  comment TEXT,
  data_version TEXT,        -- 复核当时的 contract.revision / fee_item 内容摘要
  decided_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_record_subject_idx ON review_record(subject_kind, subject_id);

-- 关键字段修改前后值。普通用户不可改写自己的历史记录。
CREATE TABLE IF NOT EXISTS change_log (
  id INTEGER PRIMARY KEY,
  entity_kind TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  field_name TEXT NOT NULL,
  value_before TEXT,
  value_after TEXT,
  reason TEXT,
  actor_user_id INTEGER REFERENCES users(id),
  actor_name TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS change_log_entity_idx ON change_log(entity_kind, entity_id, created_at);

-- ============ 迁移台账 ============
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_steps (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS migration_steps_run_idx ON migration_steps(run_id);

-- 旧记录 → 新记录映射，保证迁移可重复执行且不重复插入
CREATE TABLE IF NOT EXISTS contract_legacy_map (
  legacy_document_id TEXT PRIMARY KEY,
  contract_id INTEGER NOT NULL REFERENCES contract(id),
  file_id INTEGER REFERENCES cfile(id),
  migrated_at TEXT NOT NULL,
  run_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contract_legacy_map_contract_idx ON contract_legacy_map(contract_id);

CREATE TABLE IF NOT EXISTS finance_legacy_map (
  legacy_record_id INTEGER PRIMARY KEY,
  entry_id INTEGER,
  invoice_id INTEGER,
  migrated_at TEXT NOT NULL,
  run_id TEXT NOT NULL
);

-- 旧 companies.id → 新 comp.id。按旧主键建立对应，绝不按名称匹配或自动合并。
CREATE TABLE IF NOT EXISTS company_legacy_map (
  legacy_company_id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES comp(id),
  migrated_at TEXT NOT NULL,
  run_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS company_legacy_map_company_idx ON company_legacy_map(company_id);

-- 一次性步骤的完成标记，保证重跑不会重复插入
CREATE TABLE IF NOT EXISTS migration_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 迁移过程中发现、需要人工决定的事项
CREATE TABLE IF NOT EXISTS migration_issue (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('info', 'warn', 'block')),
  code TEXT NOT NULL,
  subject TEXT,
  detail TEXT NOT NULL,
  resolved_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS migration_issue_run_idx ON migration_issue(run_id, severity);
"""


# 权限清单：与后端校验一一对应
PERMISSIONS: list[tuple[str, str, str]] = [
    ("contract.view", "查看合同", "查看合同列表、详情与原件预览"),
    ("contract.edit", "编辑元数据", "编辑合同标题、编号、日期、分类、项目"),
    ("contract.review", "核对合同", "确认归档信息核对状态"),
    ("amount.review", "核对金额", "确认费用明细的金额与税口径"),
    ("ledger.add", "登记流水", "登记收付款记录"),
    ("ledger.void", "作废流水", "作废或更正已登记的流水"),
    ("finance.confirm", "确认财务数据", "确认款项快照、发票与结算"),
    ("export.run", "导出", "导出 Excel、台账与原件打包"),
    ("archive.manage", "归档恢复", "归档与恢复合同和文件"),
    ("admin.users", "管理账号", "创建、停用账号并分配角色"),
    ("admin.backup", "备份恢复", "创建备份与执行恢复"),
]

# 内置角色 → 权限。小团队不强制复杂审批，角色可按需组合。
BUILTIN_ROLES: dict[str, tuple[str, list[str]]] = {
    "admin": ("管理员", [name for name, _, _ in PERMISSIONS]),
    "editor": ("业务录入", [
        "contract.view", "contract.edit", "ledger.add", "export.run",
    ]),
    "reviewer": ("合同复核", [
        "contract.view", "contract.edit", "contract.review", "amount.review", "export.run",
    ]),
    "finance": ("财务", [
        "contract.view", "amount.review", "ledger.add", "ledger.void",
        "finance.confirm", "export.run",
    ]),
    "viewer": ("只读", ["contract.view"]),
}


# 旧 admin/user 角色 → 新角色
LEGACY_ROLE_MAP = {"admin": "admin", "user": "editor"}
