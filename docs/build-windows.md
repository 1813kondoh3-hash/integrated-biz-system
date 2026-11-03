# Windows 10 ビルド手順書

Rustのデータ層（`rust/ibis-core`）を Windows 10 でビルドし、暗号化DB（SQLCipher）が動くことを確認するための手順です。
Tauriアプリ化（次の段階）の土台でもあります。

> **この手順書の状態**：Linux上での検証結果と公式資料に基づいて作成しました。**Windows実機では未検証です**（第9章）。
> うまくいかない箇所があれば、`tools\collect-report.ps1` の出力（`env-report.txt`）を共有してください。環境変数・パスワード・鍵・データは含まれません（ユーザー名は `<user>` に置き換わります）。

---

## 0. この手順で確認すること
1. 必要なツールが入っているか（`tools\check-env.ps1`）
2. Rustのデータ層が、OpenSSLとSQLCipherを含めてビルドでき、テストが通るか
3. （任意）DBのテスト・アプリのテストが通るか

所要時間の目安：ツールのインストール 30〜60分（回線による）。Rustの初回ビルドは、Linuxでは約7分でした（Windowsでは同程度〜数倍の可能性があります）。

## 1. 事前に確認すること
- Windows 10 22H2（Windows 10 の通常のセキュリティ更新は2025年10月14日に終了していますが、WebView2とEdgeは少なくとも2028年10月まで更新される旨をMicrosoftが表明しています）
- 管理者の権限（Visual Studio Build Tools のインストールに必要）
- インターネット接続、ディスクの空き容量（目安：10GB以上。要確認）
- ウイルス対策ソフトが、ビルドを遅くしたり止めたりすることがあります（第6章）

## 2. ツールのインストール
PowerShell を開いて、上から順に実行します。**各ステップのあとは、PowerShell を開き直してください**（PATHが反映されます）。
winget のIDは変わることがあります。見つからないときは `winget search 名前` で探してください。

| 手順 | ツール | コマンド | 備考 |
|---|---|---|---|
| 2-1 | Git | `winget install --id Git.Git -e` | |
| 2-2 | C++ Build Tools | `winget install --id Microsoft.VisualStudio.2022.BuildTools -e --override "--passive --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"` | Tauri公式の指定は「Desktop development with C++」。管理者権限が必要。大きいので時間がかかる |
| 2-3 | Rust | `winget install --id Rustlang.Rustup -e` → 開き直して `rustup default stable-msvc` | MSVC版のツールチェーンを使う（Tauri公式の指定） |
| 2-4 | Perl | `winget install --id StrawberryPerl.StrawberryPerl -e` | OpenSSLをソースからビルドするため |
| 2-5 | NASM | `winget install --id NASM.NASM -e` → `C:\Program Files\NASM` を PATH に追加 | 同上。PATHへの追加は、システムのプロパティ → 環境変数 |
| 2-6 | Python（任意） | `winget install --id Python.Python.3.12 -e` | DBのテスト用 |
| 2-7 | Node.js（任意） | `winget install --id OpenJS.NodeJS.LTS -e` | アプリのテスト用。**22.22.2 以上、または 24.15 以上**が必要（テストに使う jsdom の要件）。古い版だと、テストの開始前に `Cannot read properties of undefined (reading 'bind')` で止まる |
| 2-8 | WebView2 | 通常は不要 | Windows 10（1803以降）は標準で入っているとされます。Tauri化の段階で必要。無ければ Microsoft の「WebView2 ランタイム（Evergreen Bootstrapper）」を入れる |

> OpenSSL を Perl・NASM でビルドする必要があるかは、Windowsでの実際のビルドで確定します（第9章）。
> 失敗する場合は、第7章の代替手順（vcpkg）を試します。

## 3. 環境を確認する
```powershell
powershell -ExecutionPolicy Bypass -File tools\check-env.ps1
```
- `[OK]` 問題なし／`[NG]` 必須なのに不足（「→」の行に、入れ方が出ます）／`[--]` 任意
- 必須がすべて `[OK]` になったら、次へ進みます

