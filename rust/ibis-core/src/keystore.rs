//! 暗号鍵の保管（DPAPI）。
//!
//! Windowsでは、鍵を DPAPI（Windowsのログオン情報にひもづく暗号化。CurrentUser）で保護してファイルに保存する。
//! そのWindowsユーザーでログオンしているときだけ開ける。別のユーザー・別のPCでは開けず、ファイルを持ち出されても読めない。
//! ただし、同じユーザーとして動くプログラム（マルウェアを含む）からは守れない。PCが壊れたときなどは、紙の復旧コードから復元する。
//!
//! ファイルの形式：MAGIC(8) | 保護の種類(1) | 鍵の指紋(8) | 保護データの長さ(4, LE) | 保護データ
//! 鍵の指紋は SHA-256(鍵) の先頭8バイト。開いたあとの確認（壊れていないか、別の鍵でないか）と、画面での識別に使う。
//! 鍵そのもの（16進数）は、画面にもログにも出さない。

use crate::{generate_key_hex, normalize_key, parse_recovery_code, recovery_code, IbisError};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

type Result<T> = std::result::Result<T, IbisError>;

pub const MAGIC: &[u8; 8] = b"IBISKEY1";
/// 保護の種類
pub const KIND_PLAIN: u8 = 0; // 保護なし（Windows以外での開発・検証用）
pub const KIND_DPAPI: u8 = 1; // Windows DPAPI（CurrentUser）
pub const KIND_TEST: u8 = 255; // テスト専用

/// 鍵を保護・復元する仕組み
pub trait Protector {
    fn kind(&self) -> u8;
    fn name(&self) -> &'static str;
    fn protect(&self, plain: &[u8]) -> Result<Vec<u8>>;
    fn unprotect(&self, blob: &[u8]) -> Result<Vec<u8>>;
}

/// 保護なし（Windows以外での開発・検証用）。ファイルの権限で守る（Linux・macOSでは本人だけが読める）
pub struct PlainProtector;
impl Protector for PlainProtector {
    fn kind(&self) -> u8 {
        KIND_PLAIN
    }
    fn name(&self) -> &'static str {
        "保護なし（Windows以外の開発用。ファイルの権限で守る）"
    }
    fn protect(&self, plain: &[u8]) -> Result<Vec<u8>> {
        Ok(plain.to_vec())
    }
    fn unprotect(&self, blob: &[u8]) -> Result<Vec<u8>> {
        Ok(blob.to_vec())
    }
}

#[cfg(any(windows, feature = "dpapi-typecheck"))]
pub mod dpapi {
    use super::{Protector, Result, KIND_DPAPI};
    use crate::IbisError;
    use windows_sys::Win32::Foundation::{LocalFree, HLOCAL};
    use windows_sys::Win32::Security::Cryptography::{
        CryptProtectData, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };

    /// 追加のエントロピー。このアプリが保護したデータを、ほかのアプリが同じ手順で偶然開くことを防ぐ（秘密ではない）
    const ENTROPY: &[u8] = b"IntegratedBizSystem/ibis-core/key/v1";

    pub struct Dpapi;

    fn blob(d: &[u8]) -> CRYPT_INTEGER_BLOB {
        CRYPT_INTEGER_BLOB { cbData: d.len() as u32, pbData: d.as_ptr() as *mut u8 }
    }

    /// DPAPIが確保したメモリを、ゼロで消してから解放する
    unsafe fn take_and_free(out: &CRYPT_INTEGER_BLOB) -> Vec<u8> {
        let v = std::slice::from_raw_parts(out.pbData, out.cbData as usize).to_vec();
        std::ptr::write_bytes(out.pbData, 0, out.cbData as usize);
        LocalFree(out.pbData as HLOCAL);
        v
    }

    impl Protector for Dpapi {
        fn kind(&self) -> u8 {
            KIND_DPAPI
        }
        fn name(&self) -> &'static str {
            "Windows DPAPI（このWindowsユーザーでだけ開ける）"
        }
        fn protect(&self, plain: &[u8]) -> Result<Vec<u8>> {
            let input = blob(plain);
            let ent = blob(ENTROPY);
            let mut out = CRYPT_INTEGER_BLOB { cbData: 0, pbData: std::ptr::null_mut() };
            let ok = unsafe {
                CryptProtectData(&input, std::ptr::null(), &ent, std::ptr::null(), std::ptr::null(),
                                 CRYPTPROTECT_UI_FORBIDDEN, &mut out)
            };
            if ok == 0 {
                return Err(IbisError(format!(
                    "DPAPIで鍵を保護できませんでした（{}）。", std::io::Error::last_os_error())));
            }
            Ok(unsafe { take_and_free(&out) })
        }
        fn unprotect(&self, data: &[u8]) -> Result<Vec<u8>> {
            let input = blob(data);
            let ent = blob(ENTROPY);
            let mut out = CRYPT_INTEGER_BLOB { cbData: 0, pbData: std::ptr::null_mut() };
            let ok = unsafe {
                CryptUnprotectData(&input, std::ptr::null_mut(), &ent, std::ptr::null(), std::ptr::null(),
                                   CRYPTPROTECT_UI_FORBIDDEN, &mut out)
            };
            if ok == 0 {
                return Err(IbisError(format!(
                    "鍵を開けませんでした。別のWindowsユーザーや別のPCで保管された鍵か、ファイルが壊れています（{}）。\
                     紙の復旧コードから復元してください。", std::io::Error::last_os_error())));
            }
            Ok(unsafe { take_and_free(&out) })
        }
    }
}

