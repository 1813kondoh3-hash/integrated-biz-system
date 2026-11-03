## 変更の内容
<!-- 何を、なぜ変えたか -->

## 確認したこと
- [ ] `npm test`（app/）が通る　※ アプリを変えた場合
- [ ] `python db/tests/run_tests.py` と `python db/tests/test_encryption.py` が通る　※ DB・移行を変えた場合
- [ ] `python db/tests/cross_check.py` が通る　※ スキーマ・Rustを変えた場合
- [ ] `schema.sql` を変えた場合：DBの版（`PRAGMA user_version`）と CHANGELOG を更新した
- [ ] 実データ・鍵・復旧コード・Excel／CSV を含めていない（`python tools/check_secrets.py --staged` が通る）
