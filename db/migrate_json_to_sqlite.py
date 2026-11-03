#!/usr/bin/env python3
"""
統合業務システム  JSON(データ形式 v2) → SQLite 移行ツール（参照実装）

  python migrate_json_to_sqlite.py migrate  backup.json  data.db [--overwrite]
  python migrate_json_to_sqlite.py verify   backup.json  data.db
  python migrate_json_to_sqlite.py export   data.db      restored.json
  python migrate_json_to_sqlite.py keygen   key.txt                       # 暗号鍵（256ビット）を生成して保存し、復旧コードを表示
  python migrate_json_to_sqlite.py key-from-code  CODE  key.txt           # 復旧コードから鍵ファイルを作り直す
  python migrate_json_to_sqlite.py recovery-sheet key.txt sheet.html      # 復旧コードの「紙の控え」（印刷用HTML）を作る
  python migrate_json_to_sqlite.py check-code     key.txt CODE            # 紙に控えたコードを再入力して、鍵と一致するか確認
  ※ migrate / verify / export に --key-file key.txt を付けると、SQLCipherで暗号化したDBを扱う

目的
  ・C案（Tauri＋SQLite）の移行処理の「仕様」と「合格基準」を、動く形で示す
  ・Rust側の実装は、このツールと同じ結果（往復一致・計算一致）になることを確認する
方針
  ・入力は、アプリが書き出した「エンベロープ形式」または旧形式（生のDATA）。形式がv2でなければ拒否する
    （v1→v2 の変換はアプリ側の migrateData が行う。ここでは重複実装しない）
  ・変換は1つのトランザクション。失敗したらDBファイルは作らない（元JSONは一切変更しない）
  ・移行後に自己検証を行い、1つでも不一致があれば失敗扱いにする
"""
import argparse, base64, copy, hashlib, json, math, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone

try:                                     # 暗号化（SQLCipher）。無い環境では、暗号化なしのDBだけ扱える
    from sqlcipher3 import dbapi2 as _sqlcipher
except Exception:                        # pragma: no cover
    _sqlcipher = None

TOOL_VERSION = "json2sqlite 1.1"
HERE = os.path.dirname(os.path.abspath(__file__))
GENESIS = "0" * 64
STATUSES = ("未収", "請求済み", "一部回収", "回収済み", "取消")
MIGRATION_ROW_STEP = "JSON(v2)→SQLite(db v1)"
MONTH_RE = re.compile(r"^[0-9]{4}-[0-9]{2}$")

