-- =====================================================================
--  統合業務システム  SQLite スキーマ（C案：Windowsデスクトップアプリ用）
--  db_schema_version = 1   ／   対応するJSONデータ形式 data_schema_version = 2
--  SQLite 3.38 以上（生成列・JSON関数が標準で使える版）を想定。検証は SQLite 3.45.1 で実施
--
--  設計の方針
--   1. 取引・マスタ・作業人員は正規化し、外部キーで整合性を守る
--   2. 金額・数量は「入力どおりの文字列」で保存し、計算用の数値は生成列で持つ
--      （現行アプリの parseFloat と同じ解釈。ハッシュ・表示を現行と一致させるため）
--   3. 「発行済み請求書は変更・削除できない」「締め済みの月は変更できない」
--      「監査ログは追記専用」をトリガーでDB自体に強制する（アプリの不具合でも破れない）
--   4. 値が無いこと（キーが無いこと）はNULLで表す（JSONとの往復で同一になる）
--   5. 将来の項目追加に備え、主要テーブルに extra(JSON) を持つ
-- =====================================================================
PRAGMA foreign_keys = ON;
PRAGMA application_id = 1229212755;   -- 'IBIS'：このアプリのDBであることの目印
PRAGMA user_version  = 1;             -- db_schema_version（DBの構造の版）

-- ---------------------------------------------------------------------
-- 1. メタ情報・設定
-- ---------------------------------------------------------------------
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE migration_history (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  at           TEXT    NOT NULL,
  from_version INTEGER NOT NULL,
  to_version   INTEGER NOT NULL,
  steps_json   TEXT    NOT NULL CHECK (json_valid(steps_json)),
  warnings     INTEGER NOT NULL DEFAULT 0
);

-- 自社情報（1行のみ）
CREATE TABLE company (
  id     INTEGER PRIMARY KEY CHECK (id = 1),
  name   TEXT, rep TEXT, zip TEXT, addr TEXT, tel TEXT, fax TEXT,
  reg_no TEXT,                          -- インボイス登録番号（JSONの reg）
  extra  TEXT CHECK (extra IS NULL OR json_valid(extra))
);

-- 上記以外の設定（簡易課税・ラベル配置・バックアップ設定・事務処理規程・保存済み検索条件 等）
CREATE TABLE app_settings (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL CHECK (json_valid(value_json))
) WITHOUT ROWID;

CREATE TABLE tax_rates (
  id             TEXT PRIMARY KEY,
  sort_order     INTEGER NOT NULL,
  label          TEXT,
  rate           REAL NOT NULL CHECK (rate BETWEEN 0 AND 100),
  effective_from TEXT NOT NULL,
  extra          TEXT CHECK (extra IS NULL OR json_valid(extra))
);

-- 免税事業者等からの仕入れの控除割合（インボイス経過措置）
CREATE TABLE transitional_rates (
  sort_order     INTEGER PRIMARY KEY,
  effective_from TEXT NOT NULL,
  rate           REAL NOT NULL CHECK (rate BETWEEN 0 AND 100)
);

-- ---------------------------------------------------------------------
-- 2. マスタ
-- ---------------------------------------------------------------------
CREATE TABLE clients (
  id                     TEXT PRIMARY KEY,
  sort_order             INTEGER NOT NULL,
  name                   TEXT NOT NULL,
  closing_day            INTEGER, payment_offset_months INTEGER, payment_day INTEGER,
  credit_limit           REAL,
  zip TEXT, addr1 TEXT, addr2 TEXT, contact_name TEXT, honorific TEXT,
  tel TEXT, fax TEXT, email TEXT, registration_no TEXT,
  archived               INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1)),  -- 使用済みの取引先は削除せず非表示にする
  extra                  TEXT CHECK (extra IS NULL OR json_valid(extra))
);

