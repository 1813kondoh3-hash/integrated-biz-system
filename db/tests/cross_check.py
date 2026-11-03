#!/usr/bin/env python3
"""Python（参照実装）と Rust（本番に近い実装）の相互運用テスト。  使い方:  python tests/cross_check.py
  1) Python が暗号化DB・鍵・復旧コードを作る → Rust が開いて検証する（金額・操作ログ・トリガー・鍵の扱い）
  2) Rust が暗号化DB・復旧コードを作る   → Python が開いて検証する
前提: cargo（Rust 1.75 以上）、pip install sqlcipher3-wheels"""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import migrate_json_to_sqlite as M  # noqa: E402

os.environ["PYTHONUTF8"] = "1"              # 子プロセス（移行ツールなど）の出力をUTF-8にする（Windowsの既定の文字コードでは、日本語を出力できず落ちる）
os.environ["PYTHONIOENCODING"] = "utf-8"
for _s in (sys.stdout, sys.stderr):           # このプロセスの出力も、出力できない文字があっても落ちないようにする
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

CRATE = os.path.join(ROOT, "..", "rust", "ibis-core")
FX = os.path.join(HERE, "fixtures")
TMP = tempfile.mkdtemp(prefix="ibis_cross_")


class TestCrossImplementation(unittest.TestCase):
    def test_python_and_rust_interoperate(self):
        key = M.generate_key_hex()
        db = os.path.join(TMP, "py_enc.db")
        M.migrate(os.path.join(FX, "rich_v2.json"), db, overwrite=True, key_hex=key)
        conn = M.open_db(db, key)
        grand_sum = conn.execute("SELECT SUM(grand) FROM v_invoice_totals").fetchone()[0]
        conn.close()
        code_key = M.generate_key_hex()
        rs_out = os.path.join(TMP, "rs_enc.db")
        env = dict(os.environ, IBIS_PY_DB=db, IBIS_PY_KEY=key, IBIS_PY_GRAND_SUM=repr(grand_sum),
                   IBIS_PY_CODE=M.recovery_code(code_key), IBIS_PY_CODE_KEY=code_key, IBIS_RS_OUT=rs_out,
                   CARGO_TARGET_DIR=os.environ.get("CARGO_TARGET_DIR", os.path.join(TMP, "target")))
        feats = os.environ.get("IBIS_CARGO_FEATURES")                       # 例：Windowsでは vendored-openssl
        cmd = ["cargo", "test"] + (["--features", feats] if feats else []) + ["--", "--nocapture", "--test-threads=1"]
        r = subprocess.run(cmd, cwd=CRATE, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(r.stdout[-1500:] if r.returncode else "\n".join(l for l in r.stdout.splitlines() if l.startswith("test ") or l.startswith("test result")))
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        # --- Rust が作ったものを Python で検証 ---
        with open(rs_out + ".key") as f:
            rkey = f.read().strip()
        c = M.open_db(rs_out, rkey)
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(c.execute("PRAGMA cipher_integrity_check").fetchall(), [])
        entries = [dict(seq=r[0], time=r[1], action=r[2], prev=r[3], hash=r[4]) for r in c.execute("SELECT seq,time,action,prev_hash,hash FROM audit_log ORDER BY seq")]
        self.assertEqual(len(entries), 3)
        self.assertIsNone(M.verify_audit_chain(entries), "Rustが書いた操作ログの連鎖を、Pythonが検証できる")
        t = c.execute("SELECT subtotal, tax, grand, remaining FROM v_invoice_totals").fetchone()
        self.assertEqual(tuple(t), (705.0, 71.0, 776.0, 476.0))
        with self.assertRaises(Exception):
            c.execute("UPDATE invoice_items SET price='1'")
        c.close()
        with open(rs_out + ".code") as f:
            rcode, rcode_key = f.read().split()
        self.assertEqual(M.parse_recovery_code(rcode), rcode_key, "Rustが作った復旧コードを、Pythonが読める")


if __name__ == "__main__":
    unittest.main(verbosity=1)
