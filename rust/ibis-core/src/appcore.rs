//! デスクトップアプリ（Tauri）の中身。画面に依存しない処理をここに置き、Linuxでもテストできるようにする。
//!
//! - 状態の確認：鍵（DPAPIの保管ファイル）とDBの状態をまとめて返す
//! - DBの新規作成：保管した鍵で暗号化した空のDBを作る（既存のDBは上書きしない）
//! - バックアップ：暗号化したまま、整合したコピーを作り、開けることを確かめる
//!
//! 鍵そのもの（64桁の16進数）は、戻り値にもエラー文にも含めない。

use crate::keystore::{self, Protector};
use crate::{append_audit, backup_into, health_problems, init_schema, open, IbisError};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

type Result<T> = std::result::Result<T, IbisError>;

/// このアプリのDBであることの目印（schema.sql の PRAGMA application_id と同じ値。'IBIS'）
pub const APPLICATION_ID: i64 = 1229212755;
pub const DB_FILE_NAME: &str = "ibis.db";

/// 鍵とDBの置き場所
#[derive(Debug, Clone)]
pub struct Paths {
    pub key: PathBuf,
    pub db: PathBuf,
}

impl Paths {
    /// 既定の場所。Windows：%LOCALAPPDATA%\IntegratedBizSystem\key.dpapi と ibis.db（同じフォルダ）
    pub fn default_paths() -> Result<Paths> {
        let key = keystore::default_path()?;
        let dir = key.parent().map(Path::to_path_buf).unwrap_or_default();
        Ok(Paths { db: dir.join(DB_FILE_NAME), key })
    }
    /// テスト・検証用：指定したフォルダの中に置く
    pub fn in_dir(dir: &Path) -> Paths {
        Paths { key: dir.join("key.store"), db: dir.join(DB_FILE_NAME) }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum KeyState {
    /// 保管ファイルがない（鍵の準備が必要）
    Missing,
    /// 開けた。保管方式と指紋
    Ok { method: String, fingerprint: String },
    /// 保管ファイルはあるが開けない（別のWindowsユーザー、破損など）
    Error { message: String },
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct DbSummary {
    pub tables: i64,
    pub invoices: i64,
    pub clients: i64,
    pub audit_entries: i64,
    pub created_at: String,
    pub source_name: String,
    /// 健全性の問題（空なら健全）
    pub problems: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum DbState {
    /// DBファイルがない
    Missing,
    /// 鍵がないので開けない
    NeedsKey,
    /// 開けた
    Ok(DbSummary),
    /// 開けない（鍵が違う、壊れている、このアプリのDBではない など）
    Error { message: String },
}

#[derive(Debug, Clone, PartialEq)]
pub struct AppStatus {
    pub key_path: PathBuf,
    pub db_path: PathBuf,
    pub key: KeyState,
    pub db: DbState,
}

/// 現在時刻（UTC、JavaScript の toISOString と同じ形式：2026-11-05T01:02:03.456Z）
pub fn now_iso() -> String {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    iso_from_unix_millis(d.as_millis() as i64)
}

/// UNIX時刻（ミリ秒）を toISOString の形式にする（暦の計算は、うるう年を含めて自前で行う）
pub fn iso_from_unix_millis(ms: i64) -> String {
    let secs = ms.div_euclid(1000);
    let milli = ms.rem_euclid(1000);
    let days = secs.div_euclid(86_400);
    let sod = secs.rem_euclid(86_400);
    // civil_from_days（Howard Hinnant のアルゴリズム）
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let mut y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    if m <= 2 {
        y += 1;
    }
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z", y, m, d, sod / 3600, sod % 3600 / 60, sod % 60, milli)
}

fn key_state(paths: &Paths, p: &dyn Protector) -> KeyState {
    if !paths.key.exists() {
        return KeyState::Missing;
    }
    match keystore::status(&paths.key, p) {
        Ok((method, fingerprint)) => KeyState::Ok { method, fingerprint },
        Err(e) => KeyState::Error { message: e.to_string() },
    }
}

/// DBを開いて、件数と健全性をまとめる
pub fn summarize(conn: &rusqlite::Connection) -> Result<DbSummary> {
    let app_id: i64 = conn.query_row("PRAGMA application_id", [], |r| r.get(0))?;
    if app_id != APPLICATION_ID {
        return Err(IbisError("このアプリのDBではありません（目印が一致しません）。".into()));
    }
    let count = |sql: &str| -> Result<i64> { Ok(conn.query_row(sql, [], |r| r.get(0))?) };
    let meta = |k: &str| -> String {
        conn.query_row("SELECT value FROM meta WHERE key = ?", [k], |r| r.get(0)).unwrap_or_default()
    };
    Ok(DbSummary {
        tables: count("SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")?,
        invoices: count("SELECT count(*) FROM invoices")?,
        clients: count("SELECT count(*) FROM clients")?,
        audit_entries: count("SELECT count(*) FROM audit_log")?,
        created_at: meta("created_at"),
        source_name: meta("source_name"),
        problems: health_problems(conn, true)?,
    })
}

fn open_db_with_key(db: &Path, key_hex: &str) -> Result<rusqlite::Connection> {
    let s = db.to_str().ok_or_else(|| IbisError("DBの場所に使えない文字が含まれています。".into()))?;
    open(s, Some(key_hex))
}

/// 鍵とDBの状態をまとめて返す（何も変更しない）
pub fn status(paths: &Paths, p: &dyn Protector) -> AppStatus {
    let key = key_state(paths, p);
    let db = if !paths.db.exists() {
        DbState::Missing
    } else {
        match &key {
            KeyState::Ok { .. } => match keystore::load_key(&paths.key, p)
                .and_then(|k| open_db_with_key(&paths.db, &k))
                .and_then(|c| summarize(&c))
            {
                Ok(s) => DbState::Ok(s),
                Err(e) => DbState::Error { message: e.to_string() },
            },
            _ => DbState::NeedsKey,
        }
    };
    AppStatus { key_path: paths.key.clone(), db_path: paths.db.clone(), key, db }
}

/// 保管した鍵で暗号化した、空のDBを作る。既存のDBは上書きしない。
/// 一時ファイルに作ってから名前を変えるため、途中で止まっても中途半端なDBは残らない。
pub fn create_empty_db(paths: &Paths, p: &dyn Protector, tool_version: &str) -> Result<DbSummary> {
    if paths.db.exists() {
        return Err(IbisError(format!(
            "DBがすでにあります：{}（上書きしません。作り直す場合は、先にバックアップを取ってから、ご自身で移動してください）",
            paths.db.display())));
    }
    let key_hex = keystore::load_key(&paths.key, p)?;
    if let Some(dir) = paths.db.parent() {
        if !dir.as_os_str().is_empty() {
            fs::create_dir_all(dir).map_err(|e| IbisError(format!("DBのフォルダを作れませんでした（{}）。", e)))?;
        }
    }
    let tmp = paths.db.with_extension("creating");
    let _ = fs::remove_file(&tmp);
    let result = (|| -> Result<()> {
        let conn = open_db_with_key(&tmp, &key_hex)?;
        init_schema(&conn)?;
        let now = now_iso();
        let tx = conn.unchecked_transaction()?;
        for (k, v) in [
            ("db_schema_version", "1"),
            ("data_schema_version", "2"),
            ("created_at", now.as_str()),
            ("tool_version", tool_version),
            ("source_name", "（新規作成）"),
            ("audit_head_seq", "0"),
        ] {
            tx.execute("INSERT INTO meta(key,value) VALUES(?,?)", [k, v])?;
        }
        append_audit(&tx, &now, "DBを新規作成（デスクトップアプリ）")?;
        tx.commit()?;
        let s = summarize(&conn)?;
        if !s.problems.is_empty() {
            return Err(IbisError(format!("作成したDBの検査で問題が見つかりました：{}", s.problems.join("／"))));
        }
        Ok(())
    })();
    if let Err(e) = result {
        let _ = fs::remove_file(&tmp);
        return Err(e);
    }
    fs::rename(&tmp, &paths.db).map_err(|e| {
        let _ = fs::remove_file(&tmp);
        IbisError(format!("DBを所定の場所に置けませんでした（{}）。", e))
    })?;
    let conn = open_db_with_key(&paths.db, &key_hex)?;
    summarize(&conn)
}

/// バックアップのファイル名（例：ibis-backup-20261105-102030.db）。時刻はUTC
pub fn backup_file_name(iso: &str) -> String {
    let digits: String = iso.chars().filter(|c| c.is_ascii_digit()).collect();
    let (d, t) = digits.split_at(digits.len().min(8));
    format!("ibis-backup-{}-{}.db", d, &t[..t.len().min(6)])
}

/// 暗号化したまま、整合したコピーを指定のフォルダに作る。作ったあと、同じ鍵で開けて健全なことを確かめる
pub fn backup_to_dir(paths: &Paths, p: &dyn Protector, dest_dir: &Path) -> Result<PathBuf> {
    if !dest_dir.is_dir() {
        return Err(IbisError(format!("バックアップ先のフォルダがありません：{}", dest_dir.display())));
    }
    let key_hex = keystore::load_key(&paths.key, p)?;
    let conn = open_db_with_key(&paths.db, &key_hex)?;
    let dest = dest_dir.join(backup_file_name(&now_iso()));
    if dest.exists() {
        return Err(IbisError(format!("同じ名前のバックアップがあります：{}（1秒ほど待ってから、もう一度実行してください）", dest.display())));
    }
    let s = dest.to_str().ok_or_else(|| IbisError("バックアップ先に使えない文字が含まれています。".into()))?;
    backup_into(&conn, s)?;
    let check = open_db_with_key(&dest, &key_hex).and_then(|c| summarize(&c));
    match check {
        Ok(sum) if sum.problems.is_empty() => Ok(dest),
        Ok(sum) => Err(IbisError(format!("バックアップの検査で問題が見つかりました：{}", sum.problems.join("／")))),
        Err(e) => Err(IbisError(format!("バックアップを開けませんでした：{}", e))),
    }
}
