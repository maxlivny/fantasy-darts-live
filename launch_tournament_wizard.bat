@echo off
cd /d "%~dp0"
python scripts\tournament_wizard.py
if errorlevel 1 (
  echo.
  echo Не удалось запустить мастер. Убедитесь, что Python установлен.
  pause
)
