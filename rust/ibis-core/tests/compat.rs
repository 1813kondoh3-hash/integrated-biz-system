//! Python参照実装との互換性テスト。
//! 環境変数で、Python側が用意したファイルを受け取る（無ければ該当テストはスキップ）:
//!   IBIS_PY_DB / IBIS_PY_KEY      … Pythonで作った暗号化DBと鍵（16進）
//!   IBIS_PY_GRAND_SUM             … そのDBの総合計の合計（Pythonで計算）
//!   IBIS_PY_CODE / IBIS_PY_CODE_KEY … Pythonで作った復旧コードと、その鍵
//!   IBIS_RS_OUT                   … Rustで作ったDBの出力先（Pythonが開いて検証する）
use ibis_core::*;

fn env(k: &str) -> Option<String> {
    std::env::var(k).ok().filter(|s| !s.is_empty())
}
fn tmp(name: &str) -> String {
    let d = std::env::temp_dir().join(format!("ibis_rs_{}_{}", std::process::id(), name));
    let _ = std::fs::remove_file(&d);
    d.to_string_lossy().to_string()
}

#[test]
fn opens_python_made_encrypted_db_and_matches_totals() {
    let (Some(db), Some(key), Some(sum)) = (env("IBIS_PY_DB"), env("IBIS_PY_KEY"), env("IBIS_PY_GRAND_SUM")) else { eprintln!("skip"); return };
    let conn = open(&db, Some(&key)).expect("Pythonで作った暗号化DBを、Rustで開ける");
    assert_eq!(health_problems(&conn, true).unwrap(), Vec::<String>::new(), "整合性・外部キー・暗号化ページ・操作ログの連鎖");
    let totals = invoice_totals(&conn).unwrap();
    assert!(totals.len() >= 5);
    let got: f64 = totals.iter().map(|t| t.3).sum();
    let exp: f64 = sum.parse().unwrap();
    assert!((got - exp).abs() < 1e-6, "総合計の合計が一致（Python {exp} / Rust {got}）");
    // 発行済みの請求書はRustからも削除できない（トリガーが働く）
    assert!(conn.execute("DELETE FROM invoices WHERE issued_at IS NOT NULL", []).is_err());
    // 操作ログへRustから追記すると、Pythonと同じ形式で連鎖する
    let (seq, _) = append_audit(&conn, "2026-11-03T00:00:00Z", "Rustからの追記").unwrap();
    assert!(seq > 1);
    assert_eq!(verify_audit_chain(&conn).unwrap(), None);
}

#[test]
fn key_handling_matches_python() {
    let (Some(db), Some(key)) = (env("IBIS_PY_DB"), env("IBIS_PY_KEY")) else { eprintln!("skip"); return };
    assert!(open(&db, None).is_err(), "鍵なしでは開けない");
    assert!(open(&db, Some(&generate_key_hex())).is_err(), "違う鍵では開けない");
    assert!(open(&db, Some("zzzz")).is_err(), "鍵の形式が不正");
    assert!(open(&db, Some(&key.to_uppercase())).is_ok(), "大文字の鍵も受け付ける");
}

#[test]
fn recovery_code_is_compatible_both_ways() {
    // Python → Rust
    if let (Some(code), Some(key)) = (env("IBIS_PY_CODE"), env("IBIS_PY_CODE_KEY")) {
        assert_eq!(parse_recovery_code(&code).unwrap(), key);
        assert_eq!(recovery_code(&key).unwrap(), code, "Rustが同じコードを作る");
    }
    // Rust → Python（出力して、Python側で読み戻す）
    let k = generate_key_hex();
    let c = recovery_code(&k).unwrap();
    assert_eq!(parse_recovery_code(&c).unwrap(), k);
    assert_eq!(parse_recovery_code(&c.to_lowercase().replace('-', " ")).unwrap(), k);
    assert!(parse_recovery_code(&format!("{}A", &c[..c.len() - 1])).is_err() || c.ends_with('A'));
    // 手書きの取り違え（0↔O、8↔B、1↔I・L）を読み替えて受け付ける
    for _ in 0..200 {
        let k2 = generate_key_hex();
        let c2 = recovery_code(&k2).unwrap();
        assert_eq!(parse_recovery_code(&c2.replace('O', "0").replace('B', "8")).unwrap(), k2);
        if c2.matches(|ch| ch == 'I' || ch == 'L').count() <= 4 {
            // 1 は I か L のどちらか：一意に決まらないときはエラー。別の鍵を受け付けることはない
            match parse_recovery_code(&c2.replace('I', "1").replace('L', "1")) {
                Ok(got) => assert_eq!(got, k2),
                Err(e) => assert!(e.0.contains("判別できません"), "{}", e.0),
            }
        }
    }
    // Pythonで作ったコードの手書き取り違えも、Rustで読める
    if let (Some(code), Some(key)) = (env("IBIS_PY_CODE"), env("IBIS_PY_CODE_KEY")) {
        assert_eq!(parse_recovery_code(&code.replace('O', "0").replace('B', "8")).unwrap(), key);
    }
    if let Some(out) = env("IBIS_RS_OUT") {
        std::fs::write(format!("{out}.code"), format!("{c}\n{k}\n")).unwrap();
    }
}