CREATE TABLE sites         (sort_order INTEGER PRIMARY KEY, name TEXT NOT NULL);   -- 摘要テンプレート（工事内容・作業区分）
CREATE TABLE expense_items (sort_order INTEGER PRIMARY KEY, name TEXT NOT NULL);   -- 摘要テンプレート（経費項目）
CREATE TABLE site_budgets  (site_name TEXT PRIMARY KEY, planned_days REAL NOT NULL) WITHOUT ROWID;

CREATE TABLE invoice_seq (                 -- 伝票No.の月別連番（KDyyyymm-NNN）
  month    TEXT PRIMARY KEY CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  last_seq INTEGER NOT NULL CHECK (last_seq >= 0)
) WITHOUT ROWID;

-- 取引先ごとの請求書ひな形
CREATE TABLE invoice_templates (
  client_id TEXT PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE invoice_template_items (
  client_id TEXT NOT NULL REFERENCES invoice_templates(client_id) ON DELETE CASCADE,
  line_no INTEGER NOT NULL, site TEXT, content TEXT, qty TEXT, unit TEXT, price TEXT,
  PRIMARY KEY (client_id, line_no)
) WITHOUT ROWID;
CREATE TABLE invoice_template_expenses (
  client_id TEXT NOT NULL REFERENCES invoice_templates(client_id) ON DELETE CASCADE,
  line_no INTEGER NOT NULL, name TEXT, amount TEXT,
  PRIMARY KEY (client_id, line_no)
) WITHOUT ROWID;

-- ---------------------------------------------------------------------
-- 3. 請求書
-- ---------------------------------------------------------------------
CREATE TABLE invoices (
  id             TEXT PRIMARY KEY,
  month          TEXT NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  seq            INTEGER NOT NULL,                     -- 月内の並び順
  no             TEXT NOT NULL DEFAULT '',             -- 伝票No.（空は下書きの未設定のみ）
  client_id      TEXT REFERENCES clients(id) ON DELETE RESTRICT,
  issue_date     TEXT,
  tax_rate_id    TEXT REFERENCES tax_rates(id) ON DELETE RESTRICT,
  status         TEXT NOT NULL DEFAULT '未収'
                 CHECK (status IN ('未収','請求済み','一部回収','回収済み','取消')),
  issued_at      TEXT,                                 -- 発行（確定）日時。NULL＝下書き
  issue_hash     TEXT,                                 -- 発行時の内容ハッシュ（SHA-256）
  voided_at      TEXT, void_reason TEXT, void_credit_no TEXT,   -- 取消された請求書
  credit_of_id   TEXT REFERENCES invoices(id) ON DELETE RESTRICT, -- 赤伝：取消対象
  credit_of_no   TEXT, credit_of_date TEXT, credit_reason TEXT,
  extra          TEXT CHECK (extra IS NULL OR json_valid(extra)),
  UNIQUE (month, seq),
  CHECK (issued_at IS NOT NULL OR issue_hash IS NULL),
  CHECK (voided_at IS NULL OR status = '取消'),
  CHECK (credit_of_id IS NULL OR status = '取消')
);
CREATE UNIQUE INDEX ux_invoices_no       ON invoices(no) WHERE no <> '';
CREATE INDEX        ix_invoices_client   ON invoices(client_id);
CREATE INDEX        ix_invoices_status   ON invoices(status);
CREATE INDEX        ix_invoices_date     ON invoices(issue_date);
CREATE INDEX        ix_invoices_credit   ON invoices(credit_of_id);

CREATE TABLE invoice_items (
  invoice_id TEXT    NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  line_no    INTEGER NOT NULL,
  site TEXT, content TEXT, qty TEXT, unit TEXT, price TEXT,
  qty_num   REAL GENERATED ALWAYS AS (COALESCE(CAST(qty   AS REAL), 0)) VIRTUAL,   -- parseFloat(x)||0 と同じ解釈
  price_num REAL GENERATED ALWAYS AS (COALESCE(CAST(price AS REAL), 0)) VIRTUAL,
  PRIMARY KEY (invoice_id, line_no)
) WITHOUT ROWID;

CREATE TABLE invoice_expenses (
  invoice_id TEXT    NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  line_no    INTEGER NOT NULL,
  name TEXT, amount TEXT,
  amount_num REAL GENERATED ALWAYS AS (COALESCE(CAST(amount AS REAL), 0)) VIRTUAL,
  PRIMARY KEY (invoice_id, line_no)
) WITHOUT ROWID;

CREATE TABLE payments (
  invoice_id TEXT    NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  line_no    INTEGER NOT NULL,
  pay_date TEXT, amount TEXT,
  amount_num REAL GENERATED ALWAYS AS (COALESCE(CAST(amount AS REAL), 0)) VIRTUAL,
  PRIMARY KEY (invoice_id, line_no)
) WITHOUT ROWID;
CREATE INDEX ix_payments_date ON payments(pay_date);

-- ---------------------------------------------------------------------
-- 4. 作業人員集計表
-- ---------------------------------------------------------------------
CREATE TABLE man_hour_months (
  month    TEXT PRIMARY KEY CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  carry    REAL NOT NULL DEFAULT 0,       -- 前月までの人員累計
  ot_carry REAL,                          -- 前月までの残業時間累計
  extra    TEXT CHECK (extra IS NULL OR json_valid(extra))
) WITHOUT ROWID;

CREATE TABLE man_hour_rows (
  id      INTEGER PRIMARY KEY,
  month   TEXT    NOT NULL REFERENCES man_hour_months(month) ON DELETE CASCADE,
  line_no INTEGER NOT NULL,
  content TEXT    NOT NULL DEFAULT '',
  kind    TEXT CHECK (kind IS NULL OR kind IN ('people','overtime')),   -- NULL＝行名に「残業」を含めば残業
  extra   TEXT CHECK (extra IS NULL OR json_valid(extra)),
  UNIQUE (month, line_no)
);

CREATE TABLE man_hour_cells (
  row_id    INTEGER NOT NULL REFERENCES man_hour_rows(id) ON DELETE CASCADE,
  seq       INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 62),   -- 集計期間内の日付の並び（21日→翌20日で1〜31）
  value     TEXT    NOT NULL,                                -- 入力どおり（「2」「1.5H」「メモ」等）
  num_value REAL    NOT NULL DEFAULT 0,                      -- 集計用の数値（アプリが行の種別に応じて算出して保存）
  PRIMARY KEY (row_id, seq)
) WITHOUT ROWID;

