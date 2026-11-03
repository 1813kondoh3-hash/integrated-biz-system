#!/usr/bin/env python3
"""SQLCipher（DBの暗号化）のテスト。  使い方:  python tests/test_encryption.py
前提: pip install sqlcipher3-wheels"""
import json, os, shutil, sqlite3, stat, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import migrate_json_to_sqlite as M  # noqa: E402

try:
    from sqlcipher3 import dbapi2 as sc
except Exception:                      # pragma: no cover
    sc = None

FX = os.path.join(HERE, "fixtures")
TMP = tempfile.mkdtemp(prefix="ibis_enc_test_")
MARKERS = ["サンプル工業", "Ｂ建設", "KD202611", "型枠工事", "山田 太郎", "T1234567890123"]   # 平文で保存されていたら漏洩とみなす文字列


def fx(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return json.load(f)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def file_has_markers(path):
    raw = read_bytes(path)
    return [m for m in MARKERS if m.encode("utf-8") in raw]


def make(name, key=None):
    out = os.path.join(TMP, f"{name}_{len(os.listdir(TMP))}.db")
    M.migrate(os.path.join(FX, "rich_v2.json"), out, overwrite=True, key_hex=key)
    return out


@unittest.skipIf(sc is None, "sqlcipher3 が必要です")
class TestEncryptedDb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = M.generate_key_hex()
        cls.enc = make("enc", cls.key)
        cls.plain = make("plain")
        cls.data = fx("rich_v2.json")

    def test_migration_and_roundtrip(self):
        self.assertEqual(M.verify_migration(self.data, self.enc, self.key), [])
        self.assertEqual(M.normalize(self.data), M.normalize(M.export_sqlite_to_json(self.enc, self.key)))

    def test_no_plaintext_in_encrypted_file(self):
        with open(self.enc, "rb") as f:
            head = f.read(16)
        self.assertNotEqual(head, b"SQLite format 3\x00")
        self.assertEqual(file_has_markers(self.enc), [])
        self.assertGreater(len(file_has_markers(self.plain)), 3, "対照：暗号化しないDBには平文が残る（検査自体が有効であることの確認）")

    def test_wrong_missing_or_misapplied_key(self):
        with self.assertRaises(sqlite3.DatabaseError):                         # 鍵なし（通常のSQLiteで開く）
            sqlite3.connect(self.enc).execute("SELECT count(*) FROM sqlite_master").fetchone()
        with self.assertRaises(M.MigrationError):                              # 違う鍵
            M.open_db(self.enc, M.generate_key_hex())
        with self.assertRaises(M.MigrationError):                              # 平文DBに鍵を指定
            M.open_db(self.plain, self.key)
        with self.assertRaises(M.MigrationError):                              # 暗号化DBを鍵なしで（SQLCipher経由でも）
            M.open_db(self.enc, None)

    def test_key_format_validation(self):
        for bad in ("", "abc", "g" * 64, "a" * 63, "a" * 65):
            with self.assertRaises(M.MigrationError):
                M.normalize_key(bad)
        self.assertEqual(M.normalize_key(" " + "A" * 64 + "\n"), "a" * 64)

    def test_guards_still_enforced(self):
        c = M.open_db(self.enc, self.key)
        with self.assertRaises(sc.DatabaseError):
            c.execute("UPDATE audit_log SET action='x' WHERE seq=1")
        with self.assertRaises(sc.DatabaseError):
            c.execute("DELETE FROM invoices WHERE issued_at IS NOT NULL")
        closed = c.execute("SELECT month FROM closed_months WHERE kind='manHours'").fetchone()[0]
        with self.assertRaises(sc.DatabaseError):
            c.execute("INSERT INTO man_hour_rows(month,line_no,content) VALUES(?,99,'x')", (closed,))
        self.assertTrue(c.execute("SELECT COUNT(*) FROM v_invoice_totals").fetchone()[0] > 0)       # ビュー・生成列・JSON関数も動く
        c.close()

    def test_cipher_settings_are_sqlcipher4_defaults(self):
        c = M.open_db(self.enc, self.key)
        self.assertTrue(c.execute("PRAGMA cipher_version").fetchone()[0].startswith("4."))
        self.assertEqual(int(c.execute("PRAGMA cipher_page_size").fetchone()[0]), 4096)
        self.assertEqual(c.execute("PRAGMA cipher_hmac_algorithm").fetchone()[0], "HMAC_SHA512")
        self.assertEqual(c.execute("PRAGMA cipher_kdf_algorithm").fetchone()[0], "PBKDF2_HMAC_SHA512")
        c.close()

    def test_wal_files_are_encrypted(self):
        marker = "秘密マーカー-ZXQ-12345"
        for name, key in (("walenc", self.key), ("walplain", None)):
            path = os.path.join(TMP, name + ".db")
            shutil.copy(self.enc if key else self.plain, path)
            c = M.open_db(path, key)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA wal_autocheckpoint=0")
            c.execute("UPDATE clients SET email=? WHERE rowid=1", (marker,))
            c.commit()
            wal = path + "-wal"
            self.assertTrue(os.path.exists(wal) and os.path.getsize(wal) > 0)
            leaked = marker.encode("utf-8") in read_bytes(wal) or marker.encode("utf-8") in read_bytes(path)
            self.assertEqual(leaked, key is None, f"{name}: WALの平文 leaked={leaked}")
            c.close()

    def test_vacuum_into_keeps_encryption(self):
        c = M.open_db(self.enc, self.key)
        bak = os.path.join(TMP, "vacuum_enc.db")
        c.execute(f"VACUUM INTO '{bak}'")
        c.close()
        with open(bak, "rb") as f:
            self.assertNotEqual(f.read(16), b"SQLite format 3\x00", "VACUUM INTO の出力が平文になっている")
        self.assertEqual(file_has_markers(bak), [])
        self.assertEqual(M.verify_migration(self.data, bak, self.key), [])

    def test_export_to_new_key_and_rekey(self):
        new_key = M.generate_key_hex()
        # 方法1：別ファイルへ新しい鍵で書き出す（sqlcipher_export）
        dst = os.path.join(TMP, "exported_newkey.db")
        c = M.open_db(self.enc, self.key)
        c.execute(f"ATTACH DATABASE '{dst}' AS newdb KEY \"x'{new_key}'\"")
        c.execute("SELECT sqlcipher_export('newdb')")
        c.execute("DETACH DATABASE newdb")
        c.close()
        self.assertEqual(M.verify_migration(self.data, dst, new_key), [])
        with self.assertRaises(M.MigrationError):
            M.open_db(dst, self.key)
        # 方法2：その場で鍵を変更（rekey）
        path = os.path.join(TMP, "rekey.db")
        shutil.copy(self.enc, path)
        c = M.open_db(path, self.key)
        c.execute(f"PRAGMA rekey = \"x'{new_key}'\"")
        c.close()
        self.assertEqual(M.verify_migration(self.data, path, new_key), [])
        with self.assertRaises(M.MigrationError):
            M.open_db(path, self.key)
        self.assertEqual(file_has_markers(path), [])

    def test_cipher_integrity_check_detects_corruption(self):
        path = os.path.join(TMP, "corrupt.db")
        shutil.copy(self.enc, path)
        c = M.open_db(path, self.key)
        self.assertEqual(c.execute("PRAGMA cipher_integrity_check").fetchall(), [])             # 健全なら空
        c.close()
        raw = bytearray(read_bytes(path))
        for off in (4096 * 3 + 100, 4096 * 5 + 2000):                                            # 途中のページを壊す
            raw[off] ^= 0xFF
        with open(path, "wb") as f:
            f.write(raw)
        c = M.open_db(path, self.key)
        detected = False
        try:
            detected = bool(c.execute("PRAGMA cipher_integrity_check").fetchall())
        except Exception:
            detected = True
        c.close()
        self.assertTrue(detected, "破損（改ざん）を検出できなかった")


@unittest.skipIf(sc is None, "sqlcipher3 が必要です")
class TestRecoveryCode(unittest.TestCase):
    def test_roundtrip_and_format(self):
        for _ in range(50):
            k = M.generate_key_hex()
            code = M.recovery_code(k)
            groups = code.split("-")
            self.assertEqual((len(groups), {len(g) for g in groups[:-1]}), (11, {5}))
            self.assertEqual(M.parse_recovery_code(code), k)
            self.assertEqual(M.parse_recovery_code(code.lower().replace("-", " ")), k)          # 小文字・空白区切りでも読める

    def test_handwriting_lookalikes_are_tolerated(self):
        """手書きで取り違えやすい文字を読み替える。0↔O・8↔B は常に正しく読める。1↔I・L はあいまいなので、
        候補が一意に決まらないときは「判別できません」とエラーにする（別の鍵を黙って受け付けることはない）"""
        wrong = ambiguous = checked = 0
        for _ in range(400):
            k = M.generate_key_hex()
            code = M.recovery_code(k)
            self.assertEqual(M.parse_recovery_code(code.replace("O", "0").replace("B", "8")), k)
            if code.count("I") + code.count("L") <= 4:
                checked += 1
                try:
                    got = M.parse_recovery_code(code.replace("I", "1").replace("L", "1"))
                    wrong += (got != k)
                except M.MigrationError as e:
                    ambiguous += 1
                    self.assertIn("判別できません", str(e))
        self.assertEqual(wrong, 0, "別の鍵を受け付けてはならない")
        self.assertGreater(checked, 100)
        self.assertLess(ambiguous / checked, 0.05, f"判別不能の割合が高すぎる {ambiguous}/{checked}")
        k = M.generate_key_hex(); code = M.recovery_code(k)
        with self.assertRaises(M.MigrationError):
            M.parse_recovery_code(code.replace(code[0], "Z" if code[0] != "Z" else "Y", 1))

    def test_detects_typos(self):
        """1文字の入力ミスは、必ず「エラー」になる。別の鍵として受け付けてしまうことはない。
        （最後の1文字だけは、鍵に使われないビットを含むため、そこだけの違いは同じ鍵として読める＝実害なし）"""
        for k in ("0123456789abcdef" * 4, M.generate_key_hex(), "ff" * 32):
            code = M.recovery_code(k)
            alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
            positions = [i for i, ch in enumerate(code) if ch != "-"]
            last = positions[-1]
            wrong_key = benign_elsewhere = 0
            for i in positions:
                for ch in alphabet:
                    if ch == code[i]:
                        continue
                    try:
                        got = M.parse_recovery_code(code[:i] + ch + code[i + 1:])
                    except M.MigrationError:
                        continue                                   # 検出できた
                    if got != k:
                        wrong_key += 1                             # 別の鍵として受け付けた（あってはならない）
                    elif i != last:
                        benign_elsewhere += 1
            self.assertEqual(wrong_key, 0, "入力ミスを別の鍵として受け付けた")
            self.assertEqual(benign_elsewhere, 0, "最後の文字以外の入力ミスを見逃した")
        with self.assertRaises(M.MigrationError):
            M.parse_recovery_code(code[:-3])                       # 短い
        with self.assertRaises(M.MigrationError):
            M.parse_recovery_code("これは復旧コードではありません")


@unittest.skipIf(sc is None, "sqlcipher3 が必要です")
class TestCli(unittest.TestCase):
    def test_keygen_migrate_verify_export(self):
        script = os.path.join(ROOT, "migrate_json_to_sqlite.py")
        keyf = os.path.join(TMP, "cli_key.txt")
        db = os.path.join(TMP, "cli_enc.db")
        out = os.path.join(TMP, "cli_enc.json")
        run = lambda *a: subprocess.run([sys.executable, script, *a], capture_output=True, text=True)
        r = run("keygen", keyf)
        self.assertEqual(r.returncode, 0, r.stderr)
        code = [ln.strip() for ln in r.stdout.splitlines() if "-" in ln and len(ln.strip()) > 40][0]
        self.assertEqual(oct(stat.S_IMODE(os.stat(keyf).st_mode)), oct(0o600))
        self.assertNotEqual(run("keygen", keyf).returncode, 0, "既存の鍵ファイルを上書きしてはならない")
        self.assertEqual(M.parse_recovery_code(code), M.read_key_file(keyf))
        r = run("migrate", os.path.join(FX, "rich_v2.json"), db, "--overwrite", "--key-file", keyf)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("暗号化あり", r.stdout)
        self.assertEqual(run("verify", os.path.join(FX, "rich_v2.json"), db, "--key-file", keyf).stdout.strip(), "合格")
        self.assertNotEqual(run("verify", os.path.join(FX, "rich_v2.json"), db).returncode, 0, "鍵なしでは開けない")
        self.assertEqual(run("export", db, out, "--key-file", keyf).returncode, 0)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(M.normalize(fx("rich_v2.json")), M.normalize(json.load(f)))
        # 復旧コードから鍵を作り直して、同じDBが開ける
        keyf2 = os.path.join(TMP, "cli_key2.txt")
        self.assertEqual(run("key-from-code", code, keyf2).returncode, 0)
        self.assertEqual(run("verify", os.path.join(FX, "rich_v2.json"), db, "--key-file", keyf2).stdout.strip(), "合格")


@unittest.skipIf(sc is None, "sqlcipher3 が必要です")
class TestRecoverySheet(unittest.TestCase):
    def test_sheet_contains_code_but_not_the_key(self):
        key = M.generate_key_hex()
        html = M.recovery_sheet_html(key, "事務所PC <script>alert(1)</script>")
        code = M.recovery_code(key)
        for g in code.split("-"):
            self.assertIn(g, html)
        self.assertNotIn(key, html, "鍵（16進）は載せない")
        self.assertNotIn("<script>", html, "入力した文字はエスケープされる")
        self.assertIn("&lt;script&gt;", html)
        self.assertEqual(sum(len(g) for g in code.split("-")), 55)
        for word in ("PCとは別の場所", "写真に撮る", "再入力による確認"):
            self.assertIn(word, html)

    def test_cli_sheet_and_check_code(self):
        script = os.path.join(ROOT, "migrate_json_to_sqlite.py")
        run = lambda *a: subprocess.run([sys.executable, script, *a], capture_output=True, text=True)
        keyf = os.path.join(TMP, "sheet_key.txt"); sheet = os.path.join(TMP, "sheet.html")
        r = run("keygen", keyf); self.assertEqual(r.returncode, 0, r.stderr)
        code = [ln.strip() for ln in r.stdout.splitlines() if "-" in ln and len(ln.strip()) > 40][0]
        r = run("recovery-sheet", keyf, sheet, "--label", "テスト"); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(oct(stat.S_IMODE(os.stat(sheet).st_mode)), oct(0o600))
        self.assertNotEqual(run("recovery-sheet", keyf, sheet).returncode, 0, "既存の控えを上書きしない")
        with open(sheet, encoding="utf-8") as f:
            self.assertIn(code.split("-")[0], f.read())
        self.assertEqual(run("check-code", keyf, code).returncode, 0)                                  # 正しく控えた
        self.assertEqual(run("check-code", keyf, code.lower().replace("-", " ")).returncode, 0)          # 小文字・空白区切りでも
        typo = code[:3] + ("A" if code[3] != "A" else "B") + code[4:]
        r = run("check-code", keyf, typo); self.assertNotEqual(r.returncode, 0)                         # 1文字の書き間違い
        self.assertIn("入力ミス", r.stderr)
        other = M.recovery_code(M.generate_key_hex())
        r = run("check-code", keyf, other); self.assertNotEqual(r.returncode, 0)                        # 別の鍵のコード
        self.assertIn("一致しません", r.stderr)


@unittest.skipIf(sc is None, "sqlcipher3 が必要です")
class TestPerformance(unittest.TestCase):
    def test_overhead(self):
        base = fx("rich_v2.json")
        data = json.loads(json.dumps(base)); data["invoices"] = {}
        n = 0
        for y in range(2021, 2027):
            for m in range(1, 13):
                lst = []
                for k in range(80):
                    n += 1
                    lst.append({"id": f"p{n}", "clientId": base["settings"]["clients"][k % 2]["id"], "date": f"{y}-{m:02d}-15", "no": f"KD{y}{m:02d}-{k+1:03d}",
                                "taxRateId": base["settings"]["taxRates"][0]["id"], "status": "未収",
                                "items": [{"site": f"現場{k % 7}", "content": "型枠工事", "qty": "10", "unit": "m2x", "price": "2100"}, {"site": "現場B", "content": "x", "qty": "5", "unit": "m2x", "price": "2000"}],
                                "expenses": [{"name": "法定福利費", "amount": "380000"}], "payments": [{"date": f"{y}-{m:02d}-28", "amount": "100000"}]})
                data["invoices"][f"{y}-{m:02d}"] = lst
        p = os.path.join(TMP, "big.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        res = {}
        for label, key in (("平文", None), ("暗号化", M.generate_key_hex())):
            db = os.path.join(TMP, f"big_{label}.db")
            t0 = time.time(); M.migrate(p, db, overwrite=True, key_hex=key); t_mig = time.time() - t0
            c = M.open_db(db, key)
            t0 = time.time(); c.execute("SELECT COUNT(*), SUM(grand) FROM v_invoice_totals").fetchone(); t_view = time.time() - t0
            t0 = time.time(); c.execute("SELECT * FROM v_client_balance").fetchall(); t_bal = time.time() - t0
            t0 = time.time(); M.open_db(db, key).close(); t_open = time.time() - t0
            c.close()
            res[label] = (t_mig, t_view, t_bal, t_open, os.path.getsize(db))
        for k, (a, b, c_, d, sz) in res.items():
            print(f"\n  [性能:{k}] 請求書5,760件 サイズ{sz/1048576:.1f}MB：移行＋検証 {a:.1f}秒／金額集計 {b*1000:.0f}ms／残高集計 {c_*1000:.0f}ms／DBを開く {d*1000:.0f}ms", end="")
        print()
        self.assertLess(res["暗号化"][1], 3.0)
        self.assertLess(res["暗号化"][3], 1.0, "鍵を直接指定するので、開くのに時間がかからない")
        self.assertLess(res["暗号化"][1] / max(res["平文"][1], 1e-6), 8, "暗号化による遅延が大きすぎる")


if __name__ == "__main__":
    unittest.main(verbosity=1)
