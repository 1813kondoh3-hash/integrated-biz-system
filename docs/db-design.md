# DB設計の要点

詳細版は設計書「C案 データベース設計書（SQLite）」（Claude Doc）にあります。ここでは、開発に必要な要点だけをまとめます。

## 方針
1. 取引・マスタ・作業人員は表に分け、外部キーで整合性を守る
2. 金額・数量は**入力どおりの文字列**で保存し、計算用の数値は生成列で持つ（現行アプリの `parseFloat(x)||0` と同じ解釈。表示・ハッシュが変わらない）
3. 「発行済みは変更・削除できない」「締め済みの月は変更できない」「監査ログは追記専用」を、**トリガーでDB自体が強制**する
4. 項目が無いことは NULL で表す（JSONとの往復で同一になる）。主要テーブルに `extra`（JSON）を持たせ、未知の項目を失わない
5. DBは SQLCipher で暗号化する（256ビットの鍵を直接指定）

## 構成（27テーブル／27トリガー／5ビュー／DB版 1）
- メタ・設定：`meta` `migration_history` `company` `app_settings`
- 税：`tax_rates` `transitional_rates`（免税事業者等からの仕入れの控除割合）
- マスタ：`clients`（`archived`で非表示）`sites` `expense_items` `site_budgets` `invoice_seq` と、請求書のひな形3表
- 請求書：`invoices` `invoice_items` `invoice_expenses` `payments`
- 作業人員：`man_hour_months` `man_hour_rows`（`kind`：人員／残業）`man_hour_cells` `man_hour_expenses`
- その他：`site_costs` `labels` `closed_months`、証跡：`audit_log`（追記専用・ハッシュ連鎖）
- ビュー：`v_invoice_totals`（消費税は**符号を分けて**四捨五入：赤伝が元の請求書と1円もずれず打ち消し合う）`v_client_balance` `v_site_revenue` `v_man_hour_rows` `v_man_hour_month`

## DBが守るルール
| ルール | 仕組み |
|---|---|
| 操作ログは追記専用 | 更新・削除をトリガーで拒否。追記時は連番と直前ハッシュの連続性を検査 |
| 発行済みの請求書・明細・経費は変更・削除できない | トリガー（状態と入金は可） |
| 取消の印は一度付けたら変更できない／取消済み・赤伝には入金できない | トリガー |
| 締め済みの月は変更できない | 請求書・明細・経費・入金、作業人員の行・セル・諸経費・繰越をトリガーで拒否。取消の印の付与だけは可（赤伝は当月に作る） |
| 使用中の取引先は削除できない | 外部キー。代わりに `archived` にする |
| 伝票No.の重複禁止 | 空でない伝票No.に一意制約 |

## 移行（現行JSON → SQLite）
`python db/migrate_json_to_sqlite.py migrate backup.json data.db [--key-file key.txt]`
1. 事前チェック（形式 v2、チェックサム、操作ログの連鎖、伝票No.の重複、状態の値、赤伝の取消対象、月キー）。1つでも問題があれば移行しない
2. 1つのトランザクションで変換（失敗したらDBを残さない）。発行済み・取消の印は明細の後に、締めは最後に投入する（改変防止トリガーのため）
3. 自己検証：整合性、外部キー、請求書ごとの金額、作業人員の月合計、操作ログの連鎖、**JSON→DB→JSONの往復一致**、発行済み請求書の内容ハッシュ
4. 元のJSONは変更しない。ロールバックは「新DBを使わず元のJSONから始める」

## 暗号化と鍵
- SQLCipher 4（AES-256、ページごとにHMAC-SHA512）。DB本体・WAL・`VACUUM INTO` のバックアップに平文が残らないことを確認済み
- 鍵：ランダム256ビット（`x'64桁の16進'`）。WindowsのDPAPIで保護して保管し、**復旧コード（55文字）を紙に控える**（[recovery-code.md](recovery-code.md)）
- 鍵の変更：`PRAGMA rekey` または `sqlcipher_export`
- **復旧コードとPC側の鍵の両方を失うと、データは復元できない**

## 検証の状況
Python（SQLite 3.45.1／SQLCipher 4.12）と Rust 1.75（Linux）で検証済み。Windows実機・WebView2・Windows向けRustビルドは未確認（[build-windows.md](build-windows.md)）。