# =====================================================================
#  JavaScript と同じ解釈をするための関数（現行アプリの挙動を再現）
# =====================================================================
_FULL = {ord("０") + i: ord("0") + i for i in range(10)}
_NUM_PREFIX = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_NUM_FULL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def js_string(v):
    """JavaScript の String(v) 相当（null→""）"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if v.is_integer() and abs(v) < 1e21:
            return str(int(v))
        return repr(v)
    return str(v)


def js_str_or_empty(v):
    """JavaScript の String(v || "")（0・空・null は空文字になる点まで再現）"""
    if v is None or v == "" or v == 0 or v is False:
        return ""
    return js_string(v)


def js_parse_float(v):
    """parseFloat(v)（先頭の数値部分だけを読む）。読めなければ NaN"""
    s = js_string(v).lstrip()
    m = _NUM_PREFIX.match(s)
    if m:
        return float(m.group())
    m = re.match(r"([+-]?)Infinity", s)
    if m:
        return float("-inf") if m.group(1) == "-" else float("inf")
    return float("nan")


def js_number(s):
    """Number(s)（文字列全体が数値でなければ NaN。0x10 は16）"""
    s = s.strip()
    if s == "":
        return 0.0
    if _NUM_FULL.fullmatch(s):
        return float(s)
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", s):
        return float(int(s, 16))
    if re.fullmatch(r"0[bB][01]+", s):
        return float(int(s, 2))
    if re.fullmatch(r"0[oO][0-7]+", s):
        return float(int(s, 8))
    if re.fullmatch(r"[+-]?Infinity", s):
        return float("-inf") if s.startswith("-") else float("inf")
    return float("nan")


def to_half_num_str(v):
    s = js_string(v).translate(_FULL)
    s = s.replace("．", ".").replace("。", ".").replace("，", "").replace(",", "")
    return s.strip()


def is_overtime_row(kind, content):
    if kind == "overtime":
        return True
    if kind == "people":
        return False
    return "残業" in (content or "")


def cell_number(kind, content, value):
    """作業人員セルの数値化（アプリの cellNumber と同じ）。残業行は「1.5H」から1.5、人員行は数値のみ"""
    s = to_half_num_str(value)
    if s == "":
        return 0.0
    n = js_parse_float(s) if is_overtime_row(kind, content) else js_number(s)
    if math.isnan(n) or math.isinf(n):
        return 0.0
    return n


def num_or_zero(v):
    n = js_parse_float(v)
    return 0.0 if (math.isnan(n) or math.isinf(n)) else n   # parseFloat(x)||0


def js_round(x):
    return math.floor(x + 0.5)                               # Math.round（正の値）


def round2(x):
    return js_round(x * 100) / 100


def get_tax_rate(inv, tax_rates):
    """請求書の税率（税率ID→なければ発行日時点の標準税率→なければ最も古い税率）"""
    for t in tax_rates:
        if t.get("id") == inv.get("taxRateId"):
            return float(t["rate"])
    date = inv.get("date") or "9999-12-31"
    ordered = sorted(tax_rates, key=lambda t: t["effectiveFrom"], reverse=True)
    for t in ordered:
        if t["effectiveFrom"] <= date:
            return float(t["rate"])
    return float(ordered[-1]["rate"]) if ordered else 10.0


def invoice_financials(inv, tax_rates):
    items = sum(num_or_zero(it.get("qty")) * num_or_zero(it.get("price")) for it in inv.get("items", []))
    exps = sum(num_or_zero(e.get("amount")) for e in inv.get("expenses", []))
    subtotal = items + exps
    rate = get_tax_rate(inv, tax_rates)
    tax = (-1 if subtotal < 0 else 1) * js_round(abs(subtotal) * (rate / 100))
    grand = subtotal + tax
    paid = sum(num_or_zero(p.get("amount")) for p in inv.get("payments", []))
    return {"subtotal": subtotal, "tax": tax, "grand": grand, "paid": paid, "remaining": max(0, grand - paid)}


def sha256_hex(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def audit_hash(prev, seq, time, action):
    return sha256_hex(f"{prev}|{seq}|{time}|{action}")


def verify_audit_chain(entries):
    """正常なら None、不整合があれば最初に壊れた seq を返す"""
    prev = GENESIS
    for i, e in enumerate(entries):
        if e.get("seq") != i + 1 or e.get("prev") != prev or e.get("hash") != audit_hash(prev, e.get("seq"), e.get("time"), e.get("action")):
            return e.get("seq", i + 1)
        prev = e["hash"]
    return None


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def invoice_content_hash(inv):
    """発行時の内容ハッシュ（アプリの invoiceContentHash と同じ）"""
    obj = {
        "no": inv.get("no") or "", "date": inv.get("date") or "",
        "clientId": inv.get("clientId") or "", "taxRateId": inv.get("taxRateId") or "",
        "items": [[it.get("site") or "", it.get("content") or "", js_str_or_empty(it.get("qty")),
                   it.get("unit") or "", js_str_or_empty(it.get("price"))] for it in inv.get("items", [])],
        "expenses": [[e.get("name") or "", js_str_or_empty(e.get("amount"))] for e in inv.get("expenses", [])],
        "creditOf": inv.get("creditOf") or "",
    }
    return sha256_hex(canonical_json(obj))


# =====================================================================
#  読み込み・事前チェック
# =====================================================================
class MigrationError(Exception):
    pass


# =====================================================================
#  暗号化（SQLCipher）：鍵・復旧コード・接続
#   鍵は「ランダムな256ビット」を直接使う（x'64桁の16進'）。パスフレーズと違って総当たりの対象にならず、
#   開くたびの鍵導出（PBKDF2）も不要なので速い。鍵はアプリがWindowsのユーザー資格情報（DPAPI）で保護して保管する。
# =====================================================================
def generate_key_hex():
    return secrets.token_hex(32)


def normalize_key(key_hex):
    k = (key_hex or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", k):
        raise MigrationError("暗号鍵は64桁の16進数（256ビット）で指定してください。")
    return k


# --- 復旧コードの形式（55文字） ---------------------------------------------------------------
#   鍵（256ビット）＋ ハッシュの先頭14ビット ＝ 270ビット ＝ 54文字（Base32）、さらに末尾に「検査文字」1文字 ＝ 55文字。
#   検査文字は Luhn mod 32 方式：1文字の書き間違いは、**必ず**検出できる（数学的に保証）。
#   ハッシュ14ビットは、2文字以上の誤り・取り違えを検出する（見逃しは約1/16,000×1/32＝約52万分の1）。
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _luhn32_generate(vals):
    factor, total = 2, 0
    for v in reversed(vals):
        a = factor * v
        factor = 1 if factor == 2 else 2
        total += a // 32 + a % 32
    return (32 - total % 32) % 32


def _luhn32_valid(vals):
    factor, total = 1, 0
    for v in reversed(vals):
        a = factor * v
        factor = 2 if factor == 1 else 1
        total += a // 32 + a % 32
    return total % 32 == 0


def _hash14(raw):
    return int.from_bytes(hashlib.sha256(raw).digest()[:2], "big") >> 2


def recovery_code(key_hex):
    """鍵を紙に控えるための復旧コード（55文字。5文字ずつ「-」で区切る）"""
    raw = bytes.fromhex(normalize_key(key_hex))
    n = (int.from_bytes(raw, "big") << 14) | _hash14(raw)           # 270ビット
    vals = [(n >> (5 * (53 - i))) & 31 for i in range(54)]
    vals.append(_luhn32_generate(vals))
    s = "".join(_B32[v] for v in vals)
    return "-".join(s[i:i + 5] for i in range(0, 55, 5))


def _decode_recovery(s):
    vals = [_B32.find(ch) for ch in s]
    if len(vals) != 55 or min(vals) < 0 or not _luhn32_valid(vals):
        raise ValueError("check-char")
    n = 0
    for v in vals[:54]:
        n = (n << 5) | v
    raw = (n >> 14).to_bytes(32, "big")
    if (n & 0x3FFF) != _hash14(raw):
        raise ValueError("hash")
    return raw.hex()


def parse_recovery_code(code):
    """復旧コードから鍵を復元する。大小文字・空白・ハイフンは無視。
    手書きの取り違えに備え、このコードでは使われない数字を文字として読み替える（0→O、8→B、1→I または L）。
    読み替えが正しいかは、検査文字とハッシュで確認する。1文字の書き間違いは必ずエラーになる。
    1が I か L か判別できない（両方とも検査を通る）場合は、推測せずエラーにする"""
    s = "".join(str(code).upper().split()).replace("-", "").replace("0", "O").replace("8", "B")
    if s.count("1") > 4:
        raise MigrationError("復旧コードの形式が正しくありません。")
    variants = [s]
    for _ in range(s.count("1")):                          # 1 は I か L のどちらか（先頭から順に決める）
        variants = [v.replace("1", c, 1) for v in variants for c in "IL"]
    found = set()
    for v in variants:
        try:
            found.add(_decode_recovery(v))
        except Exception:
            continue
    if len(found) == 1:
        return found.pop()
    if len(found) > 1:                                      # 推測で選ばない（誤った鍵を黙って受け付けない）
        raise MigrationError("1 が I と L のどちらか判別できません。控えを見て、I または L の文字で入力してください。")
    if len("".join(str(code).split()).replace("-", "")) != 55:
        raise MigrationError("復旧コードの文字数が違います（全55文字）。")
    raise MigrationError("復旧コードが正しくありません（入力ミスの可能性があります。控えと見比べてください）。")


def _h(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def recovery_sheet_html(key_hex, label=""):
    """復旧コードの「紙の控え」（印刷用HTML）。鍵そのもの（16進）は載せず、復旧コードだけを載せる"""
    code = recovery_code(key_hex)
    groups = code.split("-")
    rows = ["　".join(groups[i:i + 4]) for i in range(0, len(groups), 4)]           # 4グループずつ改行（読み書きしやすい）
    label_html = f"<tr><th>対象</th><td>{_h(label)}</td></tr>" if label else ""
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8"><title>復旧コード（控え）</title>
<style>
@page{{size:A4;margin:18mm;}}
body{{font-family:"Yu Gothic","Hiragino Kaku Gothic ProN",sans-serif;color:#111;max-width:720px;margin:0 auto;padding:16px;line-height:1.7;}}
h1{{font-size:20px;border-bottom:3px solid #222;padding-bottom:6px;}}
.code{{font-family:"Consolas","Courier New",monospace;font-size:22px;letter-spacing:2px;border:2px solid #222;padding:14px 16px;margin:14px 0;line-height:2.0;}}
.warn{{border:2px solid #b5453f;background:#fdf0ee;padding:10px 14px;margin:14px 0;font-size:14px;}}
table{{border-collapse:collapse;width:100%;margin:10px 0;}} th,td{{border:1px solid #444;padding:8px 10px;text-align:left;}} th{{width:26%;background:#eee;font-weight:normal;}}
ul{{margin:6px 0 6px 1.2em;padding:0;}} .box{{display:inline-block;width:14px;height:14px;border:2px solid #222;margin-right:8px;vertical-align:-2px;}}
</style></head><body>
<h1>データ暗号化の復旧コード（控え）</h1>
<p>このコードは、データの暗号化を解く「鍵」と同じ意味を持ちます。PCが壊れたときや、Windowsの再インストール後に、データを取り戻すために使います。</p>
<div class="code">{"<br>".join(_h(r) for r in rows)}</div>
<p style="font-size:13px;">アルファベット大文字と数字（2〜7）の全55文字です。ハイフンと区切りの空白は、入力しなくても構いません。<br>このコードには数字の 0・1・8・9 は使われません。書き写すときに迷ったら、0 は O（オー）、1 は I または L、8 は B と読んでください（入力時はそのまま 0・1・8 と打っても読み替えます）。</p>
<div class="warn"><b>取り扱いの注意</b>
<ul>
<li>このコードを知っている人は、データを開けます。<b>PCとは別の場所（金庫・鍵のかかる引き出しなど）</b>に保管してください。</li>
<li>写真に撮る、メールで送る、クラウドやチャットに保存する、ことはしないでください。</li>
<li>このコードを失い、PC側の鍵も使えなくなったときは、データを取り戻せません。</li>
<li>鍵を変更した場合は、古い控えを破棄し、新しい控えを作り直してください。</li>
<li>印刷が終わったら、この画面（ファイル）を閉じて削除してください。</li>
</ul></div>
<table>
{label_html}<tr><th>作成日</th><td>　　　　年　　月　　日</td></tr>
<tr><th>保管場所</th><td>&nbsp;</td></tr>
<tr><th>保管者</th><td>&nbsp;</td></tr>
</table>
<p><span class="box"></span>紙に印刷した　　<span class="box"></span>PCとは別の場所に保管した　　<span class="box"></span>コードの再入力による確認を行った</p>
</body></html>"""


