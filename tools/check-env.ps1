# Windows 10 ビルド環境の確認（PowerShell 5.1 で動作。パスワードや鍵は扱わない）
#   使い方:  powershell -ExecutionPolicy Bypass -File tools\check-env.ps1
#   [OK]=問題なし  [NG]=必須なのに不足  [--]=任意（今は無くてもよい）
$ErrorActionPreference = "Continue"
$rows = New-Object System.Collections.ArrayList
function Add-Row($name, $required, $ok, $detail, $hint) {
  [void]$rows.Add([pscustomobject]@{ Name = $name; Required = $required; Ok = $ok; Detail = $detail; Hint = $hint })
}
function First-Line($exe, $argList) {
  try {
    $cmd = Get-Command $exe -ErrorAction Stop
    $out = & $cmd.Source @argList 2>&1 | ForEach-Object { "" + $_ } | Where-Object { $_ -match "\S" } | Select-Object -First 1
    return ("" + $out).Trim()
  } catch { return $null }
}

# --- OS ---
$isWin = ($env:OS -eq "Windows_NT")
if ($isWin) {
  $ver = [System.Environment]::OSVersion.Version
  $disp = ""
  try { $disp = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion" -ErrorAction Stop).DisplayVersion } catch { }
  Add-Row "Windows" $true ($ver.Major -ge 10) ("{0}.{1}.{2} {3}" -f $ver.Major, $ver.Minor, $ver.Build, $disp) "Windows 10 22H2 以上を想定しています"
} else {
  Add-Row "Windows" $true $false "Windows ではありません" "この手順書は Windows 10 向けです"
}

# --- 必須：git / Rust（MSVC）/ C++ Build Tools / Perl / NASM ---
$git = First-Line "git" @("--version")
Add-Row "Git" $true ($null -ne $git) $git "winget install --id Git.Git -e"

$rustc = First-Line "rustc" @("--version")
Add-Row "Rust (rustc)" $true ($null -ne $rustc) $rustc "winget install --id Rustlang.Rustup -e  （入れたら PowerShell を開き直す）"
$cargo = First-Line "cargo" @("--version")
Add-Row "Rust (cargo)" $true ($null -ne $cargo) $cargo "rustup をインストールすると一緒に入ります"
$tc = First-Line "rustup" @("show", "active-toolchain")
$isMsvc = ($null -ne $tc) -and ($tc -match "msvc")
Add-Row "Rust ツールチェーンが MSVC" $true $isMsvc $tc "rustup default stable-msvc"

$pf86 = ${env:ProgramFiles(x86)}
if ([string]::IsNullOrEmpty($pf86)) { $pf86 = $env:ProgramFiles }
$vs = $null
$vswhere = $null
if (-not [string]::IsNullOrEmpty($pf86)) { $vswhere = Join-Path $pf86 "Microsoft Visual Studio\Installer\vswhere.exe" }
if ($isWin -and $vswhere -and (Test-Path $vswhere)) {
  try { $vs = ("" + (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1)).Trim() } catch { }
}
Add-Row "C++ Build Tools（C++によるデスクトップ開発）" $true (-not [string]::IsNullOrEmpty($vs)) $vs "Visual Studio Build Tools をインストールし、「C++によるデスクトップ開発」を選ぶ（docs/build-windows.md）"

$perl = First-Line "perl" @("-v")
Add-Row "Perl（OpenSSLのビルド用）" $true ($null -ne $perl) $perl "winget install --id StrawberryPerl.StrawberryPerl -e"
$nasm = First-Line "nasm" @("-v")
Add-Row "NASM（OpenSSLのビルド用）" $true ($null -ne $nasm) $nasm "winget install --id NASM.NASM -e  → C:\Program Files\NASM を PATH に追加"

# --- 任意：Python / Node.js / WebView2 / 長いパス ---
$py = First-Line "python" @("--version")
if ($null -eq $py) { $py = First-Line "py" @("-3", "--version") }
Add-Row "Python（DBテスト用）" $false ($null -ne $py) $py "winget install --id Python.Python.3.12 -e"
$node = First-Line "node" @("--version")
Add-Row "Node.js（アプリのテスト用）" $false ($null -ne $node) $node "winget install --id OpenJS.NodeJS.LTS -e"

$wv = $null
if ($isWin) {
  foreach ($root in @("HKLM:\SOFTWARE\WOW6432Node", "HKLM:\SOFTWARE", "HKCU:\SOFTWARE")) {
    try {
      $p = Get-ItemProperty ($root + "\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}") -ErrorAction Stop
      if ($p.pv) { $wv = $p.pv; break }
    } catch { }
  }
}
Add-Row "WebView2 ランタイム（Tauri化の段階で必須）" $false ($null -ne $wv) $wv "Windows 10 の更新で入っていることが多い。無ければ Microsoft の WebView2 ランタイム（Evergreen）をインストール"

$lp = $null
if ($isWin) { try { $lp = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -ErrorAction Stop).LongPathsEnabled } catch { } }
Add-Row "長いパス（260文字超）の許可" $false ($lp -eq 1) ("LongPathsEnabled=" + $lp) "ビルドで『パスが長すぎる』と出た場合：git config --system core.longpaths true と、Windowsの長いパスの有効化"

# --- 表示 ---
foreach ($r in $rows) {
  if ($r.Ok) { $mark = "[OK]" } elseif ($r.Required) { $mark = "[NG]" } else { $mark = "[--]" }
  Write-Output ("{0} {1}  {2}" -f $mark, $r.Name, $r.Detail)
  if (-not $r.Ok -and $r.Hint) { Write-Output ("      → " + $r.Hint) }
}
$ng = @($rows | Where-Object { $_.Required -and -not $_.Ok }).Count
if ($ng -gt 0) { Write-Output ("`n必須の項目が {0} 件不足しています。上の「→」の手順で入れてから、PowerShell を開き直して、もう一度実行してください。" -f $ng); exit 1 }
Write-Output "`n必須の項目はそろっています。次は docs\build-windows.md の「5. ビルドとテスト」へ。"
exit 0
