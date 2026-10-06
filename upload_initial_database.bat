@echo off
cd /d "%~dp0"
python "%~dp0tools\upload_initial_database.py"
pause