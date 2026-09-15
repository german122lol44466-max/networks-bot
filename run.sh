#!/bin/bash
cd "$(dirname "$0")"
[ -f .env ] || { echo "Нет файла .env. Скопируй .env.example в .env и вставь токен."; exit 1; }
python3 -m pip install -r requirements.txt -q
while true; do python3 bot.py; echo "Бот остановился, перезапуск через 5 сек..."; sleep 5; done