#[test]
fn rust_made_encrypted_db_works_and_is_readable_by_python() {
    let path = tmp("rust_enc.db");
    let key = generate_key_hex();
    let conn = open(&path, Some(&key)).unwrap();
    init_schema(&conn).unwrap();
    conn.execute("INSERT INTO company(id,name) VALUES(1,'株式会社テスト')", []).unwrap();
    conn.execute("INSERT INTO tax_rates(id,sort_order,label,rate,effective_from) VALUES('t1',0,'標準',10,'2019-10-01')", []).unwrap();
    conn.execute("INSERT INTO invoices(id,month,seq,no,issue_date,tax_rate_id,status) VALUES('i1','2026-11',0,'KD202611-001','2026-11-03','t1','未収')", []).unwrap();
    conn.execute("INSERT INTO invoice_items(invoice_id,line_no,site,qty,price) VALUES('i1',1,'現場A','1','705')", []).unwrap();
    // 発行（確定）：内容は変更できなくなる
    conn.execute("UPDATE invoices SET issued_at='2026-11-03T00:00:00Z', issue_hash=? WHERE id='i1'", [&"a".repeat(64)]).unwrap();
    assert!(conn.execute("UPDATE invoice_items SET price='1' WHERE invoice_id='i1'", []).is_err(), "発行後の明細は変更不可");
    assert!(conn.execute("DELETE FROM invoices WHERE id='i1'", []).is_err(), "発行後の削除は不可");
    conn.execute("INSERT INTO payments(invoice_id,line_no,pay_date,amount) VALUES('i1',1,'2026-11-30','300')", []).unwrap();   // 入金は可
    // 705円×10% = 70.5 → 71円（四捨五入）／総合計776／残額476
    let t = invoice_totals(&conn).unwrap();
    assert_eq!(t[0].2, 71.0);
    assert_eq!((t[0].3, t[0].5), (776.0, 476.0));
    // 操作ログ：追記専用
    for (i, a) in ["作成", "発行", "入金"].iter().enumerate() {
        append_audit(&conn, &format!("2026-11-03T00:00:0{i}Z"), a).unwrap();
    }
    assert!(conn.execute("UPDATE audit_log SET action='x'", []).is_err());
    assert!(conn.execute("DELETE FROM audit_log", []).is_err());
    assert!(conn.execute("INSERT INTO audit_log VALUES(99,'t','a','0000000000000000000000000000000000000000000000000000000000000000',?)", [&"b".repeat(64)]).is_err());
    assert_eq!(health_problems(&conn, true).unwrap(), Vec::<String>::new());
    // バックアップ（暗号化は引き継がれ、同じ鍵で開ける）
    let bak = tmp("rust_bak.db");
    backup_into(&conn, &bak).unwrap();
    let raw = std::fs::read(&bak).unwrap();
    assert_ne!(&raw[..15], b"SQLite format 3");
    assert!(!raw.windows("株式会社テスト".len()).any(|w| w == "株式会社テスト".as_bytes()), "バックアップに平文が無い");
    let b = open(&bak, Some(&key)).unwrap();
    assert_eq!(health_problems(&b, true).unwrap(), Vec::<String>::new());
    drop(conn);
    if let Some(out) = env("IBIS_RS_OUT") {
        std::fs::copy(&path, &out).unwrap();
        std::fs::write(format!("{out}.key"), &key).unwrap();
    }
}
