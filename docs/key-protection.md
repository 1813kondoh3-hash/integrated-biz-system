# 暗号鍵の保護（DPAPI）

DBを暗号化する鍵を、Windowsの仕組み（DPAPI）で保護して保管します。Tauriアプリは、ここに保管した鍵を使ってDBを開きます。

## 1. しくみ

| 項目 | 内容 |
|---|---|
| 保管場所 | `%LOCALAPPDATA%\IntegratedBizSystem\key.dpapi`（Windowsのユーザーごとのフォルダ） |
| 保護のしかた | DPAPI（CurrentUser）。そのWindowsユーザーでログオンしているときだけ開ける |
| ファイルの中身 | 形式の印・保護の種類・鍵の指紋（SHA-256の先頭8バイト）・DPAPIで暗号化した鍵。**鍵そのものは書かれていない** |
| 書き込み | 一時ファイルに書いてから置き換える（途中で止まっても、壊れたファイルを残さない）。**既存の保管ファイルは上書きしない** |
| 新しい鍵 | 復旧コードを表示し、紙に書き写したコードを再入力して一致したときだけ保存する |

### 守れるもの・守れないもの
- **守れる**：別のWindowsユーザー、別のPC、ファイルの持ち出し（コピーされても開けない）、保管ファイルの改ざん・破損（指紋で検出）。
- **守れない**：同じWindowsユーザーとして動くプログラム（マルウェアを含む）。PCを最新に保ち、不審なソフトを入れないことが前提です。
- **開けなくなる場合**：Windowsの再インストール、ユーザーの作り直し、PCの入れ替え。そのときは、**紙の復旧コードから復元**します（`restore`）。

## 2. 確認用のコマンド `ibis-key`

GitHubの「Windows build check」の成果物（`windows-build-logs`）に、`ibis-key.exe` が入っています。PowerShellで、exe を置いたフォルダに移動して実行します。

| コマンド | 内容 |
|---|---|
| `.\ibis-key.exe import C:\ibis-key\key.txt` | 移行ツールで作った鍵ファイルを取り込み、DPAPIで保護して保管する |
| `.\ibis-key.exe status` | 保管した鍵を開いて、保管方式と鍵の指紋を表示する |
| `.\ibis-key.exe check-code` | 紙の復旧コードを入力し、保管した鍵と一致するか確かめる |
| `.\ibis-key.exe restore` | 紙の復旧コードから鍵を復元して保管する（既存の保管ファイルがあるときは `--force` が必要） |
| `.\ibis-key.exe init` | 新しい鍵を作る（復旧コードを表示し、再入力で確認してから保存） |

- 鍵そのもの（64桁の16進数）は、どのコマンドでも表示しません。復旧コードを表示するのは `init` だけです。
- 署名のない exe のため、初回は「WindowsによってPCが保護されました」と表示されることがあります（Windowsビルド手順書の第8章）。
- 「VCRUNTIME140.dll が見つかりません」と出た場合は、Microsoftの「Visual C++ 再頒布可能パッケージ」が必要です（アプリの配布時には、不要にする方法を検討します）。

## 3. Windows 10 実機での確認手順（初回）
1. 「Windows build check」を実行し、成果物（`windows-build-logs`）をダウンロードして展開する。
2. `.\ibis-key.exe import C:\ibis-key\key.txt` で、今の鍵を取り込む。
3. `.\ibis-key.exe status` で、「保管方式：Windows DPAPI」と「鍵を正しく開けました」を確認する。
4. `.\ibis-key.exe check-code` で、紙の復旧コードを入力し、「一致しました」を確認する。
5. 確認できたら、平文の鍵ファイル（`C:\ibis-key\key.txt`）は削除してよい（紙の復旧コードとDPAPIの保管が残る）。ごみ箱も空にする。

## 4. 確認の状況
| 項目 | 状態 |
|---|---|
| 保管の仕組み（確認つきの保存、上書き防止、改ざん・破損の検出、取り込み、復元） | Linuxでテスト済み（テスト用の保護で） |
| DPAPIを呼ぶコードの型の検査 | Linux上で確認済み（`cargo check --tests --features dpapi-typecheck`） |
| DPAPIの実際の動作 | GitHubのWindows環境のテストで確認する（`dpapi_roundtrip_and_tamper_detection`、`dpapi_store_end_to_end`） |
| Windows 10 実機 | 第3章の手順で確認する |
