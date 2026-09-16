# -*- coding: utf-8 -*-
"""Запуск sabzNotifierBot на Windows без окна консоли. Лог рядом, в bot.log.

Токен и настройки берутся из .env рядом с этим файлом (см. .env.example).
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("BOT_DATA", os.path.join(HERE, "data"))
os.makedirs(os.environ["BOT_DATA"], exist_ok=True)

LOG = os.path.join(HERE, "bot.log")
try:
    if os.path.exists(LOG) and os.path.getsize(LOG) > 5 * 1024 * 1024:
        os.replace(LOG, LOG + ".1")
except Exception:
    pass
sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)

sys.path.insert(0, HERE)

try:
    import bot          # bot.py сам прочитает .env
    bot.main()
except BaseException:
    traceback.print_exc()
    sys.exit(1)
