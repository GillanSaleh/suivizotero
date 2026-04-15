@echo off
cd /d "%~dp0"

start "" "http://localhost:7777"
python serveur.py
pause
