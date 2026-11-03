# Rustのデータ層（ibis-core）のビルドとテスト、DBのテストを実行し、結果を build-report.txt に保存する
#   使い方:  powershell -ExecutionPolicy Bypass -File tools\build-core.ps1
#            （OpenSSLを使わずシステムのOpenSSLを使う場合は -Features "" ）
#   初回は OpenSSL と SQLCipher をソースからビルドするため、時間がかかります（Linuxでの初回は約7分でした）
param([string]$Features = "vendored-openssl", [switch]$SkipPython)
$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"   # Python の出力を UTF-8 に（Windows の既定の文字コードでは、日本語の出力で落ちるため）
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
$root = Split-Path -Parent $PSScriptRoot
$report = Join-Path $root "build-report.txt"
$lines = New-Object System.Collections.ArrayList
function Log($t) { Write-Output $t; [void]$lines.Add($t) }

$start = Get-Date
Log ("開始: " + $start.ToString("yyyy-MM-dd HH:mm:ss"))
$failed = 0

# 1) Rust
Push-Location (Join-Path $root "rust\ibis-core")
try {
  if ($Features -ne "") { $cargoArgs = @("test", "--features", $Features) } else { $cargoArgs = @("test") }
  Log ("== cargo " + ($cargoArgs -join " ") + " ==")
  $out = & cargo @cargoArgs 2>&1 | ForEach-Object { "" + $_ }
  $code = $LASTEXITCODE
  $out | Where-Object { $_ -match "^test |^test result:|^error|Finished" } | ForEach-Object { Log $_ }
  if ($code -ne 0) { $failed++; Log "（失敗：末尾40行）"; $out | Select-Object -Last 40 | ForEach-Object { Log $_ } }
} finally { Pop-Location }

# 2) Python（暗号化なしのDBテスト。sqlcipher3 が無くても実行できる）
if (-not $SkipPython) {
  $pyExe = $null; $pyArgs = @()
  if (Get-Command python -ErrorAction SilentlyContinue) { $pyExe = "python" }
  elseif (Get-Command py -ErrorAction SilentlyContinue) { $pyExe = "py"; $pyArgs = @("-3") }
  if ($null -eq $pyExe) { Log "== Python が見つからないため、DBテストを省略しました ==" }
  else {
    foreach ($t in @("db\tests\run_tests.py", "db\tests\test_encryption.py")) {
      Log ("== " + $pyExe + " " + $t + " ==")
      $o = & $pyExe @pyArgs (Join-Path $root $t) 2>&1 | ForEach-Object { "" + $_ }
      $c = $LASTEXITCODE
      $o | Where-Object { $_ -match "^Ran |^OK|^FAILED|^FAIL:|^ERROR:|ModuleNotFoundError" } | ForEach-Object { Log $_ }
      if ($c -ne 0) { Log "（失敗または一部省略：sqlcipher3 が入っていない場合、暗号化のテストは省略されます）" }
      if ($c -ne 0 -and $t -like "*run_tests*") { $failed++ }
    }
  }
}

$elapsed = [int]((Get-Date) - $start).TotalSeconds
Log ("所要: {0} 秒" -f $elapsed)
Log $(if ($failed -eq 0) { "結果: 合格" } else { "結果: 失敗あり（" + $failed + "）" })
[System.IO.File]::WriteAllLines($report, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
Write-Output ("`n結果を保存しました: " + $report)
if ($failed -gt 0) { exit 1 } else { exit 0 }
