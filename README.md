# 統合業務システム

作業人員集計表・請求書・宛名ラベル・マスタ・データ管理・レポート・工事台帳を1つにまとめた業務システムです。
現在は **単一のHTMLファイル**（ブラウザだけで動く）で、次の段階として **Windowsデスクトップアプリ化（Tauri＋暗号化SQLite）** を進めています。

> **このリポジトリは非公開（Private）で運用してください。** 実データ・暗号鍵・復旧コード・Excel／CSV は、絶対に登録しません（[docs/security-and-data.md](docs/security-and-data.md)）。

## 現在の状態

| 領域 | 状態 |
|---|---|
| アプリ（単一HTML） | 動作中。残業の分離、発行確定・取消（赤伝）、追記専用ログ、スキーマ版管理、自動バックアップ（7年保持）、事務処理規程の雛形出力まで実装・テスト済み |
| DB設計（SQLite） | 設計・移行ツール・検証まで完了（27テーブル／27トリガー／5ビュー）。発行済み・締め済み・追記専用をDB自体が強制 |
| 暗号化（SQLCipher） | 決定済み。暗号化DBでの動作、PythonとRustの相互運用を検証済み |
| Rust データ層（`ibis-core`） | 最小実装あり。**Windowsでのビルドは未検証**（手順書とCIを用意） |
| Tauri アプリ | 未着手 |
| コード署名 | 1年間は取得しない（2027年11月に見直し） |

## フォルダ構成

```
app/        アプリ本体（integrated-biz-system.html）と、その自動テスト（npm test）
db/         SQLiteのスキーマ（schema.sql）、JSON→SQLite移行ツール、暗号化・鍵・復旧コード、テスト
rust/       Rustのデータ層（ibis-core）：暗号化DBの操作、操作ログ、復旧コード
tools/      秘密情報チェック、Windows用の環境確認・ビルド・報告書スクリプト
docs/       手順書（Windowsビルド、GitHub、復旧コード、セキュリティ）と設計の要点
.github/    CI（自動テスト）、Windowsビルド確認、Dependabot
```

## 使い方

### アプリを使う
`app/integrated-biz-system.html` を Microsoft Edge で開くだけです（インターネット接続なしで動きます。PDF出力のみ外部ライブラリを読み込みます）。

### テストを実行する
| 対象 | コマンド | 期待する結果 |
|---|---|---|
| アプリ | `cd app && npm ci && npm test` | すべて合格 |
| DB・移行 | `python db/tests/run_tests.py` | 34件 OK |
| 暗号化 | `pip install -r db/requirements.txt` → `python db/tests/test_encryption.py` | 17件 OK |
| Rust⇄Python | `python db/tests/cross_check.py`（cargo が必要） | OK（Rust 4件を含む） |
| 秘密情報チェック | `python tools/test_check_secrets.py` | 15件 OK |

### Windowsでビルドする
[docs/build-windows.md](docs/build-windows.md) を参照してください（`tools\check-env.ps1` で環境を確認できます）。

## 開発の進め方
- コミット前に、秘密情報・実データのチェックが自動で動きます：`tools/install-hooks.sh`（Windowsは `tools\install-hooks.ps1`）で有効にします
- ブランチ・コミットの決まり：[docs/git-workflow.md](docs/git-workflow.md)
- GitHubへの登録と設定：[docs/github-setup.md](docs/github-setup.md)
- 復旧コード（暗号鍵の紙の控え）の作り方：[docs/recovery-code.md](docs/recovery-code.md)
- DB設計の要点：[docs/db-design.md](docs/db-design.md)

## ライセンス
未設定（社内利用）。公開・再配布はしません。
