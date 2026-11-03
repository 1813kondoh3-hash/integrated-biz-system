# ビルド環境の報告書（env-report.txt）を作る。うまくいかないときの相談用。
#   含むもの：OS・ツールの版・環境確認の結果・build-report.txt（あれば）
#   含まないもの：環境変数、パスワード、鍵、データ。ユーザー名は <user> に置き換える
#   使い方:  powershell -ExecutionPolicy Bypass -File tools\collect-report.ps1
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$sb = New-Object System.Text.StringBuilder
function Add-Line($t) { [void]$sb.AppendLine($t) }

Add-Line ("作成日時: " + (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"))
Add-Line ("PowerShell: " + $PSVersionTable.PSVersion.ToString())
Add-Line ""
Add-Line "== 環境確認（tools\check-env.ps1） =="
$envOut = & (Join-Path $PSScriptRoot "check-env.ps1") 2>&1 | Out-String
Add-Line $envOut
$buildReport = Join-Path $root "build-report.txt"
if (Test-Path $buildReport) {
  Add-Line "== ビルドとテストの結果（build-report.txt） =="
  Add-Line ([System.IO.File]::ReadAllText($buildReport))
} else {
  Add-Line "（build-report.txt はまだありません。tools\build-core.ps1 を実行すると作られます）"
}
try { Add-Line ("git: " + ("" + (& git rev-parse --short HEAD 2>$null)).Trim()) } catch { }

$text = $sb.ToString()
foreach ($v in @($env:USERNAME, $env:USERPROFILE, $env:COMPUTERNAME)) {
  if (-not [string]::IsNullOrEmpty($v)) { $text = $text.Replace($v, "<user>") }
}
$out = Join-Path $root "env-report.txt"
[System.IO.File]::WriteAllText($out, $text, (New-Object System.Text.UTF8Encoding($false)))
Write-Output ("作成しました: " + $out)
Write-Output "内容を確認し、問題がなければ共有してください（このファイルは .gitignore で、リポジトリには登録されません）。"