## 4. ソースを取得する
```powershell
git clone https://github.com/<アカウント>/integrated-biz-system.git
cd integrated-biz-system
powershell -ExecutionPolicy Bypass -File tools\install-hooks.ps1     # コミット前の秘密情報チェックを有効に
```

## 5. ビルドとテスト
### 5-1. まとめて実行（おすすめ）
```powershell
powershell -ExecutionPolicy Bypass -File tools\build-core.ps1
```
Rustのテスト（OpenSSLをソースから同梱）と、DBのテストを実行し、結果を `build-report.txt` に保存します。**初回は時間がかかります。**

**期待する結果**
```
== cargo test --features vendored-openssl ==
test key_handling_matches_python ... ok
test opens_python_made_encrypted_db_and_matches_totals ... ok
test recovery_code_is_compatible_both_ways ... ok
test rust_made_encrypted_db_works_and_is_readable_by_python ... ok
test result: ok. 4 passed; 0 failed; ...
== python db\tests\run_tests.py ==
Ran 34 tests ...
OK
結果: 合格
```
Pythonの鍵を使うテスト（`key_handling…`、`opens_python_made…`）は、環境変数が無い場合は「skip」として合格扱いになります。本当の相互確認は 5-4 で行います。

### 5-2. Rust だけを手で実行する
```powershell
cd rust\ibis-core
cargo test --features vendored-openssl
```

### 5-3. DBのテスト（Python）
```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r db\requirements.txt
python db\tests\run_tests.py              # 34件 OK
python db\tests\test_encryption.py        # 17件 OK
```
`sqlcipher3-wheels` が Windows 用に提供されていない場合、`pip install` が失敗します。その場合、暗号化のテストは Rust 側で確認できているので、`run_tests.py` だけで構いません。

### 5-4. Python と Rust の相互確認（5-3 が通った場合）
```powershell
$env:IBIS_CARGO_FEATURES = "vendored-openssl"
python db\tests\cross_check.py            # OK
```

### 5-5. アプリのテスト（任意）
```powershell
cd app
npm ci
npm test                                  # すべて合格
```

### 5-6. 結果の収集
```powershell
powershell -ExecutionPolicy Bypass -File tools\collect-report.ps1
```
作られた `env-report.txt` の内容を確認して、共有してください。

## 6. うまくいかないとき
| 症状 | 原因と対処 |
|---|---|
| `link.exe` や `cl.exe` が見つからない | C++ Build Tools の「C++によるデスクトップ開発」が入っていない（2-2）。入れたあと PowerShell を開き直す。「x64 Native Tools Command Prompt for VS 2022」から実行すると通る場合がある |
| `perl` または `nasm` が見つからない（OpenSSLのビルド中） | 2-4・2-5。NASM の PATH への追加を忘れやすい。開き直しても反映されなければ、再ログインする |
| OpenSSL のビルドが別の理由で失敗する | 第7章の代替手順（vcpkg）を試す。`env-report.txt` を共有する |
| `rustc` が見つからない | 2-3のあと PowerShell を開き直していない |
| `-ExecutionPolicy` のエラー | 例のとおり `-ExecutionPolicy Bypass` を付けて実行する |
| 「パスが長すぎる」 | `git config --system core.longpaths true`（管理者）。リポジトリを短いパス（例 `C:\work\ibis`）に置く |
| ビルドが極端に遅い・ファイルが消える | ウイルス対策ソフトが監視している可能性。リポジトリと `target` フォルダを除外に追加する（会社のポリシーに従う。要確認） |
| テストが `UnicodeEncodeError: 'charmap' codec can't encode` で落ちる | Pythonの出力の文字コードが、日本語を表せない既定値になっている。`$env:PYTHONUTF8 = "1"` を設定してから実行する（`tools\build-core.ps1` は自動で設定する。GitHubのWindows環境で実際に起きた） |
| 移行ツールで暗号化DBを扱うと「Windowsでは、このツールで暗号化DBを扱えません」と出る | 仕様です。Windows版のPython用暗号化ライブラリ（`sqlcipher3-wheels`）は、暗号化DBにスキーマを作る処理でクラッシュする（GitHubのWindows環境で確認。作業領域を128MBに広げても再現）。Windowsの暗号化DBは、動作を確認済みのRust側（アプリ）で扱う。暗号化なしの移行・確認、鍵の作成、復旧コードの控え・確認は、Windowsでもこのツールで使える |
| `pip install` で `sqlcipher3-wheels` が入らない | Windows用の配布が無い可能性。Pythonの暗号化テストは省略し、Rustのテストで確認する |

