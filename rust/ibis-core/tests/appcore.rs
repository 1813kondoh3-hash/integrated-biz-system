//! デスクトップアプリの中身（appcore）のテスト。画面を使わずに、状態の確認・DBの新規作成・バックアップを確かめる
use ibis_core::appcore::{self, DbState, KeyState, Paths};
use ibis_core::keystore::{self, Protector, KIND_TEST};
use ibis_core::{generate_key_hex, recovery_code, IbisError};
use std::fs;
use std::path::PathBuf;

struct TestProtector;
impl Protector for TestProtector {
    fn kind(&self) -> u8 { KIND_TEST }
    fn name(&self) -> &'static str { "テスト用" }
    fn protect(&self, plain: &[u8]) -> Result<Vec<u8>, IbisError> {
        Ok(plain.iter().enumerate().map(|(i, b)| b ^ 0x5A ^ (i as u8)).collect())
    }
    fn unprotect(&self, blob: &[u8]) -> Result<Vec<u8>, IbisError> { self.protect(blob) }
}

fn tmpdir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("ibis_appcore_{}_{}", name, std::process::id()));
    let _ = fs::remove_dir_all(&d);
    fs::create_dir_all(&d).unwrap();
    d
}

fn contains(hay: &[u8], needle: &[u8]) -> bool {
    hay.windows(needle.len()).any(|w| w == needle)
}

#[test]
fn status_goes_from_missing_to_ready_and_db_is_encrypted() {
    let d = tmpdir("flow");
    let paths = Paths::in_dir(&d);
    let p = TestProtector;

    // 1. 何もない
    let s = appcore::status(&paths, &p);
    assert_eq!(s.key, KeyState::Missing);
    assert_eq!(s.db, DbState::Missing);
    // 鍵がないとDBは作れない
    assert!(appcore::create_empty_db(&paths, &p, "test").is_err());
    assert!(!paths.db.exists());

    // 2. 鍵を用意（復旧コードから）
    let key = generate_key_hex();
    let fp = keystore::restore_from_code(&recovery_code(&key).unwrap(), &paths.key, &p, false).unwrap();
    match appcore::status(&paths, &p).key {
        KeyState::Ok { fingerprint, .. } => assert_eq!(fingerprint, fp),
        k => panic!("鍵を開けるはず：{:?}", k),
    }

    // 3. 空のDBを作る
    let sum = appcore::create_empty_db(&paths, &p, "test-0.1").unwrap();
    let expected = ibis_core::SCHEMA_SQL.matches("CREATE TABLE").count() as i64;
    assert_eq!(sum.tables, expected, "スキーマのテーブルがそろう");
    assert_eq!((sum.invoices, sum.clients, sum.audit_entries), (0, 0, 1));
    assert!(sum.problems.is_empty(), "{:?}", sum.problems);
    assert_eq!(sum.source_name, "（新規作成）");
    assert!(!paths.db.with_extension("creating").exists(), "一時ファイルを残さない");

    // DBファイルに平文が残らない（SQLiteの見出しも、鍵も、メタ情報の文字列も）
    let raw = fs::read(&paths.db).unwrap();
    assert!(!raw.starts_with(b"SQLite format 3"), "暗号化されている");
    assert!(!contains(&raw, key.as_bytes()));
    assert!(!contains(&raw, "新規作成".as_bytes()));

    // 4. 状態は「開けた」になる
    match appcore::status(&paths, &p).db {
        DbState::Ok(s2) => assert_eq!(s2, sum),
        x => panic!("DBを開けるはず：{:?}", x),
    }

    // 5. 2回目は上書きしない
    let e = appcore::create_empty_db(&paths, &p, "test").unwrap_err();
    assert!(e.to_string().contains("上書きしません"));
}

#[test]
fn wrong_key_or_missing_key_is_reported_without_touching_db() {
    let d = tmpdir("wrong");
    let paths = Paths::in_dir(&d);
    let p = TestProtector;
    keystore::restore_from_code(&recovery_code(&generate_key_hex()).unwrap(), &paths.key, &p, false).unwrap();
    appcore::create_empty_db(&paths, &p, "test").unwrap();
    let before = fs::read(&paths.db).unwrap();

    // 鍵の保管ファイルを別の鍵に差し替える → DBは「開けない」になり、DBは変わらない
    keystore::restore_from_code(&recovery_code(&generate_key_hex()).unwrap(), &paths.key, &p, true).unwrap();
    match appcore::status(&paths, &p).db {
        DbState::Error { message } => assert!(message.contains("開けません"), "{}", message),
        x => panic!("別の鍵では開けないはず：{:?}", x),
    }
    // 鍵の保管ファイルがない → 「鍵が必要」
    fs::remove_file(&paths.key).unwrap();
    let s = appcore::status(&paths, &p);
    assert_eq!(s.key, KeyState::Missing);
    assert_eq!(s.db, DbState::NeedsKey);
    assert_eq!(fs::read(&paths.db).unwrap(), before, "状態の確認でDBを変更しない");
}

#[test]
fn backup_is_encrypted_and_opens_with_the_same_key() {
    let d = tmpdir("backup");
    let paths = Paths::in_dir(&d);
    let p = TestProtector;
    let key = generate_key_hex();
    keystore::restore_from_code(&recovery_code(&key).unwrap(), &paths.key, &p, false).unwrap();
    appcore::create_empty_db(&paths, &p, "test").unwrap();

    let out = d.join("backup");
    assert!(appcore::backup_to_dir(&paths, &p, &out).is_err(), "フォルダがないときは失敗する");
    fs::create_dir_all(&out).unwrap();
    let file = appcore::backup_to_dir(&paths, &p, &out).unwrap();
    assert!(file.file_name().unwrap().to_str().unwrap().starts_with("ibis-backup-"));
    let raw = fs::read(&file).unwrap();
    assert!(!raw.starts_with(b"SQLite format 3"), "バックアップも暗号化されている");
    assert!(!contains(&raw, key.as_bytes()));
    let conn = ibis_core::open(file.to_str().unwrap(), Some(&key)).unwrap();
    let s = appcore::summarize(&conn).unwrap();
    assert_eq!(s.audit_entries, 1);
    assert!(ibis_core::open(file.to_str().unwrap(), Some(&generate_key_hex())).is_err(), "別の鍵では開けない");
}

#[test]
fn iso_time_matches_javascript_format() {
    assert_eq!(appcore::iso_from_unix_millis(0), "1970-01-01T00:00:00.000Z");
    assert_eq!(appcore::iso_from_unix_millis(951_782_400_000), "2000-02-29T00:00:00.000Z");
    assert_eq!(appcore::iso_from_unix_millis(1_793_840_461_007), "2026-11-05T01:01:01.007Z");
    assert_eq!(appcore::backup_file_name("2026-11-05T01:01:01.007Z"), "ibis-backup-20261105-010101.db");
    let now = appcore::now_iso();
    assert_eq!(now.len(), 24);
    assert!(now.ends_with('Z'));
}