CREATE TABLE man_hour_expenses (
  month   TEXT    NOT NULL REFERENCES man_hour_months(month) ON DELETE CASCADE,
  line_no INTEGER NOT NULL,
  name TEXT, amount TEXT,
  PRIMARY KEY (month, line_no)
) WITHOUT ROWID;

-- ---------------------------------------------------------------------
-- 5. その他の取引データ
-- ---------------------------------------------------------------------
CREATE TABLE site_costs (                  -- 工事台帳の原価
  id         TEXT PRIMARY KEY,
  site_name  TEXT NOT NULL,
  sort_order INTEGER NOT NULL,
  cost_date  TEXT, category TEXT, amount TEXT, memo TEXT,
  amount_num REAL GENERATED ALWAYS AS (COALESCE(CAST(amount AS REAL), 0)) VIRTUAL
);
CREATE INDEX ix_site_costs_site ON site_costs(site_name);

CREATE TABLE labels (                      -- 宛名ラベル
  id TEXT PRIMARY KEY, sort_order INTEGER NOT NULL,
  zip TEXT, addr1 TEXT, addr2 TEXT, name TEXT, honorific TEXT,
  extra TEXT CHECK (extra IS NULL OR json_valid(extra))
);

CREATE TABLE closed_months (               -- 月次締め
  kind      TEXT NOT NULL CHECK (kind IN ('manHours','invoices')),
  month     TEXT NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  closed_at TEXT,
  PRIMARY KEY (kind, month)
) WITHOUT ROWID;