/// この環境で使う保護の仕組み（WindowsではDPAPI）
pub fn platform_protector() -> Box<dyn Protector> {
    #[cfg(windows)]
    {
        Box::new(dpapi::Dpapi)
    }
    #[cfg(not(windows))]
    {
        Box::new(PlainProtector)
    }
}

/// 既定の保管場所。Windows：%LOCALAPPDATA%\IntegratedBizSystem\key.dpapi
pub fn default_path() -> Result<PathBuf> {
    #[cfg(windows)]
    {
        let base = std::env::var_os("LOCALAPPDATA")
            .ok_or_else(|| IbisError("LOCALAPPDATA が見つかりません。".into()))?;
        Ok(PathBuf::from(base).join("IntegratedBizSystem").join("key.dpapi"))
    }
    #[cfg(not(windows))]
    {
        let base = std::env::var_os("XDG_DATA_HOME").map(PathBuf::from).or_else(|| {
            std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local").join("share"))
        });
        let base = base.ok_or_else(|| IbisError("保管場所（HOME）が見つかりません。".into()))?;
        Ok(base.join("integrated-biz-system").join("key.plain"))
    }
}

/// 鍵の指紋（SHA-256の先頭8バイト。4桁ずつ区切った16進数）。鍵の識別用で、鍵そのものは分からない
pub fn fingerprint(key_hex: &str) -> Result<String> {
    let raw = key_bytes(key_hex)?;
    let d = Sha256::digest(&raw[..]);
    let h = hex::encode(&d[..8]);
    Ok(h.as_bytes().chunks(4).map(|c| std::str::from_utf8(c).unwrap()).collect::<Vec<_>>().join("-"))
}

fn key_bytes(key_hex: &str) -> Result<Vec<u8>> {
    let k = normalize_key(key_hex)?;
    hex::decode(k).map_err(|_| IbisError("鍵の形式が正しくありません。".into()))
}

/// メモリ上の秘密（鍵の文字列など）を、使い終わったらゼロで消す
fn wipe(buf: &mut Vec<u8>) {
    for b in buf.iter_mut() {
        unsafe { std::ptr::write_volatile(b, 0) };
    }
}

/// 新しく作った、まだ保存していない鍵。復旧コードを表示し、再入力で一致したときだけ保存する
pub struct PendingKey {
    key_hex: String,
    code: String,
}
impl PendingKey {
    pub fn recovery_code(&self) -> &str {
        &self.code
    }
    pub fn fingerprint(&self) -> Result<String> {
        fingerprint(&self.key_hex)
    }
}
impl Drop for PendingKey {
    fn drop(&mut self) {
        let mut a = std::mem::take(&mut self.key_hex).into_bytes();
        let mut b = std::mem::take(&mut self.code).into_bytes();
        wipe(&mut a);
        wipe(&mut b);
    }
}

/// 鍵を新しく作る（まだ保存しない）
pub fn begin_new_key() -> Result<PendingKey> {
    let key_hex = generate_key_hex();
    let code = recovery_code(&key_hex)?;
    Ok(PendingKey { key_hex, code })
}

/// 再入力された復旧コードが、この鍵と一致するか
fn code_matches(key_hex: &str, typed: &str) -> Result<bool> {
    Ok(parse_recovery_code(typed)? == normalize_key(key_hex)?)
}

/// 新しい鍵を、再入力による確認のうえで保存する（既存の保管ファイルは上書きしない）
pub fn save_new_key(path: &Path, p: &dyn Protector, pending: &PendingKey, typed_code: &str) -> Result<()> {
    if !code_matches(&pending.key_hex, typed_code)? {
        return Err(IbisError("入力された復旧コードが一致しません。紙に書き写したコードを確認してください。".into()));
    }
    write_store(path, p, &pending.key_hex, false)
}

/// Pythonの移行ツールで作った鍵ファイル（64桁の16進数）を取り込んで保管する。指紋を返す
pub fn import_key_file(key_file: &Path, path: &Path, p: &dyn Protector, overwrite: bool) -> Result<String> {
    let mut text = fs::read(key_file)
        .map_err(|e| IbisError(format!("鍵ファイルを読めませんでした（{}）。", e)))?;
    let r = std::str::from_utf8(&text)
        .map_err(|_| IbisError("鍵ファイルの文字コードが正しくありません。".into()))
        .and_then(normalize_key);
    wipe(&mut text);
    let key_hex = r?;
    write_store(path, p, &key_hex, overwrite)?;
    fingerprint(&key_hex)
}

