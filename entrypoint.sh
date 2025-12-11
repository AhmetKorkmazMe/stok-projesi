#!/bin/sh

# 1. cron servisini arka planda başlat
echo "Starting cron daemon..."
cron

# 2. Gunicorn'u ön planda başlat (ana işlem bu olacak)
echo "Starting Gunicorn server..."
exec gunicorn --workers 4 --bind 0.0.0.0:5000 "app:app"