-- ---------------------------------------------------------------------
-- 6. 監査ログ（追記専用・ハッシュ連鎖）
-- ---------------------------------------------------------------------
CREATE TABLE audit_log (
  seq       INTEGER PRIMARY KEY CHECK (seq >= 1),
  time      TEXT NOT NULL,
  action    TEXT NOT NULL,
  prev_hash TEXT NOT NULL CHECK (length(prev_hash) = 64),
  hash      TEXT NOT NULL UNIQUE CHECK (length(hash) = 64)
);
CREATE INDEX ix_audit_time ON audit_log(time);

CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, '監査ログは変更できません（追記専用）'); END;
CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, '監査ログは削除できません（追記専用）'); END;
-- 連番と直前ハッシュの連続性をDBが検査する（ハッシュ値そのものの計算はアプリが行う）
CREATE TRIGGER trg_audit_chain BEFORE INSERT ON audit_log
WHEN NEW.seq <> COALESCE((SELECT MAX(seq) FROM audit_log), 0) + 1
  OR NEW.prev_hash <> COALESCE((SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1),
                               '0000000000000000000000000000000000000000000000000000000000000000')
BEGIN SELECT RAISE(ABORT, '監査ログの連番または直前ハッシュが連続していません'); END;

-- ---------------------------------------------------------------------
-- 7. 改変防止トリガー（発行済み請求書・締め済みの月）
-- ---------------------------------------------------------------------
-- 7-1. 請求書本体
CREATE TRIGGER trg_inv_no_delete_issued BEFORE DELETE ON invoices
WHEN OLD.issued_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, '発行済みの請求書は削除できません（取消（赤伝）を使用してください）'); END;

CREATE TRIGGER trg_inv_no_delete_closed BEFORE DELETE ON invoices
WHEN EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = OLD.month)
BEGIN SELECT RAISE(ABORT, '締め済みの月の請求書は削除できません'); END;

CREATE TRIGGER trg_inv_no_insert_closed BEFORE INSERT ON invoices
WHEN EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = NEW.month)
BEGIN SELECT RAISE(ABORT, '締め済みの月に請求書は追加できません'); END;

-- 発行済み：内容項目は変更不可（状態・入金・取消印の付与は可）。取消印は一度付けたら変更不可
CREATE TRIGGER trg_inv_immutable BEFORE UPDATE ON invoices
WHEN OLD.issued_at IS NOT NULL AND (
     NEW.id IS NOT OLD.id OR NEW.month IS NOT OLD.month OR NEW.seq IS NOT OLD.seq OR NEW.no IS NOT OLD.no
  OR NEW.client_id IS NOT OLD.client_id OR NEW.issue_date IS NOT OLD.issue_date OR NEW.tax_rate_id IS NOT OLD.tax_rate_id
  OR NEW.issued_at IS NOT OLD.issued_at OR NEW.issue_hash IS NOT OLD.issue_hash
  OR NEW.credit_of_id IS NOT OLD.credit_of_id OR NEW.credit_of_no IS NOT OLD.credit_of_no
  OR NEW.credit_of_date IS NOT OLD.credit_of_date OR NEW.credit_reason IS NOT OLD.credit_reason
  OR NEW.extra IS NOT OLD.extra
  OR (OLD.voided_at IS NOT NULL AND (NEW.voided_at IS NOT OLD.voided_at OR NEW.void_reason IS NOT OLD.void_reason OR NEW.void_credit_no IS NOT OLD.void_credit_no)))
BEGIN SELECT RAISE(ABORT, '発行済みの請求書の内容は変更できません'); END;

