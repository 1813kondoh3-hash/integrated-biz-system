//! 鍵の保管（keystore）のテスト。保管の仕組みは全OSで、DPAPIそのものはWindowsでだけ確かめる
use ibis_core::keystore::{self, Protector, KIND_TEST};
use ibis_core::{generate_key_hex, recovery_code, IbisError};
use std::fs;
use std::path::PathBuf;

/// テスト用の保護：鍵のバイト列が、そのままファイルに現れないようにする（可逆）
struct TestProtector;
impl Protector for TestProtector {
    fn kind(&self) -> u8 { KIND_TEST }
    fn name(&self) -> &'static str { "テスト用" }
    fn protect(&self, plain: &[u8]) -> Result<Vec<u8>, IbisError> {
        Ok(plain.iter().enumerate().map(|(i, b)| b ^ 0xA5 ^ (i as u8)).collect())
    }
    fn unprotect(&self, blob: &[u8]) -> Result<Vec<u8>, IbisError> { self.protect(blob) }
}

fn tmpdir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("ibis_keystore_{}_{}", name, std::process::id()));
    let _ = fs::remove_dir_all(&d);
    fs::create_dir_all(&d).unwrap();
    d
}

fn contains(hay: &[u8], needle: &[u8]) -> bool {
    hay.windows(needle.len()).any(|w| w == needle)
}

#[test]
fn new_key_requires_matching_recovery_code_and_never_overwrites() {
    let d = tmpdir("new");
    let path = d.join("sub").join("key.store");
    let p = TestProtector;
    let pending = keystore::begin_new_key().unwrap();
    // 違うコード → 保存しない
    let other = recovery_code(&generate_key_hex()).unwrap();
    assert!(keystore::save_new_key(&path, &p, &pending, &other).is_err());
    assert!(!path.exists(), "一致しないときはファイルを作らない");
    // 正しいコード（区切りなし・小文字でも可）→ 保存
    let typed = pending.recovery_code().replace('-', "").to_lowercase();
    keystore::save_new_key(&path, &p, &pending, &typed).unwrap();
    assert!(path.exists());
    let key = keystore::load_key(&path, &p).unwrap();
    assert_eq!(keystore::fingerprint(&key).unwrap(), pending.fingerprint().unwrap());
    assert!(keystore::check_code(&path, &p, pending.recovery_code()).unwrap());
    // 2回目は上書きしない
    let again = keystore::begin_new_key().unwrap();
    let e = keystore::save_new_key(&path, &p, &again, again.recovery_code()).unwrap_err();
    assert!(e.0.contains("すでにあります"));
    assert_eq!(keystore::load_key(&path, &p).unwrap(), key, "元の鍵のまま");
    // ファイルに鍵（16進数・生のバイト列）が現れない
    let data = fs::read(&path).unwrap();
    assert!(!contains(&data, key.as_bytes()));
    assert!(!contains(&data, &hex::decode(&key).unwrap()));
    assert!(!d.join("sub").join("key.tmp").exists(), "一時ファイルを残さない");
}

#[test]
fn import_python_key_file_and_restore_from_code() {
    let d = tmpdir("import");
    let p = TestProtector;
    let key = generate_key_hex();
    let kf = d.join("key.txt");
    fs::write(&kf, format!("{}\n", key.to_uppercase())).unwrap(); // 移行ツールの形式（大文字でも可）
    let store = d.join("key.store");
    let fp = keystore::import_key_file(&kf, &store, &p, false).unwrap();
    assert_eq!(keystore::load_key(&store, &p).unwrap(), key);
    assert_eq!(fp, keystore::fingerprint(&key).unwrap());
    // 既存の保管ファイルは、--force（overwrite）なしでは上書きしない
    assert!(keystore::import_key_file(&kf, &store, &p, false).is_err());
    // 復旧コードから復元（別の場所へ）
    let code = recovery_code(&key).unwrap();
    let store2 = d.join("restored.store");
    keystore::restore_from_code(&code, &store2, &p, false).unwrap();
    assert_eq!(keystore::load_key(&store2, &p).unwrap(), key);
    // 1文字の書き間違いは、復元できない（検査文字）
    let mut bad: Vec<char> = code.chars().collect();
    let i = bad.iter().position(|c| c.is_ascii_alphabetic()).unwrap();
    bad[i] = if bad[i] == 'A' { 'C' } else { 'A' };
    let bad: String = bad.into_iter().collect();
    assert!(keystore::restore_from_code(&bad, &d.join("x.store"), &p, false).is_err());
    // 不正な鍵ファイルは取り込まない
    fs::write(d.join("bad.txt"), "not-a-key\n").unwrap();
    assert!(keystore::import_key_file(&d.join("bad.txt"), &d.join("y.store"), &p, false).is_err());
    assert!(!d.join("y.store").exists());
}

