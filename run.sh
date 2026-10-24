#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ ! -d venv ]; then
  python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
else
  source venv/bin/activate
fi
[ -f .env ] || cp .env.example .env
echo "Open http://127.0.0.1:5000  (admin login: admin@campus.edu, password from ADMIN_PASSWORD in .env)"
python app.py