def read_key_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return normalize_key(f.read())


def _driver(key_hex):
    if key_hex:
        if _sqlcipher is None:
            raise MigrationError("暗号化にはSQLCipher（pip install sqlcipher3-wheels）が必要です。")
        return _sqlcipher
    return sqlite3


def _row_factory(conn):
    return _sqlcipher.Row if (_sqlcipher is not None and isinstance(conn, _sqlcipher.Connection)) else sqlite3.Row


def _win_sqlcipher_blocked():
    """Windowsでは、Pythonの暗号化（sqlcipher3-wheels）を使わない。
    GitHubのWindows環境で、暗号化DBにスキーマを作る処理がクラッシュする（作業領域を128MBに広げても再現。ライブラリ側の問題と判断）。
    暗号化DBは、Windowsで動作を確認済みのRust側（ibis-core）で扱う。検証のために試す場合だけ、環境変数 IBIS_ALLOW_WIN_SQLCIPHER=1 で解除できる"""
    return _win_sqlcipher_policy(os.name, os.environ)


def _win_sqlcipher_policy(os_name, env):
    """判定の規則（テストで、環境に関係なく確かめられるように分けている）"""
    return os_name == "nt" and env.get("IBIS_ALLOW_WIN_SQLCIPHER") != "1"


def open_db(path, key_hex=None, isolation_level=""):
    """DBを開く。鍵があればSQLCipherで復号する（鍵が違う・暗号化DBに鍵なし・平文DBに鍵ありは、ここで失敗する）"""
    key = normalize_key(key_hex) if key_hex else None
    if key and _win_sqlcipher_blocked():
        raise MigrationError("Windowsでは、このツールで暗号化DBを扱えません（Windows版の暗号化ライブラリが不安定なため）。"
                             "暗号化DBの作成・確認は、アプリ（Rust）で行ってください。暗号化なしの移行・確認は、このまま使えます。")
    drv = _driver(key)
    conn = drv.connect(path, isolation_level=isolation_level)
    try:
        if key:
            conn.execute(f"PRAGMA key = \"x'{key}'\"")
            conn.execute("PRAGMA cipher_memory_security = ON")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA temp_store = MEMORY")        # 一時ファイルに平文を残さない
        conn.execute("PRAGMA secure_delete = ON")         # 削除した内容をDBファイル内でも消去する
    except Exception as e:
        conn.close()
        raise MigrationError("DBを開けません（鍵が違う、またはDBが壊れています）：" + str(e))
    return conn


