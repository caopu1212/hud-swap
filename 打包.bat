@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem exe 生成在本目录，和 hud_config.json、tmpl_*.png 放在一起（程序读写 exe 同目录的配置）
python -m PyInstaller --noconfirm --onefile --windowed --name HudSwap --icon "%~dp0icon.ico" --distpath "%~dp0." --workpath "%TEMP%\hudswap-build" --specpath "%TEMP%\hudswap-build" hudswap.py
echo.
echo 已生成 HudSwap.exe
pause