-- 締め済みの月：内容項目は変更不可（取消印の付与＝statusとvoided_*の変更は可）
CREATE TRIGGER trg_inv_closed_update BEFORE UPDATE ON invoices
WHEN EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = OLD.month) AND (
     NEW.month IS NOT OLD.month OR NEW.seq IS NOT OLD.seq OR NEW.no IS NOT OLD.no OR NEW.client_id IS NOT OLD.client_id
  OR NEW.issue_date IS NOT OLD.issue_date OR NEW.tax_rate_id IS NOT OLD.tax_rate_id OR NEW.issued_at IS NOT OLD.issued_at
  OR NEW.issue_hash IS NOT OLD.issue_hash OR NEW.credit_of_id IS NOT OLD.credit_of_id OR NEW.extra IS NOT OLD.extra)
BEGIN SELECT RAISE(ABORT, '締め済みの月の請求書は変更できません'); END;

-- 7-2. 明細・経費：発行済み、または締め済みの月は変更不可
CREATE TRIGGER trg_item_ins BEFORE INSERT ON invoice_items
WHEN EXISTS (SELECT 1 FROM invoices i WHERE i.id = NEW.invoice_id AND (i.issued_at IS NOT NULL
        OR EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = i.month)))
BEGIN SELECT RAISE(ABORT, '発行済み・締め済みの請求書の明細は変更できません'); END;
CREATE TRIGGER trg_item_upd BEFORE UPDATE ON invoice_items
WHEN EXISTS (SELECT 1 FROM invoices i WHERE i.id = OLD.invoice_id AND (i.issued_at IS NOT NULL
        OR EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = i.month)))
BEGIN SELECT RAISE(ABORT, '発行済み・締め済みの請求書の明細は変更できません'); END;
CREATE TRIGGER trg_item_del BEFORE DELETE ON invoice_items
WHEN EXISTS (SELECT 1 FROM invoices i WHERE i.id = OLD.invoice_id AND (i.issued_at IS NOT NULL
        OR EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = i.month)))
BEGIN SELECT RAISE(ABORT, '発行済み・締め済みの請求書の明細は変更できません'); END;

CREATE TRIGGER trg_exp_ins BEFORE INSERT ON invoice_expenses
WHEN EXISTS (SELECT 1 FROM invoices i WHERE i.id = NEW.invoice_id AND (i.issued_at IS NOT NULL
        OR EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = i.month)))
BEGIN SELECT RAISE(ABORT, '発行済み・締め済みの請求書の経費は変更できません'); END;
CREATE TRIGGER trg_exp_upd BEFORE UPDATE ON invoice_expenses
WHEN EXISTS (SELECT 1 FROM invoices i WHERE i.id = OLD.invoice_id AND (i.issued_at IS NOT NULL
        OR EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = i.month)))
BEGIN SELECT RAISE(ABORT, '発行済み・締め済みの請求書の経費は変更できません'); END;
CREATE TRIGGER trg_exp_del BEFORE DELETE ON invoice_expenses
WHEN EXISTS (SELECT 1 FROM invoices i WHERE i.id = OLD.invoice_id AND (i.issued_at IS NOT NULL
        OR EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = i.month)))
BEGIN SELECT RAISE(ABORT, '発行済み・締め済みの請求書の経費は変更できません'); END;

-- 7-3. 入金：取消済み・赤伝、または締め済みの月は変更不可（発行済みでも入金は記録できる）
CREATE TRIGGER trg_pay_ins BEFORE INSERT ON payments
WHEN EXISTS (SELECT 1 FROM invoices i WHERE i.id = NEW.invoice_id AND (i.voided_at IS NOT NULL OR i.credit_of_id IS NOT NULL
        OR EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = i.month)))
BEGIN SELECT RAISE(ABORT, '取消済み・赤伝・締め済みの請求書の入金は変更できません'); END;
CREATE TRIGGER trg_pay_upd BEFORE UPDATE ON payments
WHEN EXISTS (SELECT 1 FROM invoices i WHERE i.id = OLD.invoice_id AND (i.voided_at IS NOT NULL OR i.credit_of_id IS NOT NULL
        OR EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = i.month)))