/// 紙の復旧コードから鍵を復元して保管する（PCの入れ替え・再インストールの後など）。指紋を返す
pub fn restore_from_code(code: &str, path: &Path, p: &dyn Protector, overwrite: bool) -> Result<String> {
    let key_hex = parse_recovery_code(code)?;
    write_store(path, p, &key_hex, overwrite)?;
    fingerprint(&key_hex)
}

/// 保管した鍵を開く（64桁の16進数を返す。画面やログには出さないこと）
pub fn load_key(path: &Path, p: &dyn Protector) -> Result<String> {
    let data = fs::read(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            IbisError(format!("鍵の保管ファイルがありません：{}", path.display()))
        } else {
            IbisError(format!("鍵の保管ファイルを読めませんでした（{}）。", e))
        }
    })?;
    if data.len() < 21 || &data[..8] != MAGIC {
        return Err(IbisError("鍵の保管ファイルの形式が正しくありません。".into()));
    }
    let kind = data[8];
    if kind != p.kind() {
        return Err(IbisError(format!(
            "鍵の保管方式が違います（ファイル：{}、この環境：{}）。", kind_name(kind), p.name())));
    }
    let fp: [u8; 8] = data[9..17].try_into().unwrap();
    let len = u32::from_le_bytes(data[17..21].try_into().unwrap()) as usize;
    if data.len() != 21 + len {
        return Err(IbisError("鍵の保管ファイルが壊れています（長さが合いません）。".into()));
    }
    let mut raw = p.unprotect(&data[21..])?;
    if raw.len() != 32 || Sha256::digest(&raw[..])[..8] != fp {
        wipe(&mut raw);
        return Err(IbisError("鍵の保管ファイルが壊れているか、別の鍵です（指紋が一致しません）。".into()));
    }
    let key_hex = hex::encode(&raw);
    wipe(&mut raw);
    Ok(key_hex)
}

/// 紙の復旧コードが、保管した鍵と一致するか確かめる
pub fn check_code(path: &Path, p: &dyn Protector, code: &str) -> Result<bool> {
    let key_hex = load_key(path, p)?;
    code_matches(&key_hex, code)
}

/// 保管ファイルの状態（鍵を開いて確かめる）。保管方式と指紋を返す
pub fn status(path: &Path, p: &dyn Protector) -> Result<(String, String)> {
    let key_hex = load_key(path, p)?;
    Ok((p.name().to_string(), fingerprint(&key_hex)?))
}

pub fn kind_name(kind: u8) -> &'static str {
    match kind {
        KIND_PLAIN => "保護なし",
        KIND_DPAPI => "Windows DPAPI",
        KIND_TEST => "テスト用",
        _ => "不明",
    }
}

/// 保管ファイルを書く。一時ファイルに書いてから置き換える（途中で止まっても、壊れたファイルを残さない）
fn write_store(path: &Path, p: &dyn Protector, key_hex: &str, overwrite: bool) -> Result<()> {
    if path.exists() && !overwrite {
        return Err(IbisError(format!(
            "鍵の保管ファイルがすでにあります：{}（上書きしません。別の鍵で暗号化したDBが開けなくなるためです）", path.display())));
    }
    let mut raw = key_bytes(key_hex)?;
    let fp = Sha256::digest(&raw[..]);
    let blob = p.protect(&raw);
    wipe(&mut raw);
    let blob = blob?;
    let mut out = Vec::with_capacity(21 + blob.len());
    out.extend_from_slice(MAGIC);
    out.push(p.kind());
    out.extend_from_slice(&fp[..8]);
    out.extend_from_slice(&(blob.len() as u32).to_le_bytes());
    out.extend_from_slice(&blob);

    if let Some(dir) = path.parent() {
        if !dir.as_os_str().is_empty() {
            fs::create_dir_all(dir).map_err(|e| IbisError(format!("保管場所のフォルダを作れませんでした（{}）。", e)))?;
        }
    }
    let tmp = path.with_extension("tmp");
    let _ = fs::remove_file(&tmp);
    {
        let mut opts = fs::OpenOptions::new();
        opts.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut f = opts.open(&tmp).map_err(|e| IbisError(format!("鍵の保管ファイルを作れませんでした（{}）。", e)))?;
        f.write_all(&out).and_then(|_| f.sync_all())
            .map_err(|e| IbisError(format!("鍵の保管ファイルを書けませんでした（{}）。", e)))?;
    }
    fs::rename(&tmp, path).map_err(|e| {
        let _ = fs::remove_file(&tmp);
        IbisError(format!("鍵の保管ファイルを置き換えられませんでした（{}）。", e))
    })?;
    Ok(())
}
