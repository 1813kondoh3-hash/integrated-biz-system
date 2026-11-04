//! 統合業務システム C案：データ層（SQLCipher）の最小実装。
//! 目的は「Rust＋rusqlite＋SQLCipher で、設計済みのスキーマ・トリガー・暗号化・操作ログ・復旧コードが
//! Pythonの参照実装と互換に動く」ことの確認。画面（Tauri）はこの上に載せる。
pub mod keystore;

use rand::RngCore;
use rusqlite::{params, Connection};
use sha2::{Digest, Sha256};

pub const SCHEMA_SQL: &str = include_str!("../../../db/schema.sql");
pub const GENESIS: &str = "0000000000000000000000000000000000000000000000000000000000000000";

#[derive(Debug)]
pub struct IbisError(pub String);
impl std::fmt::Display for IbisError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}
impl std::error::Error for IbisError {}
impl From<rusqlite::Error> for IbisError {
    fn from(e: rusqlite::Error) -> Self {
        IbisError(e.to_string())
    }
}
type Result<T> = std::result::Result<T, IbisError>;

// ---------------------------------------------------------------- 鍵と復旧コード
/// ランダムな256ビットの鍵（64桁の16進）
pub fn generate_key_hex() -> String {
    let mut k = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut k);
    hex::encode(k)
}

pub fn normalize_key(key_hex: &str) -> Result<String> {
    let k = key_hex.trim().to_lowercase();
    if k.len() == 64 && k.chars().all(|c| c.is_ascii_hexdigit()) {
        Ok(k)
    } else {
        Err(IbisError("暗号鍵は64桁の16進数（256ビット）で指定してください。".into()))
    }
}

// 復旧コードの形式（55文字）：鍵256ビット＋ハッシュ先頭14ビット＝270ビット＝54文字（Base32）＋ 検査文字1文字（Luhn mod 32）。
// 検査文字により、1文字の書き間違いは必ず検出できる。ハッシュ14ビットは、複数文字の誤りを検出する。Python版と同じ形式
const B32: &[u8; 32] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

fn luhn32_generate(vals: &[u32]) -> u32 {
    let (mut factor, mut total) = (2u32, 0u32);
    for &v in vals.iter().rev() {
        let a = factor * v;
        factor = if factor == 2 { 1 } else { 2 };
        total += a / 32 + a % 32;
    }
    (32 - total % 32) % 32
}

fn luhn32_valid(vals: &[u32]) -> bool {
    let (mut factor, mut total) = (1u32, 0u32);
    for &v in vals.iter().rev() {
        let a = factor * v;
        factor = if factor == 1 { 2 } else { 1 };
        total += a / 32 + a % 32;
    }
    total % 32 == 0
}

fn hash14(raw: &[u8]) -> u32 {
    let h = Sha256::digest(raw);
    ((((h[0] as u32) << 8) | h[1] as u32) >> 2) & 0x3FFF
}

/// 鍵を紙に控えるための復旧コード（55文字。5文字ずつ「-」で区切る）
pub fn recovery_code(key_hex: &str) -> Result<String> {
    let raw = hex::decode(normalize_key(key_hex)?).unwrap();
    let mut bits: Vec<u8> = Vec::with_capacity(270);
    for byte in &raw {
        for b in (0..8).rev() {
            bits.push((byte >> b) & 1);
        }
    }
    let h = hash14(&raw);
    for b in (0..14).rev() {
        bits.push(((h >> b) & 1) as u8);
    }
    let mut vals: Vec<u32> = bits.chunks(5).map(|c| c.iter().fold(0u32, |a, &x| (a << 1) | x as u32)).collect();
    let chk = luhn32_generate(&vals);
    vals.push(chk);
    let s: Vec<u8> = vals.iter().map(|&v| B32[v as usize]).collect();
    Ok(s.chunks(5).map(|c| std::str::from_utf8(c).unwrap()).collect::<Vec<_>>().join("-"))
}

fn decode_recovery(s: &str) -> Option<String> {
    let mut vals: Vec<u32> = Vec::with_capacity(55);
    for ch in s.bytes() {
        vals.push(B32.iter().position(|&c| c == ch)? as u32);
    }
    if vals.len() != 55 || !luhn32_valid(&vals) {
        return None;
    }
    let mut bits: Vec<u8> = Vec::with_capacity(270);
    for &v in &vals[..54] {
        for b in (0..5).rev() {
            bits.push(((v >> b) & 1) as u8);
        }
    }
    let raw: Vec<u8> = bits[..256].chunks(8).map(|c| c.iter().fold(0u8, |a, &x| (a << 1) | x)).collect();
    let h = bits[256..].iter().fold(0u32, |a, &x| (a << 1) | x as u32);
    if h != hash14(&raw) {
        return None;
    }
    Some(hex::encode(raw))
}