BEGIN SELECT RAISE(ABORT, '取消済み・赤伝・締め済みの請求書の入金は変更できません'); END;
CREATE TRIGGER trg_pay_del BEFORE DELETE ON payments
WHEN EXISTS (SELECT 1 FROM invoices i WHERE i.id = OLD.invoice_id AND (i.voided_at IS NOT NULL OR i.credit_of_id IS NOT NULL
        OR EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='invoices' AND c.month = i.month)))
BEGIN SELECT RAISE(ABORT, '取消済み・赤伝・締め済みの請求書の入金は変更できません'); END;

-- 7-4. 作業人員集計表：締め済みの月は変更不可
CREATE TRIGGER trg_mhm_upd BEFORE UPDATE ON man_hour_months
WHEN EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='manHours' AND c.month = OLD.month)
BEGIN SELECT RAISE(ABORT, '締め済みの月の作業人員は変更できません'); END;

CREATE TRIGGER trg_mhr_ins BEFORE INSERT ON man_hour_rows
WHEN EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='manHours' AND c.month = NEW.month)
BEGIN SELECT RAISE(ABORT, '締め済みの月の作業人員は変更できません'); END;
CREATE TRIGGER trg_mhr_upd BEFORE UPDATE ON man_hour_rows
WHEN EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='manHours' AND c.month = OLD.month)
BEGIN SELECT RAISE(ABORT, '締め済みの月の作業人員は変更できません'); END;
CREATE TRIGGER trg_mhr_del BEFORE DELETE ON man_hour_rows
WHEN EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='manHours' AND c.month = OLD.month)
BEGIN SELECT RAISE(ABORT, '締め済みの月の作業人員は変更できません'); END;

CREATE TRIGGER trg_mhc_ins BEFORE INSERT ON man_hour_cells
WHEN EXISTS (SELECT 1 FROM man_hour_rows r JOIN closed_months c ON c.kind='manHours' AND c.month = r.month WHERE r.id = NEW.row_id)
BEGIN SELECT RAISE(ABORT, '締め済みの月の作業人員は変更できません'); END;
CREATE TRIGGER trg_mhc_upd BEFORE UPDATE ON man_hour_cells
WHEN EXISTS (SELECT 1 FROM man_hour_rows r JOIN closed_months c ON c.kind='manHours' AND c.month = r.month WHERE r.id = OLD.row_id)
BEGIN SELECT RAISE(ABORT, '締め済みの月の作業人員は変更できません'); END;
CREATE TRIGGER trg_mhc_del BEFORE DELETE ON man_hour_cells
WHEN EXISTS (SELECT 1 FROM man_hour_rows r JOIN closed_months c ON c.kind='manHours' AND c.month = r.month WHERE r.id = OLD.row_id)
BEGIN SELECT RAISE(ABORT, '締め済みの月の作業人員は変更できません'); END;

CREATE TRIGGER trg_mhe_ins BEFORE INSERT ON man_hour_expenses
WHEN EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='manHours' AND c.month = NEW.month)
BEGIN SELECT RAISE(ABORT, '締め済みの月の諸経費は変更できません'); END;
CREATE TRIGGER trg_mhe_upd BEFORE UPDATE ON man_hour_expenses
WHEN EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='manHours' AND c.month = OLD.month)
BEGIN SELECT RAISE(ABORT, '締め済みの月の諸経費は変更できません'); END;
CREATE TRIGGER trg_mhe_del BEFORE DELETE ON man_hour_expenses
WHEN EXISTS (SELECT 1 FROM closed_months c WHERE c.kind='manHours' AND c.month = OLD.month)
BEGIN SELECT RAISE(ABORT, '締め済みの月の諸経費は変更できません'); END;