def load_backup(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise MigrationError(f"JSONとして読み込めません：{e}")
    meta = {"source_name": os.path.basename(path), "source_sha256": hashlib.sha256(raw).hexdigest()}
    if isinstance(obj, dict) and obj.get("format") == "integratedBizSystem":
        data = obj.get("data")
        if not isinstance(data, dict):
            raise MigrationError("バックアップ本体（data）がありません。")
        if obj.get("checksum") and sha256_hex(canonical_json(data)) != obj["checksum"]:
            raise MigrationError("チェックサムが一致しません。ファイルが破損している、または編集された可能性があります。")
        meta.update(app_version=obj.get("appVersion", ""), exported_at=obj.get("exportedAt", ""), source_checksum=obj.get("checksum", ""))
    else:
        data = obj
        meta.update(app_version="", exported_at="", source_checksum="")
    if not isinstance(data, dict):
        raise MigrationError("データ形式が正しくありません。")
    return data, meta


def precheck(data):
    """移行できない問題（errors）と、注意すべき点（warnings）を返す"""
    errors, warnings = [], []
    isd = lambda v: isinstance(v, dict)
    ver = data.get("schemaVersion")
    if ver != 2:
        errors.append(f"データ形式が v{ver} です。先にアプリで読み込み、最新形式（v2）で書き出し直してください。")
        return errors, warnings
    for k in ("settings", "manHours", "invoices", "siteCosts", "closed"):
        if not isd(data.get(k)):
            errors.append(f"{k} がオブジェクトではありません。")
    for k in ("labels", "auditLog"):
        if not isinstance(data.get(k), list):
            errors.append(f"{k} が配列ではありません。")
    if errors:
        return errors, warnings
    s = data["settings"]
    if not s.get("taxRates"):
        errors.append("消費税率マスタが空です。")
    client_ids = {c.get("id") for c in s.get("clients", [])}
    tax_ids = {t.get("id") for t in s.get("taxRates", [])}
    if len(client_ids) != len(s.get("clients", [])):
        errors.append("取引先のIDが重複しています。")
    seen_no, inv_ids = {}, set()
    for month, lst in data["invoices"].items():
        if not MONTH_RE.match(month):
            errors.append(f"請求書の月キーが不正です：{month}")
            continue
        for inv in lst:
            if not inv.get("id"):
                errors.append(f"請求書にIDがありません（{month}）。")
                continue
            if inv["id"] in inv_ids:
                errors.append(f"請求書IDが重複しています：{inv['id']}")
            inv_ids.add(inv["id"])
            if inv.get("status") not in STATUSES:
                errors.append(f"請求書 {inv.get('no')} の状態が不正です：{inv.get('status')}")
            no = inv.get("no") or ""
            if no:
                if no in seen_no:
                    errors.append(f"伝票No.が重複しています：{no}")
                seen_no[no] = True
            cid = inv.get("clientId") or ""
            if cid and cid not in client_ids:
                warnings.append(f"請求書 {no or inv['id']}：取引先ID（{cid}）がマスタにありません。退避して保存します。")
            tid = inv.get("taxRateId") or ""
            if tid and tid not in tax_ids:
                warnings.append(f"請求書 {no or inv['id']}：税率ID（{tid}）がマスタにありません。退避して保存します。")
    for month, lst in data["invoices"].items():
        for inv in lst:
            c = inv.get("creditOf")
            if c and c not in inv_ids:
                errors.append(f"赤伝 {inv.get('no')} の取消対象（{c}）が存在しません。")
    for m in data["manHours"]:
        if not MONTH_RE.match(m):
            errors.append(f"作業人員の月キーが不正です：{m}")
    for kind in ("manHours", "invoices"):
        for m, v in (data["closed"].get(kind) or {}).items():
            if v and not MONTH_RE.match(m):
                errors.append(f"締めの月キーが不正です：{m}")
    bad = verify_audit_chain(data["auditLog"])
    if bad is not None:
        errors.append(f"操作履歴ログの連鎖が No.{bad} で壊れています（改ざん・欠落の可能性）。調査してから移行してください。")
    for month, lst in data["invoices"].items():
        for inv in lst:
            if inv.get("issued") and inv.get("issueHash") and invoice_content_hash(inv) != inv["issueHash"]:
                warnings.append(f"発行済み請求書 {inv.get('no')} の内容が、発行時のハッシュと一致しません。")
    return errors, warnings


# =====================================================================
#  変換（JSON → SQLite）
# =====================================================================
def _sv(v):
    """保存用の文字列（None はNULL、数値は文字列へ）"""
    return None if v is None else js_string(v)


def _extra(d, known):
    ex = {k: v for k, v in d.items() if k not in known}
    return json.dumps(ex, ensure_ascii=False) if ex else None


def migrate(src, dst, overwrite=False, key_hex=None):
    data, meta = load_backup(src)
    errors, warnings = precheck(data)
    if errors:
        raise MigrationError("移行できません：\n・" + "\n・".join(errors))
    if os.path.exists(dst):
        if not overwrite:
            raise MigrationError(f"{dst} は既に存在します（--overwrite で上書き）。")
    tmp = dst + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = open_db(tmp, key_hex, isolation_level=None)      # 暗号化する場合は、最初の書き込みの前に鍵を設定する
    report = {"counts": {}, "warnings": list(warnings), "dropped": {"items.amount": 0, "rows.sub": 0}, "encrypted": bool(key_hex)}
    try:
        with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN")
        conn.execute("PRAGMA defer_foreign_keys=ON")
        _insert_all(conn, data, meta, report)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    # 自己検証（不一致があれば失敗）
    conn.close()
    problems = verify_migration(data, tmp, key_hex)
    if problems:
        os.remove(tmp)
        raise MigrationError("移行後の検証で不一致が見つかりました：\n・" + "\n・".join(problems))
    if os.path.exists(dst):
        os.remove(dst)
    os.replace(tmp, dst)
    return report


def _insert_all(conn, data, meta, report):
    s = data["settings"]
    cnt = report["counts"]
    ex = conn.execute

    # --- メタ ---
    now = datetime.now(timezone.utc).isoformat()
    last = data["auditLog"][-1] if data["auditLog"] else None
    for k, v in {
        "db_schema_version": "1", "data_schema_version": "2", "created_at": now, "tool_version": TOOL_VERSION,
        "source_name": meta["source_name"], "source_sha256": meta["source_sha256"],
        "source_app_version": meta.get("app_version", ""), "source_exported_at": meta.get("exported_at", ""),
        "source_checksum": meta.get("source_checksum", ""),
        "audit_head_seq": str(last["seq"]) if last else "0", "audit_head_hash": last["hash"] if last else GENESIS,
    }.items():
        ex("INSERT INTO meta(key,value) VALUES(?,?)", (k, v))
    for h in data.get("migrationHistory", []):
        ex("INSERT INTO migration_history(at,from_version,to_version,steps_json,warnings) VALUES(?,?,?,?,?)",
           (h.get("at", ""), h.get("from", 1), h.get("to", 2), json.dumps(h.get("steps", []), ensure_ascii=False), h.get("warnings", 0)))
    ex("INSERT INTO migration_history(at,from_version,to_version,steps_json,warnings) VALUES(?,?,?,?,?)",
       (now, 2, 2, json.dumps([MIGRATION_ROW_STEP], ensure_ascii=False), len(report["warnings"])))

    # --- 自社情報 ---
    c = s.get("company") or {}
    ex("INSERT INTO company(id,name,rep,zip,addr,tel,fax,reg_no,extra) VALUES(1,?,?,?,?,?,?,?,?)",
       (c.get("name"), c.get("rep"), c.get("zip"), c.get("addr"), c.get("tel"), c.get("fax"), c.get("reg"),
        _extra(c, {"name", "rep", "zip", "addr", "tel", "fax", "reg"})))

    # --- 設定（表にしないもの） ---
    handled = {"company", "taxRates", "clients", "sites", "expenseItems", "siteBudgets", "invoiceSeqByMonth", "invoiceTemplates", "transitionalRates"}
    for k, v in s.items():
        if k not in handled:
            ex("INSERT INTO app_settings(key,value_json) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
    for k, v in data.items():
        if k not in {"settings", "schemaVersion", "migrationHistory", "manHours", "invoices", "labels", "siteCosts", "closed", "auditLog"}:
            ex("INSERT INTO app_settings(key,value_json) VALUES(?,?)", ("_top:" + k, json.dumps(v, ensure_ascii=False)))
    orphans = {}

    # --- 税率・経過措置 ---
    for i, t in enumerate(s["taxRates"]):
        ex("INSERT INTO tax_rates(id,sort_order,label,rate,effective_from,extra) VALUES(?,?,?,?,?,?)",
           (t["id"], i, t.get("label"), t["rate"], t["effectiveFrom"], _extra(t, {"id", "label", "rate", "effectiveFrom"})))
    for i, t in enumerate(s.get("transitionalRates", [])):
        ex("INSERT INTO transitional_rates(sort_order,effective_from,rate) VALUES(?,?,?)", (i, t.get("from"), t.get("rate")))
    cnt["tax_rates"] = len(s["taxRates"])

    # --- 取引先・テンプレート ---
    cmap = {"closingDay": "closing_day", "paymentOffsetMonths": "payment_offset_months", "paymentDay": "payment_day",
            "creditLimit": "credit_limit", "zip": "zip", "addr1": "addr1", "addr2": "addr2", "contactName": "contact_name",
            "honorific": "honorific", "tel": "tel", "fax": "fax", "email": "email", "registrationNo": "registration_no"}
    for i, cl in enumerate(s.get("clients", [])):
        cols = ["id", "sort_order", "name"] + list(cmap.values()) + ["extra"]
        vals = [cl["id"], i, cl.get("name", "")] + [cl.get(k) for k in cmap] + [_extra(cl, {"id", "name"} | set(cmap))]
        ex(f"INSERT INTO clients({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
    client_ids = {c["id"] for c in s.get("clients", [])}
    cnt["clients"] = len(client_ids)
    for i, n in enumerate(s.get("sites", [])):
        ex("INSERT INTO sites(sort_order,name) VALUES(?,?)", (i, n))
    for i, n in enumerate(s.get("expenseItems", [])):
        ex("INSERT INTO expense_items(sort_order,name) VALUES(?,?)", (i, n))
    for n, d in (s.get("siteBudgets") or {}).items():
        ex("INSERT INTO site_budgets(site_name,planned_days) VALUES(?,?)", (n, d))
    for m, n in (s.get("invoiceSeqByMonth") or {}).items():
        ex("INSERT INTO invoice_seq(month,last_seq) VALUES(?,?)", (m, n))
    for cid, tpl in (s.get("invoiceTemplates") or {}).items():
        if cid not in client_ids:
            orphans.setdefault("invoiceTemplates", {})[cid] = tpl
            report["warnings"].append(f"請求書ひな形：取引先ID（{cid}）がマスタにありません。退避して保存します。")
            continue
        ex("INSERT INTO invoice_templates(client_id) VALUES(?)", (cid,))
        for j, it in enumerate(tpl.get("items", [])):
            ex("INSERT INTO invoice_template_items VALUES(?,?,?,?,?,?,?)",
               (cid, j + 1, it.get("site"), it.get("content"), _sv(it.get("qty")), it.get("unit"), _sv(it.get("price"))))
        for j, e in enumerate(tpl.get("expenses", [])):
            ex("INSERT INTO invoice_template_expenses VALUES(?,?,?,?)", (cid, j + 1, e.get("name"), _sv(e.get("amount"))))

    # --- 作業人員 ---
    n_rows = n_cells = 0
    for month, m in data["manHours"].items():
        ex("INSERT INTO man_hour_months(month,carry,ot_carry,extra) VALUES(?,?,?,?)",
           (month, m.get("carry", 0), m.get("otCarry"), _extra(m, {"rows", "expenses", "carry", "otCarry"})))
        for j, r in enumerate(m.get("rows", [])):
            cur = ex("INSERT INTO man_hour_rows(month,line_no,content,kind,extra) VALUES(?,?,?,?,?)",
                     (month, j + 1, r.get("content", ""), r.get("kind"), _extra(r, {"content", "daily", "sub", "kind"})))
            rid = cur.lastrowid
            n_rows += 1
            if "sub" in r:
                report["dropped"]["rows.sub"] += 1
            for k, v in (r.get("daily") or {}).items():
                ex("INSERT INTO man_hour_cells(row_id,seq,value,num_value) VALUES(?,?,?,?)",
                   (rid, int(k), js_string(v), cell_number(r.get("kind"), r.get("content"), v)))
                n_cells += 1
        for j, e in enumerate(m.get("expenses", [])):
            ex("INSERT INTO man_hour_expenses VALUES(?,?,?,?)", (month, j + 1, e.get("name"), _sv(e.get("amount"))))
    cnt["man_hour_months"], cnt["man_hour_rows"], cnt["man_hour_cells"] = len(data["manHours"]), n_rows, n_cells

    # --- 請求書（発行済み・取消の印は、明細を入れた後で付ける：改変防止トリガーのため2段階） ---
    tax_ids = {t["id"] for t in s["taxRates"]}
    phase2, n_inv, n_items, n_exp, n_pay = [], 0, 0, 0, 0
    known = {"id", "clientId", "date", "no", "taxRateId", "status", "items", "expenses", "payments", "issued", "issueHash",
             "voided", "creditOf", "creditOfNo", "creditOfDate", "reason"}
    for month, lst in data["invoices"].items():
        for seq, inv in enumerate(lst):
            extra = {k: v for k, v in inv.items() if k not in known}
            cid, tid = inv.get("clientId") or "", inv.get("taxRateId") or ""
            if cid and cid not in client_ids:
                extra["orphanClientId"] = cid
                cid = None
            if tid and tid not in tax_ids:
                extra["orphanTaxRateId"] = tid
                tid = None
            ex("""INSERT INTO invoices(id,month,seq,no,client_id,issue_date,tax_rate_id,status,credit_of_id,credit_of_no,
                                      credit_of_date,credit_reason,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (inv["id"], month, seq, inv.get("no") or "", cid or None, inv.get("date"), tid or None, inv["status"],
                inv.get("creditOf"), inv.get("creditOfNo"), inv.get("creditOfDate"), inv.get("reason"),
                json.dumps(extra, ensure_ascii=False) if extra else None))
            n_inv += 1
            for j, it in enumerate(inv.get("items", [])):
                if "amount" in it:
                    report["dropped"]["items.amount"] += 1
                ex("INSERT INTO invoice_items(invoice_id,line_no,site,content,qty,unit,price) VALUES(?,?,?,?,?,?,?)",
                   (inv["id"], j + 1, it.get("site"), it.get("content"), _sv(it.get("qty")), it.get("unit"), _sv(it.get("price"))))
                n_items += 1
            for j, e in enumerate(inv.get("expenses", [])):
                ex("INSERT INTO invoice_expenses(invoice_id,line_no,name,amount) VALUES(?,?,?,?)", (inv["id"], j + 1, e.get("name"), _sv(e.get("amount"))))
                n_exp += 1
            for j, p in enumerate(inv.get("payments", [])):
                ex("INSERT INTO payments(invoice_id,line_no,pay_date,amount) VALUES(?,?,?,?)", (inv["id"], j + 1, p.get("date"), _sv(p.get("amount"))))
                n_pay += 1
            v = inv.get("voided") or {}
            if inv.get("issued") or v:
                phase2.append((inv.get("issued"), inv.get("issueHash"), v.get("at"), v.get("reason"), v.get("creditNo"), inv["id"]))
    for row in phase2:
        ex("UPDATE invoices SET issued_at=?, issue_hash=?, voided_at=?, void_reason=?, void_credit_no=? WHERE id=?", row)
    cnt.update(invoices=n_inv, invoice_items=n_items, invoice_expenses=n_exp, payments=n_pay)

    # --- 宛名ラベル・原価・締め・ログ（締めは最後：トリガーが以後の変更を止めるため） ---
    for i, l in enumerate(data["labels"]):
        ex("INSERT INTO labels(id,sort_order,zip,addr1,addr2,name,honorific,extra) VALUES(?,?,?,?,?,?,?,?)",
           (l["id"], i, l.get("zip"), l.get("addr1"), l.get("addr2"), l.get("name"), l.get("honorific"),
            _extra(l, {"id", "zip", "addr1", "addr2", "name", "honorific"})))
    n_cost = 0
    for site, lst in data["siteCosts"].items():
        for i, cst in enumerate(lst):
            ex("INSERT INTO site_costs(id,site_name,sort_order,cost_date,category,amount,memo) VALUES(?,?,?,?,?,?,?)",
               (cst["id"], site, i, cst.get("date"), cst.get("category"), _sv(cst.get("amount")), cst.get("memo")))
            n_cost += 1
    for e in data["auditLog"]:
        ex("INSERT INTO audit_log(seq,time,action,prev_hash,hash) VALUES(?,?,?,?,?)", (e["seq"], e["time"], e["action"], e["prev"], e["hash"]))
    n_closed = 0
    for kind in ("manHours", "invoices"):
        for m, v in (data["closed"].get(kind) or {}).items():
            if v:
                ex("INSERT INTO closed_months(kind,month) VALUES(?,?)", (kind, m))
                n_closed += 1
    if orphans:
        ex("INSERT INTO app_settings(key,value_json) VALUES('_orphans',?)", (json.dumps(orphans, ensure_ascii=False),))
    cnt.update(labels=len(data["labels"]), site_costs=n_cost, audit_log=len(data["auditLog"]), closed_months=n_closed)


# =====================================================================
#  書き戻し（SQLite → JSON）
# =====================================================================
def _num(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _put(d, k, v):
    if v is not None:
        d[k] = v


def export_sqlite_to_json(db, key_hex=None):
    is_conn = isinstance(db, sqlite3.Connection) or (_sqlcipher is not None and isinstance(db, _sqlcipher.Connection))
    conn = db if is_conn else open_db(db, key_hex)
    conn.row_factory = _row_factory(conn)
    q = lambda sql, *a: conn.execute(sql, a).fetchall()
    data = {}
    S = {}
    c = q("SELECT * FROM company")[0]
    comp = {}
    for col, key in (("name", "name"), ("rep", "rep"), ("zip", "zip"), ("addr", "addr"), ("tel", "tel"), ("fax", "fax"), ("reg_no", "reg")):
        _put(comp, key, c[col])
    if c["extra"]:
        comp.update(json.loads(c["extra"]))
    S["company"] = comp
    S["taxRates"] = []
    for r in q("SELECT * FROM tax_rates ORDER BY sort_order"):
        t = {"id": r["id"], "rate": _num(r["rate"]), "effectiveFrom": r["effective_from"]}
        _put(t, "label", r["label"])
        if r["extra"]:
            t.update(json.loads(r["extra"]))
        S["taxRates"].append(t)
    cmap = {"closing_day": "closingDay", "payment_offset_months": "paymentOffsetMonths", "payment_day": "paymentDay", "credit_limit": "creditLimit",
            "zip": "zip", "addr1": "addr1", "addr2": "addr2", "contact_name": "contactName", "honorific": "honorific", "tel": "tel",
            "fax": "fax", "email": "email", "registration_no": "registrationNo"}
    S["clients"] = []
    for r in q("SELECT * FROM clients ORDER BY sort_order"):
        cl = {"id": r["id"], "name": r["name"]}
        for col, key in cmap.items():
            _put(cl, key, _num(r[col]))
        if r["extra"]:
            cl.update(json.loads(r["extra"]))
        S["clients"].append(cl)
    S["sites"] = [r["name"] for r in q("SELECT name FROM sites ORDER BY sort_order")]
    S["expenseItems"] = [r["name"] for r in q("SELECT name FROM expense_items ORDER BY sort_order")]
    S["siteBudgets"] = {r["site_name"]: _num(r["planned_days"]) for r in q("SELECT * FROM site_budgets")}
    S["invoiceSeqByMonth"] = {r["month"]: r["last_seq"] for r in q("SELECT * FROM invoice_seq")}
    S["transitionalRates"] = [{"from": r["effective_from"], "rate": _num(r["rate"])} for r in q("SELECT * FROM transitional_rates ORDER BY sort_order")]
    tpls = {}
    for r in q("SELECT client_id FROM invoice_templates"):
        cid = r["client_id"]
        tpls[cid] = {"items": [], "expenses": []}
        for it in q("SELECT * FROM invoice_template_items WHERE client_id=? ORDER BY line_no", cid):
            o = {}
            for col in ("site", "content", "qty", "unit", "price"):
                _put(o, col, it[col])
            tpls[cid]["items"].append(o)
        for e in q("SELECT * FROM invoice_template_expenses WHERE client_id=? ORDER BY line_no", cid):
            o = {}
            _put(o, "name", e["name"]); _put(o, "amount", e["amount"])
            tpls[cid]["expenses"].append(o)
    orphans = {}
    top = {}
    for r in q("SELECT * FROM app_settings"):
        if r["key"] == "_orphans":
            orphans = json.loads(r["value_json"])
        elif r["key"].startswith("_top:"):
            top[r["key"][5:]] = json.loads(r["value_json"])
        else:
            S[r["key"]] = json.loads(r["value_json"])
    tpls.update(orphans.get("invoiceTemplates", {}))
    S["invoiceTemplates"] = tpls
    data["settings"] = S
    data["schemaVersion"] = 2
    data["migrationHistory"] = []
    for r in q("SELECT * FROM migration_history ORDER BY id"):
        steps = json.loads(r["steps_json"])
        if steps == [MIGRATION_ROW_STEP]:
            continue
        data["migrationHistory"].append({"at": r["at"], "from": r["from_version"], "to": r["to_version"], "steps": steps, "warnings": r["warnings"]})

    # 作業人員
    mh = {}
    for m in q("SELECT * FROM man_hour_months ORDER BY month"):
        month = {"carry": _num(m["carry"])}
        _put(month, "otCarry", _num(m["ot_carry"]))
        rows = []
        for r in q("SELECT * FROM man_hour_rows WHERE month=? ORDER BY line_no", m["month"]):
            daily = {str(c["seq"]): c["value"] for c in q("SELECT * FROM man_hour_cells WHERE row_id=? ORDER BY seq", r["id"])}
            total = round2(sum(c["num_value"] for c in q("SELECT num_value FROM man_hour_cells WHERE row_id=?", r["id"])))
            row = {"content": r["content"], "daily": daily, "sub": _num(total)}
            _put(row, "kind", r["kind"])
            if r["extra"]:
                row.update(json.loads(r["extra"]))
            rows.append(row)
        month["rows"] = rows
        month["expenses"] = [{k: e[k] for k in ("name", "amount") if e[k] is not None} for e in q("SELECT * FROM man_hour_expenses WHERE month=? ORDER BY line_no", m["month"])]
        if m["extra"]:
            month.update(json.loads(m["extra"]))
        mh[m["month"]] = month
    data["manHours"] = mh

    # 請求書
    invs = {}
    for r in q("SELECT * FROM invoices ORDER BY month, seq"):
        ex = json.loads(r["extra"]) if r["extra"] else {}
        inv = {"id": r["id"], "clientId": r["client_id"] or ex.pop("orphanClientId", ""), "date": r["issue_date"],
               "no": r["no"], "taxRateId": r["tax_rate_id"] or ex.pop("orphanTaxRateId", ""), "status": r["status"]}
        ex.pop("orphanClientId", None); ex.pop("orphanTaxRateId", None)
        inv["items"] = []
        for it in q("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY line_no", r["id"]):
            o = {}
            for col in ("site", "content", "qty", "unit", "price"):
                _put(o, col, it[col])
            inv["items"].append(o)
        inv["expenses"] = [{k: e[k] for k in ("name", "amount") if e[k] is not None} for e in q("SELECT * FROM invoice_expenses WHERE invoice_id=? ORDER BY line_no", r["id"])]
        inv["payments"] = []
        for p in q("SELECT * FROM payments WHERE invoice_id=? ORDER BY line_no", r["id"]):
            o = {}
            _put(o, "date", p["pay_date"]); _put(o, "amount", p["amount"])
            inv["payments"].append(o)
        _put(inv, "issued", r["issued_at"]); _put(inv, "issueHash", r["issue_hash"])
        if r["voided_at"] is not None or r["void_reason"] is not None or r["void_credit_no"] is not None:
            v = {}
            _put(v, "at", r["voided_at"]); _put(v, "reason", r["void_reason"]); _put(v, "creditNo", r["void_credit_no"])
            inv["voided"] = v
        _put(inv, "creditOf", r["credit_of_id"]); _put(inv, "creditOfNo", r["credit_of_no"])
        _put(inv, "creditOfDate", r["credit_of_date"]); _put(inv, "reason", r["credit_reason"])
        inv.update(ex)
        invs.setdefault(r["month"], []).append(inv)
    data["invoices"] = invs
    data["labels"] = []
    for r in q("SELECT * FROM labels ORDER BY sort_order"):
        l = {"id": r["id"]}
        for col in ("zip", "addr1", "addr2", "name", "honorific"):
            _put(l, col, r[col])
        if r["extra"]:
            l.update(json.loads(r["extra"]))
        data["labels"].append(l)
    sc = {}
    for r in q("SELECT * FROM site_costs ORDER BY site_name, sort_order"):
        o = {"id": r["id"]}
        _put(o, "date", r["cost_date"]); _put(o, "category", r["category"]); _put(o, "amount", r["amount"]); _put(o, "memo", r["memo"])
        sc.setdefault(r["site_name"], []).append(o)
    data["siteCosts"] = sc
    closed = {"manHours": {}, "invoices": {}}
    for r in q("SELECT * FROM closed_months"):
        closed[r["kind"]][r["month"]] = True
    data["closed"] = closed
    data["auditLog"] = [{"seq": r["seq"], "time": r["time"], "action": r["action"], "prev": r["prev_hash"], "hash": r["hash"]} for r in q("SELECT * FROM audit_log ORDER BY seq")]
    data.update(top)
    return data


# =====================================================================
#  比較用の正規化／移行後の自己検証
# =====================================================================
def normalize(data):
    """DBに保存しない項目（導出値）や表記ゆれを揃える。比較専用"""
    d = copy.deepcopy(data)
    strify = lambda v: None if v is None else js_string(v)

    def strip_none(o):
        if isinstance(o, dict):
            return {k: strip_none(v) for k, v in o.items() if v is not None}
        if isinstance(o, list):
            return [strip_none(x) for x in o]
        return _num(o)

    for month, m in d["manHours"].items():
        for r in m["rows"]:
            r.pop("sub", None)
            r["daily"] = {str(k): ("" if v is None else js_string(v)) for k, v in (r.get("daily") or {}).items()}
        for e in m.get("expenses", []):
            if "amount" in e:
                e["amount"] = strify(e["amount"])
    for month in list(d["invoices"]):
        if not d["invoices"][month]:
            del d["invoices"][month]
            continue
        for inv in d["invoices"][month]:
            for k in ("clientId", "taxRateId", "date"):
                if inv.get(k) is None:
                    inv[k] = ""
            for it in inv.get("items", []):
                it.pop("amount", None)
                for k in ("qty", "price"):
                    if k in it:
                        it[k] = strify(it[k])
            for e in inv.get("expenses", []):
                if "amount" in e:
                    e["amount"] = strify(e["amount"])
            for p in inv.get("payments", []):
                if "amount" in p:
                    p["amount"] = strify(p["amount"])
    for site in list(d["siteCosts"]):
        if not d["siteCosts"][site]:
            del d["siteCosts"][site]
            continue
        for c in d["siteCosts"][site]:
            if "amount" in c:
                c["amount"] = strify(c["amount"])
    for cid, tpl in d["settings"].get("invoiceTemplates", {}).items():
        for it in tpl.get("items", []):
            for k in ("qty", "price"):
                if k in it:
                    it[k] = strify(it[k])
        for e in tpl.get("expenses", []):
            if "amount" in e:
                e["amount"] = strify(e["amount"])
    d["closed"] = {k: {m: True for m, v in (d["closed"].get(k) or {}).items() if v} for k in ("manHours", "invoices")}
    if not d["settings"].get("invoiceSeqByMonth"):
        d["settings"].pop("invoiceSeqByMonth", None)
    if not d.get("migrationHistory"):
        d.pop("migrationHistory", None)
    return strip_none(d)


def diff_paths(a, b, path=""):
    """2つのデータの差を、最初の数件だけ文字列で返す"""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                out.append(f"{path}/{k}: 元に無い項目が増えています")
            elif k not in b:
                out.append(f"{path}/{k}: 元の項目が失われています")
            else:
                out += diff_paths(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: 件数が違います（{len(a)} → {len(b)}）")
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff_paths(x, y, f"{path}[{i}]")
    elif a != b:
        out.append(f"{path}: {a!r} → {b!r}")
    return out[:20]


def verify_migration(data, db_path, key_hex=None):
    """移行後のDBが、元のJSONと同じ内容・同じ計算結果になっているかを確認する。問題の一覧を返す（空なら合格）"""
    problems = []
    conn = open_db(db_path, key_hex)
    conn.row_factory = _row_factory(conn)
    q = lambda sql, *a: conn.execute(sql, a).fetchall()
    if q("PRAGMA integrity_check")[0][0] != "ok":
        problems.append("DBの整合性チェック（integrity_check）に失敗しました。")
    if key_hex and q("PRAGMA cipher_integrity_check"):
        problems.append("暗号化DBのページ検証（cipher_integrity_check）に失敗しました。")
    if q("PRAGMA foreign_key_check"):
        problems.append("外部キーの不整合があります。")
    tax_rates = data["settings"]["taxRates"]
    # 1) 請求書の金額：DBのビュー＝アプリと同じ計算
    totals = {r["id"]: r for r in q("SELECT * FROM v_invoice_totals")}
    n_inv = 0
    for month, lst in data["invoices"].items():
        for inv in lst:
            n_inv += 1
            exp = invoice_financials(inv, tax_rates)
            got = totals.get(inv["id"])
            if got is None:
                problems.append(f"請求書 {inv.get('no')} が金額ビューにありません。")
                continue
            for k in ("subtotal", "grand", "paid", "remaining"):
                if abs(got[k] - exp[k]) > 1e-6:
                    problems.append(f"請求書 {inv.get('no')} の {k} が一致しません（JSON {exp[k]} / DB {got[k]}）")
            if got["tax"] != exp["tax"]:
                problems.append(f"請求書 {inv.get('no')} の消費税が一致しません（JSON {exp['tax']} / DB {got['tax']}）")
    if len(totals) != n_inv:
        problems.append(f"請求書の件数が一致しません（JSON {n_inv} / DB {len(totals)}）")
    # 2) 作業人員の月合計
    mhm = {r["month"]: r for r in q("SELECT * FROM v_man_hour_month")}
    for month, m in data["manHours"].items():
        people = ot = 0.0
        for r in m.get("rows", []):
            tot = sum(cell_number(r.get("kind"), r.get("content"), v) for v in (r.get("daily") or {}).values())
            if is_overtime_row(r.get("kind"), r.get("content")):
                ot += tot
            else:
                people += tot
        got = mhm.get(month)
        if got is None or abs(got["people"] - round2(people)) > 0.011 or abs(got["overtime"] - round2(ot)) > 0.011:
            problems.append(f"{month} の作業人員の合計が一致しません（JSON 人員{round2(people)}・残業{round2(ot)} / DB {None if got is None else (got['people'], got['overtime'])}）")
    # 3) 操作履歴ログの連鎖・先頭
    entries = [dict(seq=r["seq"], time=r["time"], action=r["action"], prev=r["prev_hash"], hash=r["hash"]) for r in q("SELECT * FROM audit_log ORDER BY seq")]
    if verify_audit_chain(entries) is not None:
        problems.append("DB上の操作履歴ログの連鎖が壊れています。")
    if len(entries) != len(data["auditLog"]):
        problems.append("操作履歴ログの件数が一致しません。")
    # 4) 全体の往復一致（JSON → DB → JSON が元と同じ）
    back = export_sqlite_to_json(conn)
    problems += ["往復差分：" + x for x in diff_paths(normalize(data), normalize(back))]
    # 5) 発行済み請求書の内容ハッシュ（DBから復元した内容でも発行時のハッシュと一致する）
    orig_ok = {inv["id"]: invoice_content_hash(inv) == inv.get("issueHash")
               for lst in data["invoices"].values() for inv in lst if inv.get("issued") and inv.get("issueHash")}
    for month, lst in back["invoices"].items():
        for inv in lst:
            if orig_ok.get(inv["id"]) and invoice_content_hash(inv) != inv["issueHash"]:
                problems.append(f"DBから復元した請求書 {inv.get('no')} の内容ハッシュが発行時と一致しません。")
    conn.close()
    return problems


def main():
    # 標準出力・エラー出力をUTF-8にする（Windowsの既定の文字コード（cp1252・cp932）では、日本語を出力できず落ちることがある）
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="統合業務システム JSON⇔SQLite 移行ツール")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("migrate"); a.add_argument("src"); a.add_argument("dst"); a.add_argument("--overwrite", action="store_true"); a.add_argument("--key-file")
    b = sub.add_parser("verify"); b.add_argument("src"); b.add_argument("db"); b.add_argument("--key-file")
    c = sub.add_parser("export"); c.add_argument("db"); c.add_argument("dst"); c.add_argument("--key-file")
    k = sub.add_parser("keygen"); k.add_argument("out")
    r = sub.add_parser("key-from-code"); r.add_argument("code"); r.add_argument("out")
    sh = sub.add_parser("recovery-sheet"); sh.add_argument("keyfile"); sh.add_argument("out"); sh.add_argument("--label", default="")
    cc = sub.add_parser("check-code"); cc.add_argument("keyfile"); cc.add_argument("code")
    args = ap.parse_args()
    key = read_key_file(args.key_file) if getattr(args, "key_file", None) else None
    try:
        if args.cmd in ("keygen", "key-from-code"):
            kh = generate_key_hex() if args.cmd == "keygen" else parse_recovery_code(args.code)
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)      # 既存ファイルは上書きしない
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(kh + "\n")
            print("鍵ファイルを作成しました:", args.out)
            print("復旧コード（紙に印刷して、PCとは別の場所に保管してください）:")
            print("  " + recovery_code(kh))
            if os.name == "nt":
                print("注意：Windowsでは、ファイルの権限が本人だけに制限されません。ファイルは、自分だけがアクセスできるフォルダ（BitLockerで保護したドライブ上など）に置いてください。")
        elif args.cmd == "recovery-sheet":
            html = recovery_sheet_html(read_key_file(args.keyfile), args.label)
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)      # 既存ファイルは上書きしない
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(html)
            print("控え用紙を作成しました:", args.out)
            print("ブラウザで開いて印刷し、印刷後はこのファイルを削除してください。")
            if os.name == "nt":
                print("注意：Windowsでは、ファイルの権限が本人だけに制限されません。ファイルは、自分だけがアクセスできるフォルダ（BitLockerで保護したドライブ上など）に置いてください。")
        elif args.cmd == "check-code":
            if parse_recovery_code(args.code) == read_key_file(args.keyfile):
                print("一致しました。このコードで鍵を復元できます。")
            else:
                print("一致しません。コードの書き写しを確認してください。", file=sys.stderr)
                sys.exit(1)
        elif args.cmd == "migrate":
            rep = migrate(args.src, args.dst, args.overwrite, key)
            print("移行が完了しました（自己検証に合格）:", args.dst, "（暗号化あり）" if key else "（暗号化なし）")
            print("件数:", json.dumps(rep["counts"], ensure_ascii=False))
            for w in rep["warnings"]:
                print("注意:", w)
        elif args.cmd == "verify":
            data, _ = load_backup(args.src)
            p = verify_migration(data, args.db, key)
            print("合格" if not p else "不一致:\n・" + "\n・".join(p))
            sys.exit(0 if not p else 1)
        else:
            out = export_sqlite_to_json(args.db, key)
            with open(args.dst, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
            print("書き出しました:", args.dst)
    except MigrationError as e:
        print(e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