## 7. 代替手順：vcpkg の OpenSSL を使う（要検証）
OpenSSL のソースビルドが通らないときの、予備の手順です。`bundled-sqlcipher`（vendoredなし）は、システムのOpenSSLを探します（`OPENSSL_DIR` などの環境変数を参照します）。
```powershell
git clone https://github.com/microsoft/vcpkg C:\vcpkg
C:\vcpkg\bootstrap-vcpkg.bat
C:\vcpkg\vcpkg install openssl:x64-windows-static-md
$env:OPENSSL_DIR = "C:\vcpkg\installed\x64-windows-static-md"
$env:OPENSSL_STATIC = "1"
cd rust\ibis-core
cargo test            # --features vendored-openssl は付けない
```
vcpkg のトリプレット名・環境変数は、実機で確認が必要です。

## 8. 署名なしアプリの配布と受け取り（将来の手順）
決定事項：コード署名の証明書は、1年間（2027年11月まで）取得しません。
- **Windowsの警告**：署名のない実行ファイルを、ブラウザやメールで受け取って実行すると、「WindowsによってPCが保護されました」と表示されるのが一般的です。「詳細情報」→「実行」で起動します。USBメモリなどで渡すと表示されない場合があります（実機で確認）
- **改ざん・差し替えの確認**：配る側がインストーラの SHA-256 を書類に記載し、受け取る側が照合します
  ```powershell
  Get-FileHash -Algorithm SHA256 .\インストーラ.exe
  ```
- **ウイルス対策ソフトの誤検知**：署名のない新しいアプリでは起きやすい。起きたら Microsoft Defender へ誤検知を報告し、インストール先を除外に追加する
- **更新**：自動更新は使わず、手動で更新する（更新ファイルの SHA-256 を照合）

## 9. この手順書の検証状況
| 項目 | 状態 |
|---|---|
| Rust 1.75 ＋ rusqlite ＋ SQLCipher（システムのOpenSSL）のビルドとテスト | Linuxで確認済み |
| `vendored-openssl`（OpenSSLをソースから同梱）のビルドとテスト | **Linuxで確認済み**（初回ビルド約6分40秒、Rustのテスト4件が合格、OpenSSLは静的に同梱）。OpenSSLの最新版が Rust 1.80 以上を要求するため、この環境（Rust 1.75）では `openssl-sys` を 0.9.109 に固定した（`Cargo.lock`）。Windowsで最新のRustを使う場合も、固定した版で動く想定 |
| PowerShell スクリプトの構文 | PowerShell 7.4 の構文解析器で確認済み（5.1 でも動く書き方にしている） |
| PowerShell スクリプトの動作 | Linux上の PowerShell で、Windows固有でない部分（版の確認、報告書の作成、ユーザー名の置き換え）を確認済み。レジストリ・`vswhere` など Windows固有の部分は未確認 |
| GitHub Actions（CI） | YAMLの構文を確認済み。**実行は未確認** |
| `windows-build.yml`（手動実行） | **未実行**。GitHubのWindows環境で、第5章と同じ確認ができる |
| Windows 10 実機 | **未検証**（Perl・NASMが必要かの確定、所要時間、SmartScreenの挙動、DPAPI） |
