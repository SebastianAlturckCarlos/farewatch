@echo off
cd /d "%~dp0"
python -m pip install --quiet -r requirements.txt
python app.py
pause
