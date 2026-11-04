# GitHub への登録と設定

> このリポジトリは **非公開（Private）** で作ります。実データ・鍵は登録しませんが（[security-and-data.md](security-and-data.md)）、業務システムのソースなので、公開はしません。

## 1. 準備
- GitHubのアカウントを2要素認証にします
- Git for Windows（または git）が入っていること

## 2. GitHub で空のリポジトリを作る
1. GitHub → New repository
2. 名前：`integrated-biz-system`（任意）
3. **Private** を選ぶ
4. **README・.gitignore・ライセンスは追加しない**（このリポジトリに入っているため。追加すると、最初のpushが衝突します）

## 3. 受け取ったリポジトリを登録する
`integrated-biz-system.bundle`（履歴つきの1ファイル）から、複製を作ります。

```powershell
git clone integrated-biz-system.bundle integrated-biz-system
cd integrated-biz-system
git remote set-url origin https://github.com/<アカウント>/integrated-biz-system.git
git push -u origin main --tags
```
（zip は、履歴を含まない**閲覧用のスナップショット**です。GitHubへ登録するときは、必ず `.bundle` を使ってください）

### 作者情報の変更（push の前に）
基準版のコミットは、ダミーの作者（`Integrated Biz System <dev@example.invalid>`）で作られています。**push の前に**、ご自身の名前に直せます。

```powershell
git config user.name  "あなたの名前"
git config user.email "あなたのメールアドレス（またはGitHubのnoreplyアドレス）"
git rebase --root --exec "git commit --amend --reset-author --no-edit"
git tag -d v0.1.0
git tag -a v0.1.0 -m "基準版（P0・P1完了、DB設計・暗号化の検証済み）"
```
（コミットのIDが変わります。push 前なので問題ありません。push 後に行うと、履歴の書き換えになります）

## 4. 有効にしておく設定
| 設定 | 場所（名称は変わることがあります） |
|---|---|
| Actions を有効にし、最初の CI が緑になることを確認 | Actions タブ |
| main ブランチの保護：直接 push を禁止、Pull Request を必須、CI の成功を必須、強制 push を禁止 | Settings → Branches（または Rulesets） |
| フォークを許可しない | Settings → General |
| シークレットスキャン、プッシュ保護、Dependabot のアラート | Settings → Code security（プランにより利用可否が異なります） |
| 招待するメンバーは最小限に | Settings → Collaborators |

main の保護で必須にする CI のジョブ名：`秘密情報・実データの混入チェック`、`アプリ（単一HTML）のテスト`、`DB設計・移行・暗号化のテスト`、`Rust ⇄ Python の相互運用テスト`

## 5. Windowsでのビルドを GitHub 上で確認する
Windows実機が手元に無くても、GitHubのWindows環境で、Rustのビルドとテストを確認できます。
1. Actions タブ →「Windows build check」→ Run workflow
2. 完了後、ログと、成果物（`windows-build-logs`）を確認する
3. 失敗した場合は、ログの該当部分を共有する（Perl・NASM の用意、OpenSSL のビルドが主な原因）

※ 2026年11月に実行し、緑を確認済みです（DBテスト34件、Rustのテスト4件。Pythonの暗号化テストは、Windowsでは暗号化DBを使う12件を省略）。

## 6. 日常の流れ
[git-workflow.md](git-workflow.md) を参照してください。
