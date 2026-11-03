#!/usr/bin/env python3
"""check_secrets.py のテスト。  python tools/test_check_secrets.py"""
import os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_secrets as C  # noqa: E402

os.environ["PYTHONUTF8"] = "1"              # 子プロセス（移行ツールなど）の出力をUTF-8にする（Windowsの既定の文字コードでは、日本語を出力できず落ちる）
os.environ["PYTHONIOENCODING"] = "utf-8"
for _s in (sys.stdout, sys.stderr):           # このプロセスの出力も、出力できない文字があっても落ちないようにする
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

HEX = "0123456789abcdef" * 4


class TestPaths(unittest.TestCase):
    def test_blocked(self):
        for p in ("data.db", "a/b/backup.sqlite", "key.pem", "my.key", "請求書.xlsx", "x.XLSM", "list.csv", ".env", ".env.local", "a/.env.production", "x.bak"):
            self.assertIsNotNone(C.check_path(p), p)

    def test_allowed(self):
        for p in ("schema.sql", "data.json", "README.md", "app/integrated-biz-system.html", "db/tests/fixtures/rich_v2.json", "environment.md", "tools/check-env.ps1"):
            self.assertIsNone(C.check_path(p), p)


class TestContent(unittest.TestCase):
    def levels(self, path, text):
        return [(lv, rule) for lv, rule, _, _ in C.scan_text(path, text)]

    def test_hex_key_detected_and_masked(self):
        f = C.scan_text("src/a.py", f'KEY = "{HEX}"\n')
        self.assertEqual([(x[0], x[2]) for x in f], [("BLOCK", 1)])
        self.assertNotIn(HEX, f[0][3], "値そのものは表示しない")
        self.assertLessEqual(len(f[0][3]), 20)

    def test_hex_allowed_places_and_trivial_values(self):
        self.assertEqual(C.scan_text("db/tests/fixtures/rich_v2.json", f'"hash":"{HEX}"'), [])
        self.assertEqual(C.scan_text("rust/ibis-core/Cargo.lock", f'checksum = "{HEX}"'), [])
        self.assertEqual(C.scan_text("db/schema.sql", "'" + "0" * 64 + "'"), [])
        self.assertEqual(C.scan_text("a.py", "x = '" + "a" * 64 + "'"), [])
        self.assertEqual(C.scan_text("a.py", f'KEY = "{HEX}"  # allow-secret テスト用の固定値'), [])
        self.assertEqual(C.scan_text("a.py", "x = '" + HEX + "0'"), [], "65桁は鍵の形式ではない")

    def test_recovery_code_and_other_secrets(self):
        code = "-".join(["ABCDE", "FGHIJ", "KLMNO", "PQRST", "UVWXY", "ZABCD", "EFGHI", "JKLMN", "OPQRS", "TUVWX", "YZ234"])
        self.assertIn(("BLOCK", "復旧コード"), self.levels("a.md", f"控え：{code}"))
        self.assertIn(("BLOCK", "秘密鍵"), self.levels("a", "-----BEGIN PRIVATE KEY-----"))  # allow-secret 検出のテスト用の見本（実在の値ではない）
        self.assertIn(("BLOCK", "秘密鍵"), self.levels("a", "-----BEGIN RSA PRIVATE KEY-----"))  # allow-secret 検出のテスト用の見本（実在の値ではない）
        self.assertIn(("BLOCK", "GitHubのトークン"), self.levels("a", "token=ghp_" + "a1B2" * 9))
        self.assertIn(("BLOCK", "GitHubのトークン"), self.levels("a", "github_pat_" + "A1b_" * 12))
        self.assertIn(("BLOCK", "AWSのアクセスキー"), self.levels("a", "AKIA" + "ABCDEFGHIJKLMNOP"))
        self.assertIn(("BLOCK", "カード番号らしき並び"), self.levels("a", "4111-1111-1111-1111"))  # allow-secret 検出のテスト用の見本（実在の値ではない）

    def test_registration_number(self):
        self.assertEqual(self.levels("a", "登録番号 T1234567890123"), [])
        self.assertEqual(self.levels("a", "登録番号 T9876543210987"), [], "見本データのダミー")
        self.assertEqual([r for r in self.levels("a", "登録番号 T2468013579246") if r[0] == "BLOCK"][0][1].startswith("インボイス登録番号"), True)  # allow-secret 検出のテスト用の見本（実在の値ではない）
        self.assertEqual(self.levels("a", "ID: T12345"), [])

    def test_company_names_warn_not_block(self):
        self.assertEqual(self.levels("a.md", "取引先：株式会社山田建設"), [("WARN", "会社名らしき文字列")])  # allow-secret 検出のテスト用の見本（実在の値ではない）
        self.assertEqual(self.levels("a.md", "山田建設株式会社 御中"), [("WARN", "会社名らしき文字列")])  # allow-secret 検出のテスト用の見本（実在の値ではない）
        self.assertEqual(self.levels("a.md", "株式会社サンプル工業 / Ｂ建設株式会社 / 株式会社テスト"), [])  # allow-secret 検出のテスト用の見本（実在の値ではない）
        self.assertEqual(self.levels("a.md", "株式会社○○ とは"), [])  # allow-secret 検出のテスト用の見本（実在の値ではない）

    def test_punctuation_before_company_word_is_not_a_name(self):
        self.assertEqual(self.levels("a.md", "会社名らしき文字列（「株式会社」などの語）"), [])
        self.assertEqual(self.levels("a.md", "・「株式会社」を含む文字列"), [])

    def test_clean_text(self):
        self.assertEqual(C.scan_text("a.md", "これは普通の文章です。SHA-256 は 64桁の16進数で表されます。"), [])


