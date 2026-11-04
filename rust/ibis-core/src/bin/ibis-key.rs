//! 鍵の保管（DPAPI）を操作するコマンド。Windows 10 実機での動作確認と、鍵の取り込み・復元に使う。
//! 鍵そのもの（64桁の16進数）は表示しない。復旧コードは、新しく作るとき（init）だけ表示する。
use ibis_core::keystore::{self, Protector};
use std::io::{self, BufRead, Write};
use std::path::PathBuf;
use std::process::exit;

const USAGE: &str = "使い方：
  ibis-key status     [--path 保管ファイル]                 保管した鍵の状態（保管方式・指紋）を表示する
  ibis-key init       [--path 保管ファイル]                 新しい鍵を作る（復旧コードを表示し、再入力で確認してから保存）
  ibis-key import     <key.txt> [--path 保管ファイル] [--force]  移行ツールで作った鍵ファイルを取り込んで保管する
  ibis-key restore    [--path 保管ファイル] [--force]       紙の復旧コードから鍵を復元して保管する
  ibis-key check-code [--path 保管ファイル]                 紙の復旧コードが、保管した鍵と一致するか確かめる
保管ファイルの既定の場所（Windows）：%LOCALAPPDATA%\\IntegratedBizSystem\\key.dpapi";

fn read_line(prompt: &str) -> String {
    print!("{}", prompt);
    let _ = io::stdout().flush();
    let mut s = String::new();
    let _ = io::stdin().lock().read_line(&mut s);
    s.trim().to_string()
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || args[0] == "-h" || args[0] == "--help" {
        println!("{}", USAGE);
        exit(if args.is_empty() { 2 } else { 0 });
    }
    let cmd = args[0].as_str();
    let mut path: Option<PathBuf> = None;
    let mut force = false;
    let mut pos: Vec<String> = Vec::new();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--path" if i + 1 < args.len() => { path = Some(PathBuf::from(&args[i + 1])); i += 1; }
            "--force" => force = true,
            a => pos.push(a.to_string()),
        }
        i += 1;
    }
    let path = match path.map(Ok).unwrap_or_else(keystore::default_path) {
        Ok(p) => p,
        Err(e) => { eprintln!("エラー：{}", e); exit(1); }
    };
    let p = keystore::platform_protector();
    if let Err(e) = run(cmd, &pos, &path, p.as_ref(), force) {
        eprintln!("エラー：{}", e);
        exit(1);
    }
}

fn run(cmd: &str, pos: &[String], path: &PathBuf, p: &dyn Protector, force: bool) -> Result<(), ibis_core::IbisError> {
    use ibis_core::IbisError;
    match cmd {
        "status" => {
            let (name, fp) = keystore::status(path, p)?;
            println!("保管ファイル：{}", path.display());
            println!("保管方式　　：{}", name);
            println!("鍵の指紋　　：{}（鍵の識別用。鍵そのものは分かりません）", fp);
            println!("鍵を正しく開けました。");
        }
        "init" => {
            if path.exists() {
                return Err(IbisError(format!("鍵の保管ファイルがすでにあります：{}（上書きしません）", path.display())));
            }
            let pending = keystore::begin_new_key()?;
            println!("新しい鍵を作りました（まだ保存していません）。");
            println!("次の復旧コードを、紙に正確に書き写してください。画面のスクリーンショットは撮らないでください。");
            println!();
            println!("  {}", pending.recovery_code());
            println!();
            let typed = read_line("紙に書き写したコードを、紙を見ながら入力してください：");
            keystore::save_new_key(path, p, &pending, &typed)?;
            println!("一致しました。鍵を保管しました：{}", path.display());
            println!("保管方式：{}／鍵の指紋：{}", p.name(), pending.fingerprint()?);
            println!("この画面は閉じてください（復旧コードが表示されたままになっています）。");
        }
        "import" => {
            let src = pos.first().ok_or_else(|| IbisError("取り込む鍵ファイルを指定してください。".into()))?;
            let fp = keystore::import_key_file(std::path::Path::new(src), path, p, force)?;
            println!("鍵ファイルを取り込み、保管しました：{}", path.display());
            println!("保管方式：{}／鍵の指紋：{}", p.name(), fp);
            println!("紙の復旧コードと一致するかを、check-code で確かめてください。");
        }
        "restore" => {
            let code = read_line("紙の復旧コードを入力してください：");
            let fp = keystore::restore_from_code(&code, path, p, force)?;
            println!("復旧コードから鍵を復元し、保管しました：{}", path.display());
            println!("保管方式：{}／鍵の指紋：{}", p.name(), fp);
        }
        "check-code" => {
            let code = read_line("紙の復旧コードを入力してください：");
            if keystore::check_code(path, p, &code)? {
                println!("一致しました。このコードで鍵を復元できます。");
            } else {
                return Err(IbisError("一致しません。コードの書き写しを確認してください。".into()));
            }
        }
        _ => {
            eprintln!("{}", USAGE);
            exit(2);
        }
    }
    Ok(())
}
