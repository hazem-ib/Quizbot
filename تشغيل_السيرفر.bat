@echo off
title Telegram Quiz Mini App Server
pip install -r requirements.txt
echo ========================================================
echo Server running on http://localhost:8000
echo Use ngrok (ngrok http 8000) to get an HTTPS link for Telegram!
echo ========================================================
python main.py
pause
