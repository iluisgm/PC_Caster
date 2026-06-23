@echo off
REM Creates a branded "PC Caster" shortcut on your Desktop that launches the
REM app with NO console window. Run this once (re-run if you move the folder).
title Create PC Caster shortcut

set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\PC Caster.lnk');" ^
  "$lnk.TargetPath = 'wscript.exe';" ^
  "$lnk.Arguments = '\"%APPDIR%\PC Caster.vbs\"';" ^
  "$lnk.WorkingDirectory = '%APPDIR%';" ^
  "$lnk.IconLocation = '%APPDIR%\assets\app_icon.ico';" ^
  "$lnk.Description = 'PC Caster';" ^
  "$lnk.Save();"

echo.
echo  Done! A 'PC Caster' icon is on your Desktop.
echo  Double-click it to launch the app with no console window.
echo.
pause
