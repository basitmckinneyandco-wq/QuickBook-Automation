@echo off
REM ---------------------------------------------------------------------
REM  Run this ONCE. It puts a "QuickBooks Dashboard" icon on your Desktop
REM  so you never have to find this folder again.
REM ---------------------------------------------------------------------
setlocal
set "HERE=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$shell = New-Object -ComObject WScript.Shell;" ^
  "$link  = Join-Path ([Environment]::GetFolderPath('Desktop')) 'QuickBooks Dashboard.lnk';" ^
  "$s = $shell.CreateShortcut($link);" ^
  "$s.TargetPath       = Join-Path $env:HERE 'Start Dashboard.cmd';" ^
  "$s.WorkingDirectory = $env:HERE;" ^
  "$s.IconLocation     = \"$env:SystemRoot\System32\imageres.dll,109\";" ^
  "$s.Description      = 'Open the QuickBooks automation dashboard';" ^
  "$s.Save();" ^
  "Write-Host '';" ^
  "Write-Host ('  Done. Look on your Desktop for: QuickBooks Dashboard') -ForegroundColor Green;" ^
  "Write-Host ''"

pause
