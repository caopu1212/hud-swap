@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'hudswap\.py') -or $_.Name -eq 'HudSwap.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
if "%1"=="build" del HudSwap.old.exe >nul 2>&1
rem a stuck old process may lock the exe: rename it first (a running exe can be renamed)
if "%1"=="build" if exist HudSwap.exe move /y HudSwap.exe HudSwap.old.exe >nul 2>&1
if "%1"=="build" python -m PyInstaller --noconfirm --onefile --windowed --name HudSwap --icon "%~dp0icon.ico" --distpath "%~dp0." --workpath "%TEMP%\hudswap-build" --specpath "%TEMP%\hudswap-build" hudswap.py >nul 2>&1
if exist HudSwap.exe (start "" HudSwap.exe) else (start "" pythonw hudswap.py)
