//! 統合業務システム デスクトップアプリ（最小版）。
//! 画面（ui フォルダの HTML/JS）から呼ぶコマンドを定義する。処理の中身は ibis-core（appcore・keystore）にあり、
//! ここは受け渡しだけを行う。鍵そのもの（64桁の16進数）は、画面に渡さない。

use ibis_core::appcore::{self, DbState, DbSummary, KeyState, Paths};
use ibis_core::keystore::{self, PendingKey, Protector};
use serde::Serialize;
use std::path::PathBuf;
use std::sync::Mutex;
use tauri::State;
use tauri_plugin_dialog::DialogExt;

/// 新しく作った、まだ保存していない鍵（復旧コードの再入力を待っている間だけ持つ）
#[derive(Default)]
struct AppState {
    pending: Mutex<Option<PendingKey>>,
}

#[derive(Serialize)]
struct KeyDto {
    state: &'static str, // missing | ok | error
    method: String,
    fingerprint: String,
    message: String,
}

#[derive(Serialize, Default)]
struct SummaryDto {
    tables: i64,
    invoices: i64,
    clients: i64,
    audit_entries: i64,
    created_at: String,
    source_name: String,
    problems: Vec<String>,
}

#[derive(Serialize)]
struct DbDto {
    state: &'static str, // missing | needs_key | ok | error
    summary: Option<SummaryDto>,
    message: String,
}

#[derive(Serialize)]
struct StatusDto {
    app_version: &'static str,
    platform: &'static str,
    key_path: String,
    db_path: String,
    key: KeyDto,
    db: DbDto,
}

#[derive(Serialize)]
struct NewKeyDto {
    recovery_code: String,
    fingerprint: String,
}

fn summary_dto(s: DbSummary) -> SummaryDto {
    SummaryDto {
        tables: s.tables,
        invoices: s.invoices,
        clients: s.clients,
        audit_entries: s.audit_entries,
        created_at: s.created_at,
        source_name: s.source_name,
        problems: s.problems,
    }
}

fn paths() -> Result<Paths, String> {
    Paths::default_paths().map_err(|e| e.to_string())
}

fn protector() -> Box<dyn Protector> {
    keystore::platform_protector()
}

const APP_VERSION: &str = env!("CARGO_PKG_VERSION");

#[tauri::command]
async fn get_status() -> Result<StatusDto, String> {
    let paths = paths()?;
    let p = protector();
    let s = appcore::status(&paths, p.as_ref());
    let key = match s.key {
        KeyState::Missing => KeyDto { state: "missing", method: String::new(), fingerprint: String::new(), message: String::new() },
        KeyState::Ok { method, fingerprint } => KeyDto { state: "ok", method, fingerprint, message: String::new() },
        KeyState::Error { message } => KeyDto { state: "error", method: String::new(), fingerprint: String::new(), message },
    };
    let db = match s.db {
        DbState::Missing => DbDto { state: "missing", summary: None, message: String::new() },
        DbState::NeedsKey => DbDto { state: "needs_key", summary: None, message: String::new() },
        DbState::Ok(sum) => DbDto { state: "ok", summary: Some(summary_dto(sum)), message: String::new() },
        DbState::Error { message } => DbDto { state: "error", summary: None, message },
    };
    Ok(StatusDto {
        app_version: APP_VERSION,
        platform: if cfg!(windows) { "windows" } else { "other" },
        key_path: s.key_path.display().to_string(),
        db_path: s.db_path.display().to_string(),
        key,
        db,
    })
}

/// ファイルを選ぶ（キャンセルなら None）。ダイアログはRust側で開く
#[tauri::command]
async fn pick_key_file(app: tauri::AppHandle) -> Result<Option<String>, String> {
    let f = app.dialog().file().set_title("移行ツールで作った鍵ファイル（key.txt）を選んでください").blocking_pick_file();
    Ok(f.and_then(|p| p.into_path().ok()).map(|p| p.display().to_string()))
}

#[tauri::command]
async fn pick_folder(app: tauri::AppHandle) -> Result<Option<String>, String> {
    let f = app.dialog().file().set_title("バックアップの保存先フォルダを選んでください").blocking_pick_folder();
    Ok(f.and_then(|p| p.into_path().ok()).map(|p| p.display().to_string()))
}

#[tauri::command]
async fn key_import(path: String) -> Result<String, String> {
    let paths = paths()?;
    let p = protector();
    keystore::import_key_file(&PathBuf::from(path), &paths.key, p.as_ref(), false).map_err(|e| e.to_string())
}

#[tauri::command]
async fn key_restore(code: String) -> Result<String, String> {
    let paths = paths()?;
    let p = protector();
    keystore::restore_from_code(&code, &paths.key, p.as_ref(), false).map_err(|e| e.to_string())
}

#[tauri::command]
async fn key_new_begin(state: State<'_, AppState>) -> Result<NewKeyDto, String> {
    let paths = paths()?;
    if paths.key.exists() {
        return Err(format!("鍵の保管ファイルがすでにあります：{}（上書きしません）", paths.key.display()));
    }
    let pending = keystore::begin_new_key().map_err(|e| e.to_string())?;
    let dto = NewKeyDto { recovery_code: pending.recovery_code().to_string(), fingerprint: pending.fingerprint().map_err(|e| e.to_string())? };
    *state.pending.lock().map_err(|_| "内部の状態を読めませんでした。".to_string())? = Some(pending);
    Ok(dto)
}

#[tauri::command]
async fn key_new_confirm(code: String, state: State<'_, AppState>) -> Result<String, String> {
    let paths = paths()?;
    let p = protector();
    let mut guard = state.pending.lock().map_err(|_| "内部の状態を読めませんでした。".to_string())?;
    let pending = guard.as_ref().ok_or_else(|| "新しい鍵の作成が始まっていません。最初からやり直してください。".to_string())?;
    keystore::save_new_key(&paths.key, p.as_ref(), pending, &code).map_err(|e| e.to_string())?;
    let fp = pending.fingerprint().map_err(|e| e.to_string())?;
    *guard = None; // 保存できたら、メモリ上の鍵と復旧コードを消す（PendingKey は破棄時にゼロで上書きする）
    Ok(fp)
}

#[tauri::command]
async fn key_new_cancel(state: State<'_, AppState>) -> Result<(), String> {
    *state.pending.lock().map_err(|_| "内部の状態を読めませんでした。".to_string())? = None;
    Ok(())
}

#[tauri::command]
async fn key_check_code(code: String) -> Result<bool, String> {
    let paths = paths()?;
    let p = protector();
    keystore::check_code(&paths.key, p.as_ref(), &code).map_err(|e| e.to_string())
}

#[tauri::command]
async fn db_create() -> Result<SummaryDto, String> {
    let paths = paths()?;
    let p = protector();
    appcore::create_empty_db(&paths, p.as_ref(), &format!("ibis-desktop {}", APP_VERSION))
        .map(summary_dto)
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn db_backup(dir: String) -> Result<String, String> {
    let paths = paths()?;
    let p = protector();
    appcore::backup_to_dir(&paths, p.as_ref(), &PathBuf::from(dir))
        .map(|f| f.display().to_string())
        .map_err(|e| e.to_string())
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            get_status,
            pick_key_file,
            pick_folder,
            key_import,
            key_restore,
            key_new_begin,
            key_new_confirm,
            key_new_cancel,
            key_check_code,
            db_create,
            db_backup
        ])
        .run(tauri::generate_context!())
        .expect("アプリを起動できませんでした");
}