class TestCli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        run = lambda *a: subprocess.run(a, cwd=self.d, capture_output=True, text=True, encoding="utf-8")
        self.run = run
        run("git", "init", "-q")
        run("git", "config", "user.email", "t@example.invalid"); run("git", "config", "user.name", "t")
        os.makedirs(os.path.join(self.d, "tools"))
        shutil.copy(os.path.join(HERE, "check_secrets.py"), os.path.join(self.d, "tools", "check_secrets.py"))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, name, text="x\n"):
        p = os.path.join(self.d, name); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def check(self, *args):
        return self.run(sys.executable, "tools/check_secrets.py", *args)

    def test_staged(self):
        self.write("ok.md", "問題のない文書\n"); self.run("git", "add", "ok.md")
        r = self.check("--staged"); self.assertEqual(r.returncode, 0, r.stdout)
        self.write("data.db", "binary"); self.run("git", "add", "data.db")
        r = self.check("--staged"); self.assertEqual(r.returncode, 1); self.assertIn("data.db", r.stdout)
        self.run("git", "reset", "-q", "data.db")
        self.write("conf.py", f'KEY="{HEX}"\n'); self.run("git", "add", "conf.py")
        r = self.check("--staged"); self.assertEqual(r.returncode, 1)
        self.assertNotIn(HEX, r.stdout + r.stderr, "鍵の値は出力に出ない")

    def test_japanese_file_names_are_checked(self):
        """日本語のファイル名（gitは通常、引用符つきの表記にする）でも、拡張子で止まる"""
        for name in ("見積一覧.xlsx", "取引先リスト 2026.csv", "フォルダ/請求データ.xlsm"):
            self.write(name, "x"); self.run("git", "add", "-f", name)
        r = self.check("--staged")
        self.assertEqual(r.returncode, 1, r.stdout)
        for name in ("見積一覧.xlsx", "取引先リスト 2026.csv", "請求データ.xlsm"):
            self.assertIn(name, r.stdout)
        r2 = self.check("--all"); self.assertEqual(r2.returncode, 1)

    def test_japanese_file_name_content_is_scanned(self):
        self.write("メモ/鍵のメモ.md", f"鍵：{HEX}\n"); self.run("git", "add", "-f", "メモ/鍵のメモ.md")
        r = self.check("--staged"); self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("鍵のメモ.md", r.stdout)

    def test_untracked_files_are_not_checked_when_staged(self):
        self.write("ok.md"); self.run("git", "add", "ok.md"); self.write("secret.key")
        self.assertEqual(self.check("--staged").returncode, 0)

    def test_all_and_strict_names(self):
        self.write("a.md", "株式会社山田建設\n"); self.run("git", "add", "a.md")  # allow-secret 検出のテスト用の見本（実在の値ではない）
        self.assertEqual(self.check("--all").returncode, 0)                    # 注意のみ
        r = self.check("--all", "--strict-names"); self.assertEqual(r.returncode, 1)

    def test_binary_content_is_not_scanned(self):
        p = os.path.join(self.d, "img.png")
        with open(p, "wb") as f:
            f.write(b"\x89PNG\0" + HEX.encode())
        self.assertEqual(self.check("img.png").returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
