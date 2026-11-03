# コミット前チェック（秘密情報・実データの混入防止）を有効にする
Set-Location (Split-Path -Parent $PSScriptRoot)
git config core.hooksPath .githooks
if ($LASTEXITCODE -eq 0) { Write-Host "有効にしました（core.hooksPath = .githooks）" } else { Write-Host "失敗しました。git が使えるか確認してください。"; exit 1 }
