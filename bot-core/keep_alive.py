"""
keep_alive.py — يشغّل لوحة التحكم الويب
"""
from web_panel import start_panel


def keep_alive(bot=None, db=None, port: int = 8080):
    return start_panel(bot, db, port)
