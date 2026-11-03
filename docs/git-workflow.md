# Git の使い方（ブランチ・コミット・テスト）

## ブランチ
- `main`：常に「テストが通る状態」。直接 push せず、Pull Request で取り込む
- 作業は枝を切る：`feat/○○`（機能）、`fix/○○`（不具合）、`docs/○○`（文書）、`chore/○○`（その他）

## コミットのメッセージ
`種類: 内容`（日本語で簡潔に）。種類は `feat` `fix` `docs` `test` `refactor` `chore`。
例：`fix: 発行日が空の請求書で、最新の税率を使うように修正` ／ `feat(db): 取引先の非表示（archived）を追加`
1つのコミットには、1つの目的だけを入れる。

## コミット前
```bash
git status                              # 意図しないファイルが無いか
python tools/check_secrets.py --staged  # 秘密情報・実データのチェック（フックを入れていれば自動）
```

## 変更したら、何のテストを実行するか
| 変更した場所 | 実行するもの |
|---|---|
| `app/integrated-biz-system.html` | `cd app && npm test` |
| `db/schema.sql`、`db/migrate_json_to_sqlite.py` | `python db/tests/run_tests.py`、`python db/tests/test_encryption.py`、`python db/tests/cross_check.py` |
| `rust/ibis-core` | `python db/tests/cross_check.py`（Rust 4件を含む） |
| `tools/check_secrets.py` | `python tools/test_check_secrets.py` |
| スキーマ（`schema.sql`）の構造 | 上のすべて ＋ DBの版（`PRAGMA user_version`）と `CHANGELOG.md` の更新 |

## スキーマを変えるとき
- DBの構造を変えたら、`PRAGMA user_version` を上げ、既存DBを新しい構造へ移す手順（移行）を用意する
- JSONのデータ形式（アプリ側のスキーマ版）を変えたら、`migrateData` に移行の手順を追加し、`gen_fixtures.js` で検証データを作り直す
- どちらも、旧データから新データへ移して、**往復・計算・ハッシュが一致する**ことをテストで確認する

## タグと版
- アプリ・リポジトリ全体の版：`v0.1.0` のように、変更履歴（`CHANGELOG.md`）と一緒に付ける
- DBの構造の版：`schema.sql` の `PRAGMA user_version`

## 検証データを作り直すとき
`db/tests/gen_fixtures.js` で、アプリのサンプルデータから作ります（実データを使わない）。
```bash
cd app && npm ci && cd ../db/tests
NODE_PATH=../../app/node_modules node gen_fixtures.js
```

## 依存ライブラリの更新（Dependabot）
- 更新の提案は月1回、同時に3件までです
- **GitHub Actions・npm・pip**：CIが緑になったPull Requestを、1件ずつマージする（マージのたびに残りが自動で再実行される）。`upload-artifact` は手動実行のWindows確認でしか使わないため、マージ後に「Windows build check」を1回実行して確認する
- **Rust（rusqlite・sha2・rand）**：自動では提案しない設定にしている。DBの暗号化と、操作ログ・内容ハッシュの形式に直結するため。四半期ごとに更新の有無を確認し、上げるときは次を確認してから手動で行う
  1. `python db/tests/cross_check.py`（Pythonとの相互運用）と `python db/tests/test_encryption.py` が通る
  2. 既存の暗号化DBが、新しい版で開ける（SQLCipherの版が変わる場合は、互換性を確認する）
  3. Windowsでのビルド（`windows-build.yml`）が通る
- セキュリティの更新は、この設定とは別に通知されることがあります（GitHubの設定・プランによる。要確認）
