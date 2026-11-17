# AVD Flagship — Windows 便携包脚手架（未签名；WiX/MSI 见文末注释）
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Dist = Join-Path $Root "dist\windows\AVD-Flagship"
New-Item -ItemType Directory -Force -Path $Dist | Out-Null

$Items = @("fusion", "downloader", "static", "server.py", "boot.py", "pyproject.toml", "NOTICE.md")
foreach ($name in $Items) {
  $src = Join-Path $Root $name
  if (Test-Path $src) {
    Copy-Item -Recurse -Force $src (Join-Path $Dist $name)
  }
}

@"
@echo off
set PYTHONPATH=%~dp0
cd /d %~dp0
python -c "from boot import main; raise SystemExit(main())" %*
"@ | Set-Content -Encoding ASCII (Join-Path $Dist "avd-flagship.bat")

if ($env:AVD_WIN_CERT) {
  Write-Host "TODO: signtool sign /f `$env:AVD_WIN_CERT avd-flagship.bat (and any .exe)"
} else {
  "UNSIGNED — set AVD_WIN_CERT for signtool" | Set-Content (Join-Path $Dist "UNSIGNED.txt")
}

Write-Host "Built portable tree: $Dist"
# WiX 示例（需另装 WiX Toolset）：
# heat dir dist\windows\AVD-Flagship -cg AVDFiles -gg -sfrag -out avd.wxs
# candle avd.wxs && light avd.wixobj -o AVD-Flagship.msi