/// 復旧コードから鍵を復元する。大小文字・空白・ハイフンは無視。
/// 手書きの取り違えに備え、このコードでは使われない数字を読み替える（0→O、8→B、1→I または L）。
/// 読み替えが正しいかは、検査文字とハッシュで確認する。1文字の書き間違いは必ずエラーになる。
/// 1が I か L か判別できない（両方とも検査を通る）場合は、推測せずエラーにする
pub fn parse_recovery_code(code: &str) -> Result<String> {
    let plain: String = code.to_uppercase().chars().filter(|c| !c.is_whitespace() && *c != '-').collect();
    let s = plain.replace('0', "O").replace('8', "B");
    let ones = s.matches('1').count();
    if ones > 4 {
        return Err(IbisError("復旧コードの形式が正しくありません。".into()));
    }
    let mut variants = vec![s.clone()];
    for _ in 0..ones {
        variants = variants
            .iter()
            .flat_map(|v| ["I", "L"].iter().map(move |c| v.replacen('1', c, 1)))
            .collect();
    }
    let mut found: Vec<String> = vec![];
    for v in &variants {
        if let Some(key) = decode_recovery(v) {
            if !found.contains(&key) {
                found.push(key);
            }
        }
    }
    match found.len() {
        1 => Ok(found.remove(0)),
        0 if plain.chars().count() != 55 => Err(IbisError("復旧コードの文字数が違います（全55文字）。".into())),
        0 => Err(IbisError("復旧コードが正しくありません（入力ミスの可能性があります。控えと見比べてください）。".into())),
        // 推測で選ばない（誤った鍵を黙って受け付けない）
        _ => Err(IbisError("1 が I と L のどちらか判別できません。控えを見て、I または L の文字で入力してください。".into())),
    }
}

// ---------------------------------------------------------------- 接続
/// DBを開く。鍵があればSQLCipherで復号し、開けるか（鍵が合っているか）をその場で確認する
pub fn open(path: &str, key_hex: Option<&str>) -> Result<Connection> {
    let conn = Connection::open(path)?;
    if let Some(k) = key_hex {
        let k = normalize_key(k)?;
        conn.execute_batch(&format!("PRAGMA key = \"x'{}'\"; PRAGMA cipher_memory_security = ON;", k))?;
    }
    conn.query_row("SELECT count(*) FROM sqlite_master", [], |r| r.get::<_, i64>(0))
        .map_err(|e| IbisError(format!("DBを開けません（鍵が違う、またはDBが壊れています）：{e}")))?;
    conn.execute_batch("PRAGMA foreign_keys = ON; PRAGMA temp_store = MEMORY; PRAGMA secure_delete = ON;")?;
    Ok(conn)
}

pub fn init_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(SCHEMA_SQL)?;
    Ok(())
}

// ---------------------------------------------------------------- 操作ログ（追記専用・ハッシュ連鎖）
pub fn audit_hash(prev: &str, seq: i64, time: &str, action: &str) -> String {
    hex::encode(Sha256::digest(format!("{prev}|{seq}|{time}|{action}").as_bytes()))
}

/// 操作ログを1件追記する（連番・直前ハッシュの連続性は、DBのトリガーも検査する）
pub fn append_audit(conn: &Connection, time: &str, action: &str) -> Result<(i64, String)> {
    let (seq, prev): (i64, String) = conn
        .query_row("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1", [], |r| Ok((r.get::<_, i64>(0)? + 1, r.get(1)?)))
        .unwrap_or((1, GENESIS.to_string()));
    let hash = audit_hash(&prev, seq, time, action);
    conn.execute("INSERT INTO audit_log(seq,time,action,prev_hash,hash) VALUES(?,?,?,?,?)", params![seq, time, action, prev, hash])?;
    Ok((seq, hash))
}

/// 連鎖を検証する。正常なら None、壊れていれば最初に壊れた seq
pub fn verify_audit_chain(conn: &Connection) -> Result<Option<i64>> {
    let mut st = conn.prepare("SELECT seq,time,action,prev_hash,hash FROM audit_log ORDER BY seq")?;
    let rows = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, String>(3)?, r.get::<_, String>(4)?)))?;
    let mut prev = GENESIS.to_string();
    let mut i = 0i64;
    for row in rows {
        let (seq, time, action, p, h) = row?;
        i += 1;
        if seq != i || p != prev || h != audit_hash(&prev, seq, &time, &action) {
            return Ok(Some(seq));
        }
        prev = h;
    }
    Ok(None)
}

// ---------------------------------------------------------------- 検証・集計・バックアップ
/// DBの健全性。問題の一覧（空なら健全）
pub fn health_problems(conn: &Connection, encrypted: bool) -> Result<Vec<String>> {
    let mut out = vec![];
    let ok: String = conn.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
    if ok != "ok" {
        out.push("integrity_check に失敗しました。".into());
    }
    let fk: i64 = conn.query_row("SELECT count(*) FROM pragma_foreign_key_check", [], |r| r.get(0))?;
    if fk > 0 {
        out.push("外部キーの不整合があります。".into());
    }
    if encrypted {
        let mut st = conn.prepare("PRAGMA cipher_integrity_check")?;
        if st.query([])?.next()?.is_some() {
            out.push("暗号化DBのページ検証に失敗しました。".into());
        }
    }
    if let Some(seq) = verify_audit_chain(conn)? {
        out.push(format!("操作ログの連鎖が No.{seq} で壊れています。"));
    }
    Ok(out)
}

/// 請求書の金額（DBのビューで計算）：(伝票No., 小計, 消費税, 総合計, 入金, 残額)
pub fn invoice_totals(conn: &Connection) -> Result<Vec<(String, f64, f64, f64, f64, f64)>> {
    let mut st = conn.prepare("SELECT no, subtotal, tax, grand, paid, remaining FROM v_invoice_totals ORDER BY no")?;
    let v = st
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    Ok(v)
}

/// 整合した完全なコピー（暗号化は引き継がれる）。書き込み中でも取得できる
pub fn backup_into(conn: &Connection, dest: &str) -> Result<()> {
    let esc = dest.replace('\'', "''");
    conn.execute_batch(&format!("VACUUM INTO '{esc}'"))?;
    Ok(())
}
