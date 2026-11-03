#!/usr/bin/env python3
"""DB設計の総合テスト。  使い方:  python tests/run_tests.py
フィクスチャは gen_fixtures.js が現行アプリを実際に動かして作ったもの（アプリの計算結果を「正解」として使う）。"""
import copy, hashlib, json, os, shutil, sqlite3, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import migrate_json_to_sqlite as M  # noqa: E402

FX = os.path.join(HERE, "fixtures")
TMP = tempfile.mkdtemp(prefix="ibis_db_test_")


def dump_json(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def fx(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return json.load(f)


def build_db(name, **kw):
    out = os.path.join(TMP, name.replace(".json", "") + f"_{len(os.listdir(TMP))}.db")
    M.migrate(os.path.join(FX, name), out, **kw)
    return out


def conn_of(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def expect_abort(tc, conn, sql, params=(), msg=None):
    with tc.assertRaises(sqlite3.IntegrityError if msg is None else sqlite3.DatabaseError) as cm:
        conn.execute(sql, params)
    if msg:
        tc.assertIn(msg, str(cm.exception))


class TestMigration(unittest.TestCase):
    def test_rich_roundtrip(self):
        db = build_db("rich_v2.json")
        data = fx("rich_v2.json")
        self.assertEqual(M.verify_migration(data, db), [])
        back = M.export_sqlite_to_json(db)
        self.assertEqual(M.normalize(data), M.normalize(back))

    def test_legacy_migrated_roundtrip(self):
        db = build_db("legacy_migrated_v2.json")
        self.assertEqual(M.verify_migration(fx("legacy_migrated_v2.json"), db), [])

    def test_dangling_references_are_preserved(self):
        db = build_db("dangling_v2.json")
        c = conn_of(db)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM invoices WHERE client_id IS NULL").fetchone()[0] > 0, True)
        orphan = c.execute("SELECT extra FROM invoices WHERE extra LIKE '%orphanClientId%' LIMIT 1").fetchone()
        self.assertIsNotNone(orphan)
        self.assertIn("_orphans", [r[0] for r in c.execute("SELECT key FROM app_settings")])
        back = M.export_sqlite_to_json(db)
        self.assertIn("deleted-client-xyz", back["settings"]["invoiceTemplates"])           # 持ち主のいないひな形も失わない
        self.assertEqual(M.verify_migration(fx("dangling_v2.json"), db), [])

    def test_idempotent_double_roundtrip(self):
        db1 = build_db("rich_v2.json")
        out = os.path.join(TMP, "again.json")
        dump_json(M.export_sqlite_to_json(db1), out)
        db2 = os.path.join(TMP, "again.db")
        M.migrate(out, db2, overwrite=True)
        self.assertEqual(M.normalize(M.export_sqlite_to_json(db1)), M.normalize(M.export_sqlite_to_json(db2)))

    def test_meta_records_source_and_audit_head(self):
        db = build_db("rich_v2.json")
        c = conn_of(db)
        meta = {r["key"]: r["value"] for r in c.execute("SELECT * FROM meta")}
        with open(os.path.join(FX, "rich_v2.json"), "rb") as f:
            raw = f.read()
        self.assertEqual(meta["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(meta["db_schema_version"], "1")
        log = fx("rich_v2.json")["auditLog"]
        self.assertEqual((meta["audit_head_seq"], meta["audit_head_hash"]), (str(log[-1]["seq"]), log[-1]["hash"]))
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(c.execute("PRAGMA application_id").fetchone()[0], 1229212755)

    def test_cli_migrate_verify_export(self):
        db = os.path.join(TMP, "cli.db")
        out = os.path.join(TMP, "cli.json")
        script = os.path.join(ROOT, "migrate_json_to_sqlite.py")
        r = subprocess.run([sys.executable, script, "migrate", os.path.join(FX, "rich_v2.json"), db, "--overwrite"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, script, "verify", os.path.join(FX, "rich_v2.json"), db], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "合格"))
        r = subprocess.run([sys.executable, script, "export", db, out], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(M.normalize(fx("rich_v2.json")), M.normalize(json.load(f)))


class TestRejection(unittest.TestCase):
    def mig(self, data, name="x.json"):
        p = os.path.join(TMP, name)
        dump_json(data, p)
        dst = os.path.join(TMP, name + ".db")
        with self.assertRaises(M.MigrationError) as cm:
            M.migrate(p, dst, overwrite=True)
        self.assertFalse(os.path.exists(dst), "失敗時にDBファイルを残してはならない")
        self.assertFalse(os.path.exists(dst + ".tmp"), "失敗時に一時ファイルを残してはならない")
        return str(cm.exception)

    def test_v1_is_rejected_with_guidance(self):
        self.assertIn("最新形式（v2）で書き出し直してください", self.mig(fx("legacy_v1.json")))

    def test_not_json(self):
        p = os.path.join(TMP, "bad.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("これはJSONではない")
        with self.assertRaises(M.MigrationError):
            M.migrate(p, os.path.join(TMP, "bad.db"))

    def test_envelope_checksum_mismatch(self):
        data = fx("rich_v2.json")
        env = {"format": "integratedBizSystem", "schemaVersion": 2, "checksum": M.sha256_hex(M.canonical_json(data)), "data": data}
        ok = os.path.join(TMP, "env_ok.json"); dump_json(env, ok)
        M.migrate(ok, os.path.join(TMP, "env_ok.db"), overwrite=True)                       # 正しいチェックサムなら通る
        env["data"]["settings"]["company"]["name"] = "書き換え"
        bad = os.path.join(TMP, "env_bad.json"); dump_json(env, bad)
        with self.assertRaises(M.MigrationError) as cm:
            M.migrate(bad, os.path.join(TMP, "env_bad.db"))
        self.assertIn("チェックサムが一致しません", str(cm.exception))

    def test_broken_audit_chain(self):
        d = fx("rich_v2.json"); d["auditLog"][2]["action"] += "改ざん"
        self.assertIn("連鎖が No.3 で壊れています", self.mig(d))

    def test_duplicate_invoice_no(self):
        d = fx("rich_v2.json"); ks = sorted(d["invoices"])
        d["invoices"][ks[1]][0]["no"] = d["invoices"][ks[0]][0]["no"]
        self.assertIn("伝票No.が重複しています", self.mig(d))

    def test_invalid_status_and_missing_credit_target(self):
        d = fx("rich_v2.json"); d["invoices"][sorted(d["invoices"])[0]][0]["status"] = "謎の状態"
        self.assertIn("状態が不正です", self.mig(d))
        d = fx("rich_v2.json")
        for lst in d["invoices"].values():
            for i in lst:
                if i.get("creditOf"):
                    i["creditOf"] = "no-such-id"
        self.assertIn("取消対象", self.mig(d))

    def test_existing_destination_needs_overwrite(self):
        db = build_db("rich_v2.json")
        with self.assertRaises(M.MigrationError):
            M.migrate(os.path.join(FX, "rich_v2.json"), db)

    def test_tampered_issued_invoice_is_reported_as_warning(self):
        d = fx("rich_v2.json"); k = sorted(d["invoices"])[0]
        d["invoices"][k][0]["items"][0]["price"] = "1"           # 発行後に内容が変わった状態
        errors, warnings = M.precheck(d)
        self.assertEqual(errors, [])
        self.assertTrue(any("発行時のハッシュと一致しません" in w for w in warnings))


class TestEquivalenceWithApp(unittest.TestCase):
    """現行アプリ（JavaScript）の出力を正解として、Python再現・SQLite の両方が一致するか"""

    def test_sha256_and_content_hash_vectors(self):
        hv = fx("hash_vectors.json")
        for v in hv["sha"]:
            self.assertEqual(M.sha256_hex(v["s"]), v["h"])
        for v in hv["invoices"]:
            self.assertEqual(M.invoice_content_hash(v["inv"]), v["hash"], v["inv"].get("no"))

    def test_cell_number_vectors(self):
        vec = fx("cell_vectors.json")
        self.assertGreater(len(vec), 200)
        for v in vec:
            value = None if v["value"] == "__undef__" else v["value"]
            self.assertEqual(M.is_overtime_row(v["kind"], v["content"]), v["overtime"], v)
            self.assertEqual(M.cell_number(v["kind"], v["content"], value), float(v["expect"]), v)

    def test_random_invoices_python_and_sql_match_app(self):
        rnd = fx("random_invoices.json")
        data = {"schemaVersion": 2, "settings": {"company": {}, "taxRates": rnd["taxRates"], "clients": [], "sites": [], "expenseItems": [],
                                                 "siteBudgets": {}, "invoiceTemplates": {}, "transitionalRates": []},
                "manHours": {}, "invoices": {"2026-11": [c["inv"] for c in rnd["cases"]]}, "labels": [], "siteCosts": {}, "closed": {"manHours": {}, "invoices": {}}, "auditLog": []}
        p = os.path.join(TMP, "rnd.json"); dump_json(data, p)
        db = os.path.join(TMP, "rnd.db"); M.migrate(p, db, overwrite=True)
        c = conn_of(db)
        totals = {r["id"]: r for r in c.execute("SELECT * FROM v_invoice_totals")}
        bad_py = bad_sql = 0
        for case in rnd["cases"]:
            inv, exp = case["inv"], case["expect"]
            py = M.invoice_financials(inv, rnd["taxRates"])
            sq = totals[inv["id"]]
            for k in ("subtotal", "grand", "paid", "remaining"):
                if abs(py[k] - exp[k]) > 1e-6: bad_py += 1
                if abs(sq[k] - exp[k]) > 1e-6: bad_sql += 1
            if py["tax"] != exp["tax"]: bad_py += 1
            if sq["tax"] != exp["tax"]: bad_sql += 1
        self.assertEqual((bad_py, bad_sql), (0, 0), f"アプリとの不一致 python={bad_py} sql={bad_sql}")
        self.assertEqual(len(totals), 2000)


class TestGuards(unittest.TestCase):
    """DB自体が「発行済みは変更不可」「締め済みは変更不可」「監査ログは追記専用」を強制するか"""

    @classmethod
    def setUpClass(cls):
        cls.base = build_db("rich_v2.json")

    def setUp(self):
        self.path = os.path.join(TMP, f"guard_{self._testMethodName}.db")
        shutil.copy(self.base, self.path)
        self.c = conn_of(self.path)
        self.issued = self.c.execute("SELECT * FROM invoices WHERE issued_at IS NOT NULL AND voided_at IS NULL AND credit_of_id IS NULL AND month NOT IN (SELECT month FROM closed_months WHERE kind='invoices') LIMIT 1").fetchone()
        self.draft = self.c.execute("SELECT * FROM invoices WHERE issued_at IS NULL LIMIT 1").fetchone()
        self.closed_issued = self.c.execute("SELECT * FROM invoices WHERE month IN (SELECT month FROM closed_months WHERE kind='invoices') LIMIT 1").fetchone()
        self.voided = self.c.execute("SELECT * FROM invoices WHERE voided_at IS NOT NULL").fetchone()
        self.credit = self.c.execute("SELECT * FROM invoices WHERE credit_of_id IS NOT NULL").fetchone()

    def test_audit_log_is_append_only(self):
        expect_abort(self, self.c, "UPDATE audit_log SET action='x' WHERE seq=1", msg="追記専用")
        expect_abort(self, self.c, "DELETE FROM audit_log WHERE seq=1", msg="追記専用")
        last = self.c.execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        expect_abort(self, self.c, "INSERT INTO audit_log VALUES(?,?,?,?,?)", (last["seq"] + 2, "t", "a", last["hash"], "f" * 64), msg="連続していません")
        expect_abort(self, self.c, "INSERT INTO audit_log VALUES(?,?,?,?,?)", (last["seq"] + 1, "t", "a", "0" * 64, "e" * 64), msg="連続していません")
        seq, t, a = last["seq"] + 1, "2026-11-03T00:00:00Z", "テスト追記"
        self.c.execute("INSERT INTO audit_log VALUES(?,?,?,?,?)", (seq, t, a, last["hash"], M.audit_hash(last["hash"], seq, t, a)))
        entries = [dict(seq=r["seq"], time=r["time"], action=r["action"], prev=r["prev_hash"], hash=r["hash"]) for r in self.c.execute("SELECT * FROM audit_log ORDER BY seq")]
        self.assertIsNone(M.verify_audit_chain(entries))

    def test_issued_invoice_cannot_be_deleted_or_edited(self):
        i = self.issued["id"]
        expect_abort(self, self.c, "DELETE FROM invoices WHERE id=?", (i,), "発行済みの請求書は削除できません")
        for col, val in (("no", "KD999"), ("client_id", None), ("issue_date", "2000-01-01"), ("tax_rate_id", None), ("issue_hash", "0" * 64), ("issued_at", None), ("month", "2026-12")):
            expect_abort(self, self.c, f"UPDATE invoices SET {col}=? WHERE id=?", (val, i), "発行済みの請求書の内容は変更できません")
        self.c.execute("UPDATE invoices SET status='請求済み' WHERE id=?", (i,))            # 状態の変更は可
        self.assertEqual(self.c.execute("SELECT status FROM invoices WHERE id=?", (i,)).fetchone()[0], "請求済み")

    def test_issued_invoice_lines_are_locked_but_payments_allowed(self):
        i = self.issued["id"]
        expect_abort(self, self.c, "INSERT INTO invoice_items(invoice_id,line_no,qty,price) VALUES(?,99,'1','1')", (i,), "明細は変更できません")
        expect_abort(self, self.c, "UPDATE invoice_items SET price='1' WHERE invoice_id=?", (i,), "明細は変更できません")
        expect_abort(self, self.c, "DELETE FROM invoice_items WHERE invoice_id=?", (i,), "明細は変更できません")
        expect_abort(self, self.c, "INSERT INTO invoice_expenses(invoice_id,line_no,amount) VALUES(?,99,'1')", (i,), "経費は変更できません")
        expect_abort(self, self.c, "DELETE FROM invoice_expenses WHERE invoice_id=?", (i,), "経費は変更できません")
        n = self.c.execute("SELECT COUNT(*) FROM payments WHERE invoice_id=?", (i,)).fetchone()[0]
        self.c.execute("INSERT INTO payments(invoice_id,line_no,pay_date,amount) VALUES(?,?,?,?)", (i, n + 1, "2026-11-30", "1000"))   # 入金は記録できる
        self.c.execute("UPDATE payments SET amount='2000' WHERE invoice_id=? AND line_no=?", (i, n + 1))
        self.c.execute("DELETE FROM payments WHERE invoice_id=? AND line_no=?", (i, n + 1))

    def test_draft_invoice_is_editable_and_deletable(self):
        i = self.draft["id"]
        self.c.execute("UPDATE invoices SET issue_date='2026-11-30' WHERE id=?", (i,))
        self.c.execute("UPDATE invoice_items SET price='999' WHERE invoice_id=?", (i,))
        self.c.execute("DELETE FROM invoices WHERE id=?", (i,))                                    # 明細もまとめて消える
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM invoice_items WHERE invoice_id=?", (i,)).fetchone()[0], 0)

    def test_void_mark_is_write_once(self):
        i = self.issued["id"]
        self.c.execute("UPDATE invoices SET status='取消', voided_at='2026-11-03T00:00:00Z', void_reason='誤り', void_credit_no='KD-X' WHERE id=?", (i,))
        expect_abort(self, self.c, "UPDATE invoices SET void_reason='書換え' WHERE id=?", (i,), "変更できません")
        expect_abort(self, self.c, "UPDATE invoices SET voided_at=NULL WHERE id=?", (i,), "変更できません")
        expect_abort(self, self.c, "INSERT INTO payments(invoice_id,line_no,amount) VALUES(?,99,'1')", (i,), "入金は変更できません")   # 取消後は入金不可

    def test_credit_note_is_locked(self):
        c = self.credit["id"]
        expect_abort(self, self.c, "DELETE FROM invoices WHERE id=?", (c,), "削除できません")
        expect_abort(self, self.c, "INSERT INTO payments(invoice_id,line_no,amount) VALUES(?,99,'1')", (c,), "入金は変更できません")
        expect_abort(self, self.c, "UPDATE invoice_items SET price='1' WHERE invoice_id=?", (c,), "明細は変更できません")

    def test_closed_month_blocks_invoice_changes(self):
        i = self.closed_issued["id"]
        expect_abort(self, self.c, "INSERT INTO payments(invoice_id,line_no,amount) VALUES(?,99,'1')", (i,), "入金は変更できません")
        expect_abort(self, self.c, "DELETE FROM invoices WHERE id=?", (i,))
        expect_abort(self, self.c, "INSERT INTO invoices(id,month,seq,no,status) VALUES('n1',?,99,'N1','未収')", (self.closed_issued["month"],), "締め済みの月に請求書は追加できません")
        # 取消印の付与（status と voided_*）だけは締め済みの月でも可能（赤伝は当月に作成するため）
        self.c.execute("UPDATE invoices SET status='取消', voided_at='2026-11-03T00:00:00Z', void_reason='r', void_credit_no='X' WHERE id=?", (i,))

    def test_closed_month_blocks_man_hours(self):
        closed = self.c.execute("SELECT month FROM closed_months WHERE kind='manHours'").fetchone()[0]
        row = self.c.execute("SELECT id FROM man_hour_rows WHERE month=? LIMIT 1", (closed,)).fetchone()[0]
        expect_abort(self, self.c, "UPDATE man_hour_cells SET value='9' WHERE row_id=?", (row,), "変更できません")
        expect_abort(self, self.c, "INSERT INTO man_hour_cells(row_id,seq,value) VALUES(?,40,'1')", (row,), "変更できません")
        expect_abort(self, self.c, "DELETE FROM man_hour_cells WHERE row_id=?", (row,), "変更できません")
        expect_abort(self, self.c, "INSERT INTO man_hour_rows(month,line_no,content) VALUES(?,99,'x')", (closed,), "変更できません")
        expect_abort(self, self.c, "UPDATE man_hour_months SET carry=5 WHERE month=?", (closed,), "変更できません")
        expect_abort(self, self.c, "INSERT INTO man_hour_expenses(month,line_no,name,amount) VALUES(?,99,'x','1')", (closed,), "変更できません")
        opened = self.c.execute("SELECT month FROM man_hour_months WHERE month NOT IN (SELECT month FROM closed_months WHERE kind='manHours') LIMIT 1").fetchone()[0]
        orow = self.c.execute("SELECT id FROM man_hour_rows WHERE month=? LIMIT 1", (opened,)).fetchone()[0]
        self.c.execute("UPDATE man_hour_cells SET value='9', num_value=9 WHERE row_id=? AND seq=(SELECT MIN(seq) FROM man_hour_cells WHERE row_id=?)", (orow, orow))   # 未締めの月は編集できる

    def test_constraints(self):
        c = self.c
        expect_abort(self, c, "INSERT INTO invoices(id,month,seq,no,status) VALUES('a','2026-12',1,'Z1','謎')")
        expect_abort(self, c, "INSERT INTO invoices(id,month,seq,no,status) VALUES('a','2026/12',1,'Z1','未収')")
        expect_abort(self, c, "INSERT INTO invoices(id,month,seq,no,status,issue_hash) VALUES('a','2026-12',1,'Z1','未収','x')")   # 未発行なのにハッシュ
        expect_abort(self, c, "INSERT INTO invoices(id,month,seq,no,status,credit_of_id) VALUES('a','2026-12',1,'Z1','未収',?)", (self.issued["id"],))  # 赤伝は状態が取消
        expect_abort(self, c, f"INSERT INTO invoices(id,month,seq,no,status) VALUES('a','2026-12',1,?, '未収')", (self.issued["no"],))             # 伝票No.の重複
        c.execute("INSERT INTO invoices(id,month,seq,no,status) VALUES('e1','2026-12',1,'','未収')")                                              # 空のNoは複数可
        c.execute("INSERT INTO invoices(id,month,seq,no,status) VALUES('e2','2026-12',2,'','未収')")
        expect_abort(self, c, "INSERT INTO invoices(id,month,seq,no,status,client_id) VALUES('a','2026-12',3,'Z9','未収','no-such')")             # 外部キー
        expect_abort(self, c, "DELETE FROM clients WHERE id=(SELECT client_id FROM invoices WHERE client_id IS NOT NULL LIMIT 1)")                 # 使用中の取引先は削除不可
        expect_abort(self, c, "INSERT INTO tax_rates(id,sort_order,rate,effective_from) VALUES('bad',9,150,'2020-01-01')")
        expect_abort(self, c, "INSERT INTO man_hour_months(month) VALUES('bad')")
        expect_abort(self, c, "INSERT INTO closed_months(kind,month) VALUES('x','2026-11')")
        c.execute("UPDATE clients SET archived=1 WHERE id=(SELECT client_id FROM invoices WHERE client_id IS NOT NULL LIMIT 1)")               # 代わりに非表示にする
        self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])


class TestViews(unittest.TestCase):
    def setUp(self):
        self.data = fx("rich_v2.json")
        self.db = build_db("rich_v2.json")
        self.c = conn_of(self.db)

    def test_credit_note_cancels_exactly(self):
        orig = self.c.execute("SELECT * FROM v_invoice_totals WHERE voided_at IS NULL AND 0").fetchall()  # 構文確認用（空）
        rows = {r["no"]: r for r in self.c.execute("SELECT * FROM v_invoice_totals")}
        void_no = next(i["no"] for l in self.data["invoices"].values() for i in l if i.get("voided"))
        credit_no = next(i["no"] for l in self.data["invoices"].values() for i in l if i.get("creditOf"))
        a, b = rows[void_no], rows[credit_no]
        self.assertEqual((a["subtotal"] + b["subtotal"], a["tax"] + b["tax"], a["grand"] + b["grand"]), (0, 0, 0))   # 小数・税のずれなし

    def test_client_balance_excludes_voided_and_paid(self):
        rates = self.data["settings"]["taxRates"]
        exp = {}
        for l in self.data["invoices"].values():
            for i in l:
                if i["status"] in ("回収済み", "取消"):
                    continue
                exp[i["clientId"]] = exp.get(i["clientId"], 0) + M.invoice_financials(i, rates)["remaining"]
        for r in self.c.execute("SELECT * FROM v_client_balance"):
            self.assertAlmostEqual(r["outstanding"], exp.get(r["client_id"], 0), places=6)

    def test_site_revenue_and_man_hour_views(self):
        exp = {}
        for l in self.data["invoices"].values():
            for i in l:
                for it in i["items"]:
                    if it.get("site"):
                        exp[it["site"]] = exp.get(it["site"], 0) + M.num_or_zero(it.get("qty")) * M.num_or_zero(it.get("price"))
        got = {r["site_name"]: r["revenue"] for r in self.c.execute("SELECT * FROM v_site_revenue")}
        for k, v in exp.items():
            self.assertAlmostEqual(got[k], round(v, 2), places=2)
        for r in self.c.execute("SELECT * FROM v_man_hour_month"):
            m = self.data["manHours"][r["month"]]
            people = sum(sum(M.cell_number(x.get("kind"), x.get("content"), v) for v in x["daily"].values()) for x in m["rows"] if not M.is_overtime_row(x.get("kind"), x["content"]))
            self.assertAlmostEqual(r["people"], M.round2(people), places=2)

    def test_man_hour_overtime_is_excluded_from_people(self):
        total_all = 0
        for r in self.c.execute("SELECT * FROM v_man_hour_rows"):
            total_all += r["row_total"]
        people = sum(r["people"] for r in self.c.execute("SELECT * FROM v_man_hour_month"))
        ot = sum(r["overtime"] for r in self.c.execute("SELECT * FROM v_man_hour_month"))
        self.assertGreater(ot, 0)
        self.assertAlmostEqual(people + ot, total_all, places=2)


class TestOperations(unittest.TestCase):
    def test_vacuum_into_backup_is_consistent_and_verifiable(self):
        db = build_db("rich_v2.json")
        c = sqlite3.connect(db)
        c.execute("BEGIN"); c.execute("UPDATE clients SET archived=0"); c.execute("COMMIT")      # 書き込み可能な接続でも取得できる
        bak = os.path.join(TMP, "vacuum_backup.db")
        c.execute(f"VACUUM INTO '{bak}'")
        c.close()
        self.assertEqual(M.verify_migration(fx("rich_v2.json"), bak), [])
        self.assertEqual(sqlite3.connect(bak).execute("PRAGMA user_version").fetchone()[0], 1)

    def test_online_backup_api(self):
        db = build_db("rich_v2.json")
        src = sqlite3.connect(db); dst = sqlite3.connect(os.path.join(TMP, "api_backup.db"))
        src.backup(dst); dst.close(); src.close()
        self.assertEqual(M.verify_migration(fx("rich_v2.json"), os.path.join(TMP, "api_backup.db")), [])

    def test_wal_mode_and_concurrent_reader(self):
        db = build_db("rich_v2.json")
        w = sqlite3.connect(db); w.execute("PRAGMA journal_mode=WAL")
        r = sqlite3.connect(db)
        w.execute("BEGIN"); w.execute("UPDATE clients SET email='x@example.com' WHERE rowid=1")
        self.assertEqual(r.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 5)           # 書き込み中でも読める
        w.execute("ROLLBACK")

    def test_performance_large_dataset(self):
        base = fx("rich_v2.json"); rates = base["settings"]["taxRates"]
        data = copy.deepcopy(base)
        data["invoices"] = {}
        n = 0
        for y in range(2021, 2027):
            for m in range(1, 13):
                lst = []
                for k in range(80):
                    n += 1
                    lst.append({"id": f"p{n}", "clientId": base["settings"]["clients"][k % 2]["id"], "date": f"{y}-{m:02d}-15", "no": f"KD{y}{m:02d}-{k+1:03d}",
                                "taxRateId": rates[-1]["id"] if False else base["settings"]["taxRates"][0]["id"], "status": "未収",
                                "items": [{"site": f"現場{k % 7}", "content": "型枠工事", "qty": "10", "unit": "m2x", "price": "2100"}, {"site": "現場B", "content": "x", "qty": "5", "unit": "m2x", "price": "2000"}],
                                "expenses": [{"name": "法定福利費", "amount": "380000"}], "payments": [{"date": f"{y}-{m:02d}-28", "amount": "100000"}]})
                data["invoices"][f"{y}-{m:02d}"] = lst
        p = os.path.join(TMP, "big.json"); dump_json(data, p)
        t0 = time.time(); db = os.path.join(TMP, "big.db"); M.migrate(p, db, overwrite=True); t_mig = time.time() - t0
        c = sqlite3.connect(db)
        t0 = time.time(); total = c.execute("SELECT COUNT(*), SUM(grand) FROM v_invoice_totals").fetchone(); t_view = time.time() - t0
        t0 = time.time(); c.execute("SELECT * FROM v_client_balance").fetchall(); t_bal = time.time() - t0
        print(f"\n  [性能] 請求書 {total[0]:,} 件（{os.path.getsize(db)/1048576:.1f}MB）：移行＋自己検証 {t_mig:.1f}秒／全件の金額集計 {t_view*1000:.0f}ms／取引先別残高 {t_bal*1000:.0f}ms")
        self.assertEqual(total[0], 72 * 80)
        self.assertLess(t_view, 3.0)
        self.assertLess(t_mig, 120)


if __name__ == "__main__":
    unittest.main(verbosity=1)