#[test]
fn detects_corruption_wrong_kind_and_missing_file() {
    let d = tmpdir("corrupt");
    let p = TestProtector;
    let store = d.join("key.store");
    keystore::restore_from_code(&recovery_code(&generate_key_hex()).unwrap(), &store, &p, false).unwrap();
    let good = fs::read(&store).unwrap();
    // 保護データの1バイトを変える → 指紋が合わない
    let mut bad = good.clone();
    let last = bad.len() - 1;
    bad[last] ^= 0x01;
    fs::write(&store, &bad).unwrap();
    assert!(keystore::load_key(&store, &p).unwrap_err().0.contains("指紋"));
    // 途中で切れたファイル
    fs::write(&store, &good[..good.len() - 3]).unwrap();
    assert!(keystore::load_key(&store, &p).is_err());
    // 別の保管方式で開こうとする
    fs::write(&store, &good).unwrap();
    assert!(keystore::load_key(&store, &keystore::PlainProtector).unwrap_err().0.contains("保管方式が違います"));
    // ファイルが無い
    assert!(keystore::load_key(&d.join("none.store"), &p).unwrap_err().0.contains("ありません"));
}

// Windowsでだけ実行する。Linuxでは cargo check --tests --features dpapi-typecheck で型の検査だけ行う
#[cfg(any(windows, feature = "dpapi-typecheck"))]
mod windows_dpapi {
    use super::*;
    use ibis_core::keystore::{dpapi::Dpapi, KIND_DPAPI};

    #[test]
    fn dpapi_roundtrip_and_tamper_detection() {
        let p = Dpapi;
        let secret = hex::decode(generate_key_hex()).unwrap();
        let blob = p.protect(&secret).unwrap();
        assert!(blob.len() > secret.len());
        assert!(!contains(&blob, &secret), "保護データに鍵が現れない");
        assert_eq!(p.unprotect(&blob).unwrap(), secret);
        let mut bad = blob.clone();
        let mid = bad.len() / 2;
        bad[mid] ^= 0xFF;
        assert!(p.unprotect(&bad).is_err(), "改ざんされたデータは開けない");
    }

    #[test]
    fn dpapi_store_end_to_end() {
        let d = tmpdir("dpapi");
        let p = keystore::platform_protector();
        assert_eq!(p.kind(), KIND_DPAPI);
        let pending = keystore::begin_new_key().unwrap();
        let store = d.join("key.dpapi");
        keystore::save_new_key(&store, p.as_ref(), &pending, pending.recovery_code()).unwrap();
        let key = keystore::load_key(&store, p.as_ref()).unwrap();
        let data = fs::read(&store).unwrap();
        assert!(!contains(&data, key.as_bytes()));
        assert!(!contains(&data, &hex::decode(&key).unwrap()));
        assert!(keystore::check_code(&store, p.as_ref(), pending.recovery_code()).unwrap());
        let dp = keystore::default_path().unwrap();
        assert!(dp.ends_with("IntegratedBizSystem\\key.dpapi"), "{}", dp.display());
    }
}
