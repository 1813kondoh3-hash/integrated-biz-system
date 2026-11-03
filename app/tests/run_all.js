// アプリ（単一HTML）のテストを順に実行する。1つでも失敗したら終了コード1
const { spawnSync } = require("child_process");
const path = require("path");
const suites = [
  ["test_phase0_manhours_tax.js", "残業分離・月プルダウン・簡易課税・経過措置・閲覧専用ファイル"],
  ["test_phase1_evidence.js", "発行確定・取消（赤伝）・追記専用ログ・規程雛形"],
  ["test_phase1_schema_backup.js", "スキーマ移行・読込検証・自動バックアップ・復元"],
  ["test_regression.js", "全タブ・全ボタンの回帰テスト／請求金額の照合"],
];
let failed = 0;
for (const [file, title] of suites) {
  const r = spawnSync(process.execPath, [path.join(__dirname, file)], { encoding: "utf-8", env: process.env });
  const out = (r.stdout || "") + (r.stderr || "");
  const lines = out.split("\n").filter(l => /^Total:|^FAIL|^ERRORS:/.test(l));
  console.log(`${r.status === 0 ? "OK  " : "NG  "} ${file}  （${title}）`);
  lines.forEach(l => console.log("      " + l));
  if (r.status !== 0) { failed++; if (!lines.length) console.log(out.slice(-1500)); }
}
console.log(failed ? `\n失敗したスイート: ${failed}` : "\nすべて合格");
process.exit(failed ? 1 : 0);