-- ---------------------------------------------------------------------
-- 8. 集計ビュー（現行アプリの計算ロジックと同じ結果になることをテストで確認済み）
-- ---------------------------------------------------------------------
-- 請求書ごとの金額。消費税は「符号を分けて」四捨五入（赤伝が元の請求書と1円もずれず打ち消し合う）
-- 税率：税率IDが有効ならそれ、なければ発行日時点の税率（発行日が空なら最新）、なければ最も古い税率
CREATE VIEW v_invoice_totals AS
WITH base AS (
  SELECT i.id, i.month, i.no, i.status, i.client_id, i.issue_date, i.issued_at,
         COALESCE((SELECT SUM(it.qty_num * it.price_num) FROM invoice_items it WHERE it.invoice_id = i.id), 0)
       + COALESCE((SELECT SUM(e.amount_num) FROM invoice_expenses e WHERE e.invoice_id = i.id), 0) AS subtotal,
         COALESCE(
           (SELECT t.rate FROM tax_rates t WHERE t.id = i.tax_rate_id),
           (SELECT t.rate FROM tax_rates t WHERE t.effective_from <= COALESCE(NULLIF(i.issue_date, ''), '9999-12-31') ORDER BY t.effective_from DESC LIMIT 1),
           (SELECT t.rate FROM tax_rates t ORDER BY t.effective_from ASC LIMIT 1),
           10) AS rate,
         COALESCE((SELECT SUM(p.amount_num) FROM payments p WHERE p.invoice_id = i.id), 0) AS paid
  FROM invoices i
), calc AS (
  SELECT *, (CASE WHEN subtotal < 0 THEN -1 ELSE 1 END) * ROUND(ABS(subtotal) * (rate / 100.0)) AS tax FROM base
)
SELECT id, month, no, status, client_id, issue_date, issued_at, subtotal, rate, tax,
       subtotal + tax AS grand, paid,
       MAX(0, subtotal + tax - paid) AS remaining
FROM calc;

-- 取引先ごとの未収残高（回収済み・取消を除く。与信管理用）
CREATE VIEW v_client_balance AS
SELECT c.id AS client_id, c.name, c.credit_limit,
       COALESCE(SUM(CASE WHEN t.status NOT IN ('回収済み','取消') THEN t.remaining END), 0) AS outstanding
FROM clients c LEFT JOIN v_invoice_totals t ON t.client_id = c.id
GROUP BY c.id;

-- 現場別の売上（請求書明細。赤伝のマイナスで自動的に打ち消される）
CREATE VIEW v_site_revenue AS
SELECT it.site AS site_name, ROUND(SUM(it.qty_num * it.price_num), 2) AS revenue
FROM invoice_items it WHERE COALESCE(it.site, '') <> '' GROUP BY it.site;

-- 作業人員：行ごとの種別（残業か）と合計
CREATE VIEW v_man_hour_rows AS
SELECT r.id AS row_id, r.month, r.line_no, r.content,
       CASE WHEN r.kind = 'overtime' THEN 1 WHEN r.kind = 'people' THEN 0
            WHEN r.content LIKE '%残業%' THEN 1 ELSE 0 END AS is_overtime,
       COALESCE((SELECT SUM(c.num_value) FROM man_hour_cells c WHERE c.row_id = r.id), 0) AS row_total
FROM man_hour_rows r;

-- 作業人員：月ごとの人員・残業時間・累計（残業は人員に含めない）
CREATE VIEW v_man_hour_month AS
SELECT m.month, m.carry, m.ot_carry,
       ROUND(COALESCE((SELECT SUM(v.row_total) FROM v_man_hour_rows v WHERE v.month = m.month AND v.is_overtime = 0), 0), 2) AS people,
       ROUND(COALESCE((SELECT SUM(v.row_total) FROM v_man_hour_rows v WHERE v.month = m.month AND v.is_overtime = 1), 0), 2) AS overtime
FROM man_hour_months m;
