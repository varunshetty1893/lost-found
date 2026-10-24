@echo off
cd /d "%~dp0"
if not exist venv (
  python -m venv venv
  call venv\Scripts\activate
  pip install -r requirements.txt
) else (
  call venv\Scripts\activate
)
if not exist .env copy .env.example .env >nul
echo Open http://127.0.0.1:5000  (admin login: admin@campus.edu, password from ADMIN_PASSWORD in .env)
python app.py
pause
