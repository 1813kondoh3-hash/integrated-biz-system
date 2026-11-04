//! ibis-key コマンドのテスト（鍵そのものを表示しないこと、確認と上書き防止）
use ibis_core::{generate_key_hex, recovery_code};
use std::io::Write;
use std::process::{Command, Stdio};

fn run(args: &[&str], stdin: &str) -> (i32, String) {
    let mut c = Command::new(env!("CARGO_BIN_EXE_ibis-key"))
        .args(args).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().unwrap();
    c.stdin.take().unwrap().write_all(stdin.as_bytes()).unwrap();
    let o = c.wait_with_output().unwrap();
    let text = String::from_utf8_lossy(&o.stdout).to_string() + &String::from_utf8_lossy(&o.stderr);
    (o.status.code().unwrap_or(-1), text)
}

#[test]
fn import_status_check_code_restore() {
    let d = std::env::temp_dir().join(format!("ibis_cli_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    let key = generate_key_hex();
    let code = recovery_code(&key).unwrap();
    let kf = d.join("key.txt");
    std::fs::write(&kf, format!("{}\n", key)).unwrap();
    let store = d.join("key.store");
    let s = store.to_str().unwrap();

    let (rc, out) = run(&["import", kf.to_str().unwrap(), "--path", s], "");
    assert_eq!(rc, 0, "{}", out);
    let (rc, out) = run(&["status", "--path", s], "");
    assert_eq!(rc, 0, "{}", out);
    assert!(out.contains("鍵の指紋") && out.contains("正しく開けました"), "{}", out);
    assert!(!out.contains(&key), "鍵そのものを表示しない");

    let (rc, out) = run(&["check-code", "--path", s], &format!("{}\n", code));
    assert_eq!(rc, 0, "{}", out);
    assert!(out.contains("一致しました"));
    let other = recovery_code(&generate_key_hex()).unwrap();
    let (rc, out) = run(&["check-code", "--path", s], &format!("{}\n", other));
    assert_eq!(rc, 1, "{}", out);

    // restore は、--force なしでは既存の保管ファイルを上書きしない
    let (rc, out) = run(&["restore", "--path", s], &format!("{}\n", other));
    assert_eq!(rc, 1);
    assert!(out.contains("すでにあります"), "{}", out);
    // init も上書きしない
    let (rc, _) = run(&["init", "--path", s], "");
    assert_eq!(rc, 1);
    // 使い方の表示
    let (rc, out) = run(&["--help"], "");
    assert_eq!(rc, 0);
    assert!(out.contains("check-code"));
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn init_saves_only_after_correct_retype() {
    let d = std::env::temp_dir().join(format!("ibis_cli_init_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    let store = d.join("new.store");
    let s = store.to_str().unwrap();
    // 間違ったコードを入力 → 保存しない
    let wrong = recovery_code(&generate_key_hex()).unwrap();
    let (rc, out) = run(&["init", "--path", s], &format!("{}\n", wrong));
    assert_eq!(rc, 1, "{}", out);
    assert!(!store.exists());
    let _ = std::fs::remove_dir_all(&d);
}
