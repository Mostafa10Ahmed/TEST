"""
web_panel.py — لوحة تحكم Cat-Bi الاحترافية
تصميم كامل + تحكم بالبوت (restart/stop/status)
"""

import os
import sys
import datetime
import asyncio
import signal
import threading
import time
import json
from functools import wraps

from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify
from bot_config import Config

app = Flask(__name__)
app.secret_key = Config.WEB_PANEL_SECRET or "catbi-secret-2025"

_bot_ref    = None
_db_module  = None
_start_time = datetime.datetime.now(datetime.timezone.utc)
_dl_count   = 0     # عداد التحميلات (يُزاد من main.py)


def set_bot(bot, db):
    global _bot_ref, _db_module
    _bot_ref   = bot
    _db_module = db


def inc_download():
    global _dl_count
    _dl_count += 1


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def run_async(coro):
    """
    Safely bridge sync Flask requests to async DB calls.
    
    Uses the bot's event loop (via run_coroutine_threadsafe) when available.
    This avoids the 'attached to a different loop' error that happens when
    creating a new event loop while aiosqlite holds connections on the bot loop.
    Falls back to a temporary loop if no bot is running (startup/test context).
    """
    # If the bot is running, use its loop
    if _bot_ref is not None and hasattr(_bot_ref, 'loop') and _bot_ref.loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, _bot_ref.loop)
        try:
            return future.result(timeout=15)
        except Exception:
            return None
    # Fallback: standalone loop (used during startup when bot isn't ready yet)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════
#  HTML — الواجهة الكاملة
# ══════════════════════════════════════════════════════════════
LOGIN_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8"><title>Cat-Bi Panel — تسجيل الدخول</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@300;400;600;700;800&family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root{--acc:#8b5cf6;--acc2:#6d28d9;--dark:#07070a;--card-bg:rgba(15,15,25,.65);--card-border:rgba(255,255,255,.07)}
    body{background:radial-gradient(circle at 50% 50%,#151233 0%,#050508 100%);min-height:100vh;
         display:flex;align-items:center;justify-content:center;font-family:'Cairo','Outfit',sans-serif}
    .card{background:var(--card-bg);border:1px solid var(--card-border);border-radius:24px;
          padding:45px;width:380px;box-shadow:0 30px 70px rgba(0,0,0,.6);backdrop-filter:blur(15px);
          transition:transform .3s,box-shadow .3s}
    .card:hover{transform:translateY(-5px);box-shadow:0 35px 80px rgba(139,92,246,.25)}
    .brand{font-size:2.2rem;font-weight:800;background:linear-gradient(135deg,#fff,#8b5cf6);
           -webkit-background-clip:text;-webkit-text-fill-color:transparent;
           text-shadow:0 0 20px rgba(139,92,246,.2)}
    .form-control{background:rgba(0,0,0,.3);color:#f3f4f6;border:1px solid var(--card-border);border-radius:12px;padding:14px}
    .form-control:focus{background:rgba(0,0,0,.4);color:#f3f4f6;border-color:var(--acc);box-shadow:0 0 0 4px rgba(139,92,246,.2)}
    .btn-acc{background:linear-gradient(135deg,#8b5cf6,#6d28d9);color:#fff;border:none;
             border-radius:12px;padding:14px;font-weight:700;transition:.3s;width:100%;box-shadow:0 5px 15px rgba(139,92,246,.3)}
    .btn-acc:hover{transform:translateY(-2px);box-shadow:0 8px 25px rgba(139,92,246,.5);color:#fff}
    .alert{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.2);
           color:#fecaca;border-radius:12px;font-weight:600}
  </style>
</head>
<body>
<div class="card text-center">
  <div class="brand mb-2">🤖 Cat-Bi</div>
  <p class="text-muted mb-4" style="font-size:.9rem;letter-spacing:1px">لوحة تحكم البوت الاحترافية</p>
  {% if error %}<div class="alert mb-3 py-2">{{ error }}</div>{% endif %}
  <form method="post">
    <input type="password" name="password" class="form-control mb-3 text-center"
           placeholder="كلمة المرور" autocomplete="current-password" required>
    <button class="btn btn-acc">دخول <i class="bi bi-arrow-left-circle ms-1"></i></button>
  </form>
</div>
</body></html>"""


BASE_LAYOUT = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8"><title>Cat-Bi Panel — {page_title}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@300;400;600;700;800&family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root{{
      --acc: #8b5cf6;
      --acc2: #6d28d9;
      --acc-glow: rgba(139, 92, 246, 0.35);
      --dark: #0a0b10;
      --card-bg: rgba(17, 24, 39, 0.65);
      --card-border: rgba(255, 255, 255, 0.07);
      --card-blur: blur(12px);
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --green: #10b981;
      --red: #ef4444;
      --gold: #f59e0b;
      --blue: #3b82f6;
    }}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{
      background: radial-gradient(circle at 50% 50%, #151233 0%, #050508 100%);
      color: var(--text);
      font-family: 'Cairo', 'Outfit', sans-serif;
      display: flex;
      min-height: 100vh;
      overflow-x: hidden;
    }}

    /* ── Sidebar ── */
    .sidebar{{
      width: 250px;
      min-height: 100vh;
      background: rgba(17, 24, 39, 0.85);
      border-left: 1px solid var(--card-border);
      position: fixed;
      top: 0;
      right: 0;
      z-index: 1000;
      display: flex;
      flex-direction: column;
      backdrop-filter: blur(15px);
      box-shadow: -5px 0 25px rgba(0,0,0,0.5);
    }}
    .sidebar-brand{{
      padding: 24px 20px;
      border-bottom: 1px solid var(--card-border);
    }}
    .sidebar-brand .logo{{
      font-size: 1.6rem;
      font-weight: 800;
      background: linear-gradient(135deg, #fff 30%, var(--acc) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      text-shadow: 0 0 15px var(--acc-glow);
    }}
    .sidebar-brand .sub{{
      font-size: .75rem;
      color: var(--text-muted);
      margin-top: 4px;
    }}
    .sidebar-status{{
      margin: 15px 12px;
      background: rgba(255,255,255,0.03);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 10px 14px;
      font-size: .85rem;
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
    .dot.online{{background:var(--green);box-shadow:0 0 8px var(--green)}}
    .dot.offline{{background:var(--red);box-shadow:0 0 8px var(--red)}}
    .nav-section{{
      padding: 12px 18px 6px;
      font-size: .75rem;
      color: #555870;
      text-transform: uppercase;
      letter-spacing: .08em;
      font-weight: 700;
    }}
    .nav-link{{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 11px 18px;
      color: var(--text-muted);
      border-radius: 12px;
      margin: 2px 12px;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      font-size: .95rem;
      text-decoration: none;
      font-weight: 500;
    }}
    .nav-link i{{font-size:1.15rem;width:20px;text-align:center}}
    .nav-link:hover{{
      background: rgba(139, 92, 246, 0.1);
      color: #fff;
      transform: translateX(-4px);
    }}
    .nav-link.active{{
      background: linear-gradient(135deg, var(--acc), var(--acc2));
      color: #fff;
      box-shadow: 0 4px 15px var(--acc-glow);
    }}
    .sidebar-footer{{margin-top:auto;padding:15px;border-top:1px solid var(--card-border)}}

    /* ── Main ── */
    .main{{
      margin-right: 250px;
      padding: 30px;
      flex: 1;
      min-width: 0;
    }}
    .page-header{{margin-bottom:28px}}
    .page-header h1{{font-size:1.8rem;font-weight:800;display:flex;align-items:center;gap:12px}}
    .page-header .breadcrumb{{font-size:.85rem;color:var(--text-muted);margin-top:6px}}

    /* ── Cards ── */
    .card{{
      background: var(--card-bg);
      backdrop-filter: var(--card-blur);
      border: 1px solid var(--card-border);
      border-radius: 18px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35);
      transition: transform 0.3s ease, box-shadow 0.3s ease;
    }}
    .card:hover{{
      box-shadow: 0 12px 40px rgba(139, 92, 246, 0.18);
    }}
    .card-body{{padding:24px}}
    .card-title{{
      font-size: .95rem;
      color: var(--text-muted);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .06em;
      margin-bottom: 16px;
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    /* ── Stat Cards ── */
    .stat-card{{
      border-radius: 18px;
      padding: 24px;
      position: relative;
      overflow: hidden;
      background: var(--card-bg);
      backdrop-filter: var(--card-blur);
      border: 1px solid var(--card-border);
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
    }}
    .stat-card.purple{{
      background: linear-gradient(135deg, rgba(139, 92, 246, 0.12), rgba(139, 92, 246, 0.02));
      border: 1px solid rgba(139, 92, 246, 0.25);
    }}
    .stat-card.green{{
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.12), rgba(16, 185, 129, 0.02));
      border: 1px solid rgba(16, 185, 129, 0.25);
    }}
    .stat-card.blue{{
      background: linear-gradient(135deg, rgba(59, 130, 246, 0.12), rgba(59, 130, 246, 0.02));
      border: 1px solid rgba(59, 130, 246, 0.25);
    }}
    .stat-card.gold{{
      background: linear-gradient(135deg, rgba(245, 158, 11, 0.12), rgba(245, 158, 11, 0.02));
      border: 1px solid rgba(245, 158, 11, 0.25);
    }}
    .stat-num{{font-size:2.4rem;font-weight:800;line-height:1}}
    .stat-label{{font-size:.85rem;color:var(--text-muted);margin-top:8px;font-weight:600}}
    .stat-icon{{font-size:2.2rem;opacity:.35;position:absolute;left:20px;top:50%;transform:translateY(-50%)}}

    /* ── Controls ── */
    .ctrl-btn{{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 18px;
      border-radius: 12px;
      font-size: .9rem;
      font-weight: 700;
      border: none;
      cursor: pointer;
      transition: all 0.2s;
    }}
    .ctrl-btn:hover{{
      transform: translateY(-2px);
      box-shadow: 0 5px 15px rgba(0,0,0,0.4);
    }}
    .ctrl-restart{{background:rgba(245,158,11,.15);color:var(--gold);border:1px solid rgba(245,158,11,.3)}}
    .ctrl-stop{{background:rgba(239,68,68,.15);color:var(--red);border:1px solid rgba(239,68,68,.3)}}
    .ctrl-sync{{background:rgba(59,130,246,.15);color:var(--blue);border:1px solid rgba(59,130,246,.3)}}

    /* ── Tables ── */
    .table{{color:var(--text)}}
    .table th{{
      border-color: var(--card-border);
      color: var(--text-muted);
      font-size: .8rem;
      text-transform: uppercase;
      letter-spacing: .06em;
      font-weight: 700;
      padding: 12px 10px;
    }}
    .table td{{border-color:var(--card-border);vertical-align:middle;padding:15px 10px}}
    .table tbody tr:hover{{background:rgba(139,92,246,.05)}}

    /* ── Forms ── */
    .form-control,.form-select{{
      background: rgba(0,0,0,0.3);
      color: var(--text);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 10px 14px;
    }}
    .form-control:focus,.form-select:focus{{
      background: rgba(0,0,0,0.45);
      color: var(--text);
      border-color: var(--acc);
      box-shadow: 0 0 0 4px rgba(139,92,246,.25);
    }}
    .form-select option{{background:#11121d}}
    .btn-acc{{
      background: linear-gradient(135deg,var(--acc),var(--acc2));
      color: #fff;
      border: none;
      border-radius: 10px;
      padding: 10px 20px;
      font-weight: 700;
      transition: all 0.2s;
      box-shadow: 0 4px 12px var(--acc-glow);
    }}
    .btn-acc:hover{{
      transform: translateY(-2px);
      box-shadow: 0 6px 18px var(--acc-glow);
      color: #fff;
    }}
    .btn-danger-soft{{
      background: rgba(239,68,68,.12);
      color: var(--red);
      border: 1px solid rgba(239,68,68,.25);
      border-radius: 8px;
      padding: 6px 12px;
      font-size: .85rem;
      font-weight: 600;
      transition: all 0.2s;
    }}
    .btn-danger-soft:hover{{background:rgba(239,68,68,.25)}}

    /* ── Badges ── */
    .badge-online{{
      background: rgba(16, 185, 129, 0.15);
      color: var(--green);
      border: 1px solid rgba(16, 185, 129, 0.3);
      padding: 4px 12px;
      border-radius: 20px;
      font-size: .8rem;
      font-weight: 600;
    }}
    .badge-offline{{
      background: rgba(239, 68, 68, 0.15);
      color: var(--red);
      border: 1px solid rgba(239, 68, 68, 0.3);
      padding: 4px 12px;
      border-radius: 20px;
      font-size: .8rem;
      font-weight: 600;
    }}
    .badge-rank{{padding:4px 12px;border-radius:20px;font-size:.8rem;font-weight:600}}
    .badge-owner{{background:rgba(245,158,11,.15);color:var(--gold);border:1px solid rgba(245,158,11,.3)}}
    .badge-vip{{background:rgba(139,92,246,.15);color:#c4b5fd;border:1px solid rgba(139,92,246,.3)}}
    .badge-user{{background:rgba(59,130,246,.15);color:#93c5fd;border:1px solid rgba(59,130,246,.3)}}
    .badge-site{{
      background: rgba(139,92,246,.15);
      color: #c4b5fd;
      border: 1px solid rgba(139,92,246,.25);
      padding: 3px 10px;
      border-radius: 8px;
      font-size: .8rem;
      font-weight: 600;
    }}

    /* ── Logs ── */
    .log-box{{
      background: #06070a;
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 16px;
      max-height: 450px;
      overflow-y: auto;
      font-family: 'Consolas', monospace;
      font-size: .82rem;
      line-height: 1.5;
    }}
    .log-OK{{color:#4ade80}}.log-INFO{{color:#9ca3af}}.log-WARN{{color:#fbbf24}}.log-ERROR{{color:#f87171}}
    .log-box::-webkit-scrollbar{{width:6px}}
    .log-box::-webkit-scrollbar-track{{background:transparent}}
    .log-box::-webkit-scrollbar-thumb{{background:var(--card-border);border-radius:3px}}

    /* ── Toast ── */
    .toast-container{{position:fixed;bottom:25px;right:25px;z-index:9999}}
    .toast{{
      background: rgba(17, 24, 39, 0.9);
      backdrop-filter: blur(10px);
      border: 1px solid var(--card-border);
      color: var(--text);
      border-radius: 12px;
      font-size: .9rem;
      font-weight: 600;
      box-shadow: 0 10px 30px rgba(0,0,0,0.5);
    }}

    /* ── Divider ── */
    hr{{border-color:var(--card-border)}}
    code{{background:rgba(139,92,246,.12);color:#c4b5fd;padding:3px 8px;border-radius:6px;font-size:.85rem}}
    .text-muted{{color:var(--text-muted)!important}}

    /* ── Floating Panel ── */
    .floating-panel-trigger{{
      position: fixed;
      bottom: 25px;
      left: 25px;
      width: 56px;
      height: 56px;
      border-radius: 50%;
      background: linear-gradient(135deg, var(--acc), var(--acc2));
      box-shadow: 0 8px 25px var(--acc-glow);
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      z-index: 2000;
      transition: all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
    }}
    .floating-panel-trigger:hover{{
      transform: scale(1.1) rotate(15deg);
      box-shadow: 0 12px 30px var(--acc-glow);
    }}
    .floating-panel-trigger i{{
      color: #fff;
      font-size: 1.5rem;
    }}
    .pulse-ring{{
      border: 3px solid var(--acc);
      border-radius: 50%;
      position: absolute;
      height: 100%;
      width: 100%;
      animation: pulse 2s infinite;
      opacity: 0;
    }}
    @keyframes pulse{{
      0%{{ transform: scale(0.9); opacity: 0; }}
      50%{{ opacity: 0.5; }}
      100%{{ transform: scale(1.4); opacity: 0; }}
    }}
    .floating-panel{{
      position: fixed;
      bottom: 95px;
      left: 25px;
      width: 380px;
      max-height: 550px;
      background: rgba(10, 11, 16, 0.95);
      backdrop-filter: blur(20px);
      border: 1px solid var(--card-border);
      border-radius: 20px;
      box-shadow: 0 15px 50px rgba(0,0,0,0.6);
      z-index: 2000;
      opacity: 0;
      transform: translateY(30px) scale(0.9);
      pointer-events: none;
      transition: all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}
    .floating-panel.show{{
      opacity: 1;
      transform: translateY(0) scale(1);
      pointer-events: auto;
    }}
    .floating-panel-header{{
      padding: 16px 20px;
      background: rgba(139, 92, 246, 0.15);
      border-bottom: 1px solid var(--card-border);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .floating-panel-header h5{{
      margin: 0;
      font-size: 1rem;
      font-weight: 700;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .floating-panel-header button{{
      background: none;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      font-size: 1rem;
      transition: color 0.2s;
    }}
    .floating-panel-header button:hover{{color:#fff}}
    .floating-panel-body{{
      padding: 20px;
      overflow-y: auto;
      flex: 1;
    }}
    .floating-panel-body::-webkit-scrollbar{{width:4px}}
    .floating-panel-body::-webkit-scrollbar-thumb{{background:var(--card-border);border-radius:2px}}
    
    .section-title{{
      font-size: 0.72rem;
      color: var(--text-muted);
      text-transform: uppercase;
      font-weight: 700;
      letter-spacing: 0.08em;
      margin-bottom: 12px;
      margin-top: 15px;
      border-bottom: 1px solid rgba(255,255,255,0.03);
      padding-bottom: 4px;
    }}
    .section-title:first-child{{
      margin-top: 0;
    }}
    
    .quick-stat-box{{
      background: rgba(255,255,255,0.02);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 12px;
      text-align: center;
    }}
    .quick-stat-box .val{{
      display: block;
      font-size: 1.4rem;
      font-weight: 800;
      color: var(--acc);
    }}
    .quick-stat-box .lbl{{
      font-size: 0.72rem;
      color: var(--text-muted);
    }}
    
    .job-card{{
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 12px;
      margin-bottom: 10px;
    }}
    .job-card-header{{
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.8rem;
      font-weight: 600;
      margin-bottom: 6px;
    }}
    .job-progress-container{{
      height: 6px;
      background: rgba(255,255,255,0.07);
      border-radius: 3px;
      overflow: hidden;
      margin-bottom: 6px;
    }}
    .job-progress-bar{{
      height: 100%;
      background: linear-gradient(90deg, var(--acc), var(--blue));
      width: 0%;
      transition: width 0.4s ease;
    }}
    .job-card-footer{{
      display: flex;
      justify-content: space-between;
      font-size: 0.7rem;
      color: var(--text-muted);
    }}
  </style>
</head>
<body>
<!-- Sidebar -->
<div class="sidebar">
  <div class="sidebar-brand">
    <div class="logo">🤖 Cat-Bi</div>
    <div class="sub">Manga Bot Control Panel</div>
  </div>
  <div class="sidebar-status">
    <div class="dot {status_dot}"></div>
    <span style="font-size:.8rem;color:var(--text-muted)">{status_txt}</span>
  </div>
  <div class="nav-section">الرئيسية</div>
  <a class="nav-link {a_dash}" href="/"><i class="bi bi-speedometer2"></i> Dashboard</a>
  <div class="nav-section">إدارة</div>
  <a class="nav-link {a_users}" href="/users"><i class="bi bi-people-fill"></i> المستخدمون</a>
  <a class="nav-link {a_trackers}" href="/trackers"><i class="bi bi-radar"></i> الرادار</a>
  <a class="nav-link {a_sites}" href="/sites"><i class="bi bi-globe2"></i> المواقع</a>
  <a class="nav-link {a_selectors}" href="/selectors"><i class="bi bi-code-slash"></i> المحددات المخصصة</a>
  <div class="nav-section">النظام</div>
  <a class="nav-link {a_logs}" href="/logs"><i class="bi bi-terminal-fill"></i> السجلات</a>
  <div class="sidebar-footer">
    <a class="nav-link text-danger" href="/logout"><i class="bi bi-box-arrow-left"></i> خروج</a>
  </div>
</div>

<!-- Main -->
<div class="main">
  {msg_html}
  {content}
</div>

<!-- Floating Panel -->
<div class="floating-panel-trigger" id="floatTrigger" onclick="toggleFloatingPanel()">
  <i class="bi bi-radar"></i>
  <span class="pulse-ring"></span>
</div>

<div class="floating-panel" id="floatingPanel">
  <div class="floating-panel-header">
    <h5><i class="bi bi-cpu-fill"></i> مراقب التتبع المباشر</h5>
    <button onclick="toggleFloatingPanel()"><i class="bi bi-x-lg"></i></button>
  </div>
  <div class="floating-panel-body">
    <div class="section-title">📥 العمليات الجارية (Worker)</div>
    <div id="liveJobsList">
      <div class="text-center text-muted py-3" style="font-size:0.75rem;">لا توجد عمليات نشطة حالياً</div>
    </div>
    
    <div class="section-title">📊 إحصائيات عامة</div>
    <div class="row g-2">
      <div class="col-6">
        <div class="quick-stat-box">
          <span class="val" id="sseGuildTrackers">0</span>
          <span class="lbl">متتبع عام</span>
        </div>
      </div>
      <div class="col-6">
        <div class="quick-stat-box">
          <span class="val" id="sseUserTrackers">0</span>
          <span class="lbl">متتبع خاص</span>
        </div>
      </div>
    </div>
    
    <div class="section-title">⚡ إجراءات سريعة</div>
    <button class="w-100 btn btn-sm btn-outline-info mb-2" style="border-radius:8px;font-size:0.8rem;" onclick="syncRadarNow()">
      <i class="bi bi-arrow-repeat"></i> فحص فوري للرادار
    </button>
    <button class="w-100 btn btn-sm btn-outline-light" style="border-radius:8px;font-size:0.8rem;" onclick="location.reload()">
      <i class="bi bi-arrow-clockwise"></i> تحديث الواجهة
    </button>
  </div>
</div>

<!-- Toast container -->
<div class="toast-container" id="toastContainer"></div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
function showToast(msg, type='success') {{
  var tc = document.getElementById('toastContainer');
  if(!tc) return;
  var id = 'toast_' + Date.now();
  var color = type==='success' ? '#10b981' : type==='error' ? '#ef4444' : '#f59e0b';
  var icon = type==='success' ? 'bi-check-circle-fill' : type==='error' ? 'bi-exclamation-triangle-fill' : 'bi-info-circle-fill';
  tc.innerHTML += '<div id="'+id+'" class="toast show p-1" style="border-left:4px solid '+color+'">'+
    '<div class="toast-body d-flex align-items-center gap-2"><i class="bi '+icon+'" style="color:'+color+'"></i><span>'+msg+'</span></div></div>';
  setTimeout(function(){{ var el=document.getElementById(id); if(el) el.remove(); }}, 3000);
}}
function botAction(action) {{
  fetch('/bot/'+action, {{method:'POST'}})
    .then(r=>r.json()).then(d=>{{
      showToast(d.message || action, d.ok ? 'success' : 'error');
      if(action==='restart') setTimeout(()=>location.reload(), 5000);
    }}).catch(()=>showToast('خطأ في الاتصال','error'));
}}

function toggleFloatingPanel() {{
  var panel = document.getElementById('floatingPanel');
  if(panel) panel.classList.toggle('show');
}}

function syncRadarNow() {{
  showToast('جاري بدء فحص الرادار...', 'info');
  fetch('/bot/sync', {{method: 'POST'}})
    .then(r=>r.json()).then(d=>{{
      showToast(d.message || 'تم إرسال إشارة الفحص', d.ok ? 'success' : 'error');
    }}).catch(()=>showToast('فشل إرسال إشارة الفحص', 'error'));
}}

// SSE integration
var sseSource = new EventSource('/api/trackers/live-sse');
sseSource.onmessage = function(event) {{
  try {{
    var data = JSON.parse(event.data);
    if (document.getElementById('sseGuildTrackers')) document.getElementById('sseGuildTrackers').innerText = data.guild_trackers || 0;
    if (document.getElementById('sseUserTrackers')) document.getElementById('sseUserTrackers').innerText = data.user_trackers || 0;
    
    var jobsList = document.getElementById('liveJobsList');
    if (jobsList) {{
      var jobs = data.active_jobs || {{}};
      var keys = Object.keys(jobs);
      if (keys.length === 0) {{
        jobsList.innerHTML = '<div class="text-center text-muted py-3" style="font-size:0.75rem;">لا توجد عمليات نشطة حالياً</div>';
      }} else {{
        var html = '';
        keys.forEach(function(k) {{
          var job = jobs[k];
          var pct = job.progress || 0;
          var status = job.status || 'queued';
          var msg = job.message || 'Processing...';
          var title = job.title || 'Manga';
          var badgeColor = status === 'completed' ? 'var(--green)' : status === 'failed' ? 'var(--red)' : 'var(--blue)';
          
          html += '<div class="job-card">' +
              '<div class="job-card-header">' +
                '<span style="max-width:70%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#fff;">' + title + '</span>' +
                '<span class="badge" style="background:' + badgeColor + '; font-size:0.65rem;">' + status + '</span>' +
              '</div>' +
              '<div class="job-progress-container">' +
                '<div class="job-progress-bar" style="width:' + pct + '%"></div>' +
              '</div>' +
              '<div class="job-card-footer">' +
                '<span style="max-width:80%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">' + msg + '</span>' +
                '<span>' + pct + '%</span>' +
              '</div>' +
            '</div>';
        }});
        jobsList.innerHTML = html;
      }}
    }}
  }} catch(e) {{
    console.error('SSE JSON error', e);
  }}
}};
sseSource.onerror = function() {{
  console.log('SSE connection lost, retrying...');
}};
</script>
</body></html>"""


def _render(page: str, content: str, msg: str = "", **extra):
    active = {"a_dash": "", "a_users": "", "a_trackers": "", "a_sites": "", "a_selectors": "", "a_logs": ""}
    active[f"a_{page}"] = "active"

    bot_ok   = _bot_ref is not None and not _bot_ref.is_closed()
    status_dot = "online" if bot_ok else "offline"
    status_txt = (_bot_ref.user.name if bot_ok else "غير متصل") if _bot_ref else "جاري التحميل..."

    msg_html = ""
    if msg:
        kind = "success" if not msg.startswith("❌") else "danger"
        msg_html = f'<div class="alert alert-{kind} alert-dismissible fade show" role="alert">{msg}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>'

    page_titles = {
        "dash": "Dashboard", "users": "المستخدمون",
        "trackers": "الرادار", "sites": "المواقع", "selectors": "المحددات المخصصة", "logs": "السجلات"
    }

    return BASE_LAYOUT.format(
        page_title=page_titles.get(page, "Cat-Bi"),
        status_dot=status_dot, status_txt=status_txt,
        msg_html=msg_html, content=content,
        **active
    )


# ══════════════════════════════════════════════════════════════
#  Auth Routes
# ══════════════════════════════════════════════════════════════
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == Config.WEB_PANEL_SECRET:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        return render_template_string(LOGIN_HTML, error="❌ كلمة المرور خاطئة")
    return render_template_string(LOGIN_HTML, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ══════════════════════════════════════════════════════════════
#  Bot Control API
# ══════════════════════════════════════════════════════════════
@app.route("/bot/restart", methods=["POST"])
@login_required
def bot_restart():
    def _do_restart():
        time.sleep(0.8)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"ok": True, "message": "🔄 البوت سيُعاد تشغيله خلال ثوانٍ..."})


@app.route("/bot/stop", methods=["POST"])
@login_required
def bot_stop():
    if _bot_ref and not _bot_ref.is_closed():
        asyncio.run_coroutine_threadsafe(_bot_ref.close(), _bot_ref.loop)
    return jsonify({"ok": True, "message": "⏹️ تم إيقاف البوت"})


@app.route("/health")
def health():
    bot_ok = _bot_ref is not None and not _bot_ref.is_closed()
    return jsonify({"status": "ok" if bot_ok else "starting", "bot": bot_ok}), 200


@app.route("/bot/sync", methods=["POST"])
@login_required
def bot_sync():
    """Trigger a manual radar check now (non-blocking)."""
    try:
        if _bot_ref and not _bot_ref.is_closed():
            # Try to find and call the radar cog's check function
            for cog_name in ["Radar", "RadarCog", "radar"]:
                cog = _bot_ref.get_cog(cog_name)
                if cog and hasattr(cog, "_check_all"):
                    asyncio.run_coroutine_threadsafe(cog._check_all(), _bot_ref.loop)
                    return jsonify({"ok": True, "message": "Radar sync triggered"})
        return jsonify({"ok": False, "message": "Radar cog not found or bot offline"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/trackers/live-sse")
@login_required
def live_sse():
    def event_stream():
        while True:
            # 1. عدد متتبعات السيرفر
            guild_trackers = run_async(_db_module.get_tracker_count()) if _db_module else 0
            
            # 2. عدد متتبعات الأفراد
            user_trackers = run_async(_db_module.get_all_user_trackers_count()) if _db_module else 0

            # 3. المهام النشطة من خادم الـ Worker
            active_jobs = {}
            if _bot_ref and hasattr(_bot_ref, "remote_down") and _bot_ref.remote_down.is_enabled:
                try:
                    jobs_res = run_async(_bot_ref.remote_down.get_all_jobs())
                    if jobs_res and isinstance(jobs_res, dict) and "error" not in jobs_res:
                        active_jobs = {
                            jid: job for jid, job in jobs_res.items() 
                            if job.get("status") in ("running", "queued")
                        }
                except Exception:
                    pass

            data = {
                "guild_trackers": guild_trackers,
                "user_trackers": user_trackers,
                "active_jobs": active_jobs,
            }
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(4)

    return Response(event_stream(), mimetype="text/event-stream")


@app.route("/api/trackers/toggle", methods=["POST"])
@login_required
def api_tracker_toggle():
    try:
        data = request.get_json() or {}
        tid = data.get("tracker_id")
        gid = data.get("guild_id")
        paused = data.get("paused", 0)
        if tid is not None and gid is not None:
            run_async(_db_module.set_tracker_paused(int(tid), int(gid), int(paused)))
            status_str = "موقوف" if paused else "نشط"
            return jsonify({"ok": True, "message": f"تم تغيير حالة المتتبع إلى {status_str}"})
        return jsonify({"ok": False, "message": "معاملات غير صالحة"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/trackers/remove", methods=["POST"])
@login_required
def api_tracker_remove():
    try:
        data = request.get_json() or {}
        tid = data.get("tracker_id")
        gid = data.get("guild_id")
        if tid is not None and gid is not None:
            ok = run_async(_db_module.remove_tracker(int(tid), int(gid)))
            if ok:
                return jsonify({"ok": True, "message": "تم حذف المتتبع بنجاح"})
            return jsonify({"ok": False, "message": "المتتبع غير موجود"})
        return jsonify({"ok": False, "message": "معاملات غير صالحة"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})



# ══════════════════════════════════════════════════════════════
#  Dashboard
# ══════════════════════════════════════════════════════════════
@app.route("/")
@login_required
def dashboard():
    bot_ok      = _bot_ref is not None and not _bot_ref.is_closed()
    bot_name    = str(_bot_ref.user) if bot_ok else "—"
    guild_count = len(_bot_ref.guilds) if bot_ok else 0
    uptime      = str(datetime.datetime.now(datetime.timezone.utc) - _start_time).split(".")[0]

    user_count    = run_async(_db_module.get_user_count())   if _db_module else 0
    tracker_count = run_async(_db_module.get_tracker_count()) if _db_module else 0
    custom_sites  = run_async(_db_module.get_custom_sites()) if _db_module else []
    logs          = run_async(_db_module.get_recent_logs(12)) if _db_module else []

    content = f"""
<div class="page-header">
  <h1><i class="bi bi-speedometer2"></i> Dashboard</h1>
  <div class="breadcrumb">نظرة عامة على حالة البوت</div>
</div>

<!-- Bot Controls -->
<div class="card mb-4">
  <div class="card-body">
    <div class="d-flex align-items-center justify-content-between flex-wrap gap-3">
      <div class="d-flex align-items-center gap-3">
        <div>
          <div style="font-weight:700;font-size:1.1rem">{bot_name}</div>
          <div class="text-muted" style="font-size:.8rem">Discord Bot</div>
        </div>
        {'<span class="badge-online">🟢 Online</span>' if bot_ok else '<span class="badge-offline">🔴 Offline</span>'}
      </div>
      <div class="d-flex gap-2">
        <button class="ctrl-btn ctrl-restart" onclick="botAction('restart')">
          <i class="bi bi-arrow-clockwise"></i> Restart
        </button>
        <button class="ctrl-btn ctrl-stop" onclick="botAction('stop')">
          <i class="bi bi-stop-circle"></i> Stop
        </button>
      </div>
    </div>
  </div>
</div>

<!-- Stats -->
<div class="row g-3 mb-4">
  <div class="col-6 col-lg-3">
    <div class="stat-card purple">
      <i class="bi bi-people-fill stat-icon" style="color:#a78bfa"></i>
      <div class="stat-num" style="color:#a78bfa">{user_count}</div>
      <div class="stat-label">المستخدمون</div>
    </div>
  </div>
  <div class="col-6 col-lg-3">
    <div class="stat-card green">
      <i class="bi bi-radar stat-icon" style="color:#4ade80"></i>
      <div class="stat-num" style="color:#4ade80">{tracker_count}</div>
      <div class="stat-label">متتبعات الرادار</div>
    </div>
  </div>
  <div class="col-6 col-lg-3">
    <div class="stat-card blue">
      <i class="bi bi-globe2 stat-icon" style="color:#93c5fd"></i>
      <div class="stat-num" style="color:#93c5fd">{len(custom_sites)}</div>
      <div class="stat-label">مواقع مخصصة</div>
    </div>
  </div>
  <div class="col-6 col-lg-3">
    <div class="stat-card gold">
      <i class="bi bi-server stat-icon" style="color:#fbbf24"></i>
      <div class="stat-num" style="color:#fbbf24">{guild_count}</div>
      <div class="stat-label">السيرفرات</div>
    </div>
  </div>
</div>

<!-- Info + Logs -->
<div class="row g-3">
  <div class="col-md-5">
    <div class="card h-100">
      <div class="card-body">
        <div class="card-title"><i class="bi bi-info-circle"></i> معلومات النظام</div>
        <table class="table table-sm mb-0">
          <tr><td class="text-muted">وقت التشغيل</td><td><code>{uptime}</code></td></tr>
          <tr><td class="text-muted">التحميلات</td><td><code>{_dl_count}</code></td></tr>
          <tr><td class="text-muted">Gofile</td><td><code>{'OK' if Config.GOFILE_TOKEN else '—'}</code></td></tr>
          <tr><td class="text-muted">Guild ID</td><td><code>{Config.GUILD_ID or 'Global'}</code></td></tr>
        </table>
      </div>
    </div>
  </div>
  <div class="col-md-7">
    <div class="card h-100">
      <div class="card-body">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div class="card-title mb-0"><i class="bi bi-activity"></i> آخر السجلات</div>
          <a href="/logs" style="color:#a78bfa;font-size:.8rem">عرض الكل →</a>
        </div>
        <div class="log-box">
          {''.join(f'<div class="log-{lv}">[{ts[:19]}] {msg}</div>' for lv,msg,ts in logs) or '<span class="text-muted">لا توجد سجلات</span>'}
        </div>
      </div>
    </div>
  </div>
</div>
"""
    return _render("dash", content)


# ══════════════════════════════════════════════════════════════
#  Users
# ══════════════════════════════════════════════════════════════
@app.route("/users")
@login_required
def users_page():
    users = run_async(_db_module.get_all_users()) if _db_module else []
    msg   = request.args.get("msg", "")

    def rank_badge(rank):
        if rank >= 3: return '<span class="badge-rank badge-owner">👑 Owner</span>'
        if rank == 2: return '<span class="badge-rank badge-vip">⭐ VIP</span>'
        return '<span class="badge-rank badge-user">👤 User</span>'

    rows = "".join(f"""
      <tr>
        <td><code>{uid}</code></td>
        <td>{rank_badge(rank)}</td>
        <td class="text-muted">{note or '—'}</td>
        <td class="text-muted" style="font-size:.8rem">{(added or '')[:10]}</td>
        <td>
          <form method="post" action="/users/remove" style="display:inline">
            <input type="hidden" name="user_id" value="{uid}">
            <button class="btn-danger-soft" onclick="return confirm('حذف المستخدم {uid}؟')">
              <i class="bi bi-trash3"></i>
            </button>
          </form>
        </td>
      </tr>""" for uid,rank,note,added in users) or '<tr><td colspan="5" class="text-center text-muted py-3">لا يوجد مستخدمون</td></tr>'

    content = f"""
<div class="page-header">
  <h1><i class="bi bi-people-fill"></i> المستخدمون</h1>
  <div class="breadcrumb">{len(users)} مستخدم مسجّل</div>
</div>
<div class="card mb-3">
  <div class="card-body">
    <div class="card-title"><i class="bi bi-person-plus"></i> إضافة / تعديل مستخدم</div>
    <form method="post" action="/users/add">
      <div class="row g-2">
        <div class="col-md-4">
          <input name="user_id" class="form-control" placeholder="Discord User ID" required>
        </div>
        <div class="col-md-3">
          <select name="rank" class="form-select">
            <option value="1">👤 User</option>
            <option value="2">⭐ VIP</option>
          </select>
        </div>
        <div class="col-md-3">
          <input name="note" class="form-control" placeholder="ملاحظة (اختياري)">
        </div>
        <div class="col-md-2">
          <button class="btn btn-acc w-100">إضافة</button>
        </div>
      </div>
    </form>
  </div>
</div>
<div class="card">
  <div class="card-body">
    <div class="table-responsive">
      <table class="table table-hover mb-0">
        <thead><tr>
          <th>User ID</th><th>الرتبة</th><th>ملاحظة</th><th>تاريخ الإضافة</th><th>إجراء</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
</div>"""
    return _render("users", content, msg)


@app.route("/users/add", methods=["POST"])
@login_required
def users_add():
    uid  = request.form.get("user_id", "").strip()
    rank = int(request.form.get("rank", 1))
    note = request.form.get("note", "").strip()
    if uid.isdigit():
        run_async(_db_module.set_user_rank(int(uid), rank, note or "Added via panel"))
    return redirect(url_for("users_page", msg="✅ تم إضافة المستخدم"))


@app.route("/users/remove", methods=["POST"])
@login_required
def users_remove():
    uid = request.form.get("user_id", "").strip()
    if uid.isdigit():
        run_async(_db_module.remove_user(int(uid)))
    return redirect(url_for("users_page", msg="🗑️ تم حذف المستخدم"))


# ══════════════════════════════════════════════════════════════
#  Trackers
# ══════════════════════════════════════════════════════════════
@app.route("/trackers")
@login_required
def trackers_page():
    trackers = run_async(_db_module.get_all_trackers()) if _db_module else []
    msg      = request.args.get("msg", "")

    rows_html = []
    for row in trackers:
        # Safe column access with defaults — resilient to schema migrations
        def _col(idx, default=""):
            try:
                return row[idx] if len(row) > idx and row[idx] is not None else default
            except Exception:
                return default

        tid      = _col(0, "?")
        gid      = _col(1, "?")
        cid      = _col(2, "?")
        url      = _col(3, "")
        lch      = _col(4, "—")
        interval = _col(6, "?")
        last_raw = _col(7, "")
        dl       = bool(_col(8, 0))
        paused   = bool(_col(9, 0))

        # Format URL display
        url_short = (url[-50:] if len(url) > 50 else url)

        # Format last check time
        last_display = str(last_raw)[:16] if last_raw else "—"

        # Status badge
        if paused:
            status_html = '<span class="badge-rank" style="background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.3);padding:3px 8px;border-radius:20px;font-size:.72rem">موقوف</span>'
        else:
            status_html = '<span class="badge-rank" style="background:rgba(34,197,94,.15);color:#86efac;border:1px solid rgba(34,197,94,.3);padding:3px 8px;border-radius:20px;font-size:.72rem">نشط</span>'

        # DL badge
        dl_html = '<span style="color:#4ade80">&#10003;</span>' if dl else '<span style="color:#64748b">&#10007;</span>'

        rows_html.append(f"""
      <tr>
        <td><code style="font-size:.75rem">{tid}</code></td>
        <td style="max-width:220px">
          <a href="{url}" target="_blank" style="color:#a78bfa;font-size:.82rem;word-break:break-all">{url_short}</a>
        </td>
        <td><code style="font-size:.72rem;color:#94a3b8">{cid}</code></td>
        <td><span style="color:#e2e8f0;font-weight:600">{lch}</span></td>
        <td><span class="badge-site">{interval}m</span></td>
        <td>{dl_html}</td>
        <td>{status_html}</td>
        <td style="font-size:.75rem;color:#64748b">{last_display}</td>
        <td>
          <form method="post" action="/trackers/remove" style="display:inline">
            <input type="hidden" name="tracker_id" value="{tid}">
            <input type="hidden" name="guild_id" value="{gid}">
            <button class="btn-danger-soft" onclick="return confirm('Delete tracker {tid}?')">
              <i class="bi bi-trash3"></i>
            </button>
          </form>
        </td>
      </tr>""")
    rows = "".join(rows_html) or '<tr><td colspan="9" class="text-center text-muted py-4">No active trackers</td></tr>'

    content = f"""
<div class="page-header">
  <h1><i class="bi bi-radar"></i> الرادار</h1>
  <div class="breadcrumb">{len(trackers)} متتبعة نشطة</div>
</div>
<div class="card">
  <div class="card-body">
    <div class="table-responsive">
      <table class="table table-hover mb-0" style="font-size:.88rem">
        <thead><tr>
          <th>ID</th><th>الرابط</th><th>القناة</th><th>آخر فصل</th><th>الفترة</th><th>DL</th><th>الحالة</th><th>آخر فحص</th><th>إجراء</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
</div>"""
    return _render("trackers", content, msg)


@app.route("/trackers/remove", methods=["POST"])
@login_required
def trackers_remove():
    tid = int(request.form.get("tracker_id", 0))
    gid = int(request.form.get("guild_id", 0))
    run_async(_db_module.remove_tracker(tid, gid))
    return redirect(url_for("trackers_page", msg="🗑️ تم حذف المتتبع"))


# ══════════════════════════════════════════════════════════════
#  Custom Sites
# ══════════════════════════════════════════════════════════════
@app.route("/sites")
@login_required
def sites_page():
    sites = run_async(_db_module.get_custom_sites()) if _db_module else []
    msg   = request.args.get("msg", "")

    def type_badge(t):
        colors = {"madara": "#7c5cfc", "arabic": "#f59e0b", "generic": "#22c55e"}
        c = colors.get(t, "#94a3b8")
        return f'<span style="background:rgba(124,92,252,.1);color:{c};border:1px solid {c}44;padding:2px 8px;border-radius:6px;font-size:.75rem">{t}</span>'

    rows = "".join(f"""
      <tr>
        <td><code>{domain}</code></td>
        <td>{type_badge(stype)}</td>
        <td><code style="font-size:.75rem">{by or '—'}</code></td>
        <td class="text-muted" style="font-size:.8rem">{(at or '')[:10]}</td>
        <td class="text-muted" style="font-size:.8rem;max-width:200px">{notes or '—'}</td>
        <td>
          <form method="post" action="/sites/remove" style="display:inline">
            <input type="hidden" name="domain" value="{domain}">
            <button class="btn-danger-soft" onclick="return confirm('حذف {domain}؟')">
              <i class="bi bi-trash3"></i>
            </button>
          </form>
        </td>
      </tr>""" for domain,stype,by,at,notes in sites) or '<tr><td colspan="6" class="text-center text-muted py-3">لا توجد مواقع مخصصة</td></tr>'

    content = f"""
<div class="page-header">
  <h1><i class="bi bi-globe2"></i> المواقع المخصصة</h1>
  <div class="breadcrumb">{len(sites)} موقع مضاف</div>
</div>
<div class="card mb-3">
  <div class="card-body">
    <div class="card-title"><i class="bi bi-plus-circle"></i> إضافة موقع يدوياً</div>
    <form method="post" action="/sites/add">
      <div class="row g-2">
        <div class="col-md-4">
          <input name="domain" class="form-control" placeholder="domain.com" required>
        </div>
        <div class="col-md-3">
          <select name="site_type" class="form-select">
            <option value="madara">⚡ Madara (WordPress)</option>
            <option value="arabic">🇸🇦 Arabic</option>
            <option value="generic">🌐 Generic</option>
          </select>
        </div>
        <div class="col-md-3">
          <input name="notes" class="form-control" placeholder="ملاحظة">
        </div>
        <div class="col-md-2">
          <button class="btn btn-acc w-100">إضافة</button>
        </div>
      </div>
    </form>
  </div>
</div>
<div class="card">
  <div class="card-body">
    <div class="table-responsive">
      <table class="table table-hover mb-0">
        <thead><tr>
          <th>الدومين</th><th>النوع</th><th>أضيف بواسطة</th><th>التاريخ</th><th>ملاحظة</th><th>حذف</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
</div>"""
    return _render("sites", content, msg)


@app.route("/sites/add", methods=["POST"])
@login_required
def sites_add():
    domain    = request.form.get("domain", "").strip().lower()
    site_type = request.form.get("site_type", "madara")
    notes     = request.form.get("notes", "").strip()
    if domain:
        run_async(_db_module.add_custom_site(domain, site_type, 0, notes or "Added via panel"))
        if _bot_ref and hasattr(_bot_ref, "provider_mgr"):
            run_async(_bot_ref.provider_mgr.reload_custom_sites())
    return redirect(url_for("sites_page", msg=f"✅ تم إضافة {domain}"))


@app.route("/sites/remove", methods=["POST"])
@login_required
def sites_remove():
    domain = request.form.get("domain", "").strip()
    if domain:
        run_async(_db_module.remove_custom_site(domain))
        if _bot_ref and hasattr(_bot_ref, "provider_mgr"):
            run_async(_bot_ref.provider_mgr.reload_custom_sites())
    return redirect(url_for("sites_page", msg=f"🗑️ تم حذف {domain}"))


# ══════════════════════════════════════════════════════════════
#  Custom Selectors Page
# ══════════════════════════════════════════════════════════════
@app.route("/selectors")
@login_required
def selectors_page():
    rules = run_async(_db_module.get_custom_selector_rules()) if _db_module else []
    msg   = request.args.get("msg", "")

    rows = "".join(f"""
      <tr>
        <td><code>{domain}</code></td>
        <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{selector or ''}"><code>{selector or '—'}</code></td>
        <td><code>{url_attr or 'href'}</code></td>
        <td><code>{num_re or '—'}</code></td>
        <td>{'أول عنصر' if get_first else 'آخر عنصر'}</td>
        <td><span class="badge bg-{'success' if use_browser else 'secondary'}">{'Playwright' if use_browser else 'HTTP'}</span></td>
        <td style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{notes or ''}">{notes or '—'}</td>
        <td style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{raw_config or ''}"><code>{raw_config or '—'}</code></td>
        <td>
          <form method="post" action="/selectors/remove" style="display:inline">
            <input type="hidden" name="domain" value="{domain}">
            <button class="btn-danger-soft" onclick="return confirm('حذف محدد {domain}؟')">
              <i class="bi bi-trash3"></i>
            </button>
          </form>
        </td>
      </tr>""" for domain,selector,url_attr,num_re,get_first,use_browser,notes,raw_config,updated_at in rules) or '<tr><td colspan="9" class="text-center text-muted py-3">لا توجد محددات مخصصة</td></tr>'

    content = f"""
<div class="page-header">
  <h1><i class="bi bi-code-slash"></i> المحددات المخصصة</h1>
  <div class="breadcrumb">{len(rules)} محدد مضاف</div>
</div>
<div class="card mb-3">
  <div class="card-body">
    <div class="card-title"><i class="bi bi-plus-circle"></i> إضافة محدد مخصص</div>
    <form method="post" action="/selectors/add">
      <div class="row g-2">
        <div class="col-md-3">
          <input name="domain" class="form-control" placeholder="domain.com" required>
        </div>
        <div class="col-md-3">
          <input name="selector" class="form-control" placeholder="css:.chapter-list a or xpath://a" required>
        </div>
        <div class="col-md-2">
          <input name="url_attr" class="form-control" placeholder="url_attr (e.g. href)" value="href">
        </div>
        <div class="col-md-2">
          <input name="number_regex" class="form-control" placeholder="Regex رقم الفصل">
        </div>
        <div class="col-md-2">
          <select name="get_first" class="form-select">
            <option value="0">آخر عنصر (الأحدث)</option>
            <option value="1">أول عنصر (القديم)</option>
          </select>
        </div>
        <div class="col-md-2">
          <select name="use_browser" class="form-select">
            <option value="0">طلب HTTP عادي</option>
            <option value="1">تشغيل Playwright</option>
          </select>
        </div>
        <div class="col-md-3">
          <input name="notes" class="form-control" placeholder="ملاحظات">
        </div>
        <div class="col-md-5">
          <textarea name="raw_config" class="form-control" placeholder='JSON Config (مثال: {{"item":".ch", "image_selector":".img"}})' style="height:38px;padding:8px"></textarea>
        </div>
        <div class="col-md-2">
          <button class="btn btn-acc w-100">حفظ المحدد</button>
        </div>
      </div>
    </form>
  </div>
</div>
<div class="card">
  <div class="card-body">
    <div class="table-responsive">
      <table class="table table-hover mb-0">
        <thead><tr>
          <th>الدومين</th><th>المحدد</th><th>خاصية الرابط</th><th>Regex الرقم</th><th>ترتيب المطابقة</th><th>المحرك</th><th>ملاحظة</th><th>JSON Config</th><th>حذف</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
</div>"""
    return _render("selectors", content, msg)


@app.route("/selectors/add", methods=["POST"])
@login_required
def selectors_add():
    domain       = request.form.get("domain", "").strip().lower()
    selector     = request.form.get("selector", "").strip()
    url_attr     = request.form.get("url_attr", "href").strip()
    number_regex = request.form.get("number_regex", "").strip()
    get_first    = int(request.form.get("get_first", 0))
    use_browser  = int(request.form.get("use_browser", 0))
    notes        = request.form.get("notes", "").strip()
    raw_config   = request.form.get("raw_config", "").strip()
    
    if domain and (selector or raw_config):
        run_async(_db_module.set_custom_selector_rule(
            domain, selector, url_attr, number_regex, get_first, use_browser, notes, raw_config
        ))
        # Reload custom sites/selectors dynamically inside the bot
        if _bot_ref and hasattr(_bot_ref, "provider_mgr"):
            run_async(_bot_ref.provider_mgr.reload_custom_sites())
            
    return redirect(url_for("selectors_page", msg=f"✅ تم حفظ محدد {domain}"))


@app.route("/selectors/remove", methods=["POST"])
@login_required
def selectors_remove():
    domain = request.form.get("domain", "").strip()
    if domain:
        run_async(_db_module.remove_custom_selector_rule(domain))
        # Reload custom sites/selectors dynamically inside the bot
        if _bot_ref and hasattr(_bot_ref, "provider_mgr"):
            run_async(_bot_ref.provider_mgr.reload_custom_sites())
            
    return redirect(url_for("selectors_page", msg=f"🗑️ تم حذف محدد {domain}"))


# ══════════════════════════════════════════════════════════════
#  Logs
# ══════════════════════════════════════════════════════════════
@app.route("/logs")
@login_required
def logs_page():
    logs = run_async(_db_module.get_recent_logs(300)) if _db_module else []
    log_html = "".join(
        f'<div class="log-{lv}">[{ts[:19]}] <span style="opacity:.6">[{lv}]</span> {msg}</div>'
        for lv, msg, ts in logs
    ) or '<span class="text-muted">لا توجد سجلات</span>'

    content = f"""
<div class="page-header d-flex justify-content-between align-items-start">
  <div>
    <h1><i class="bi bi-terminal-fill"></i> السجلات</h1>
    <div class="breadcrumb">{len(logs)} سجل</div>
  </div>
  <button onclick="location.reload()" class="ctrl-btn ctrl-sync">
    <i class="bi bi-arrow-clockwise"></i> تحديث
  </button>
</div>
<div class="card">
  <div class="card-body p-0">
    <div class="log-box" id="logBox" style="border-radius:14px;max-height:600px">
      {log_html}
    </div>
  </div>
</div>
<script>
  var lb = document.getElementById('logBox');
  lb.scrollTop = lb.scrollHeight;
  setTimeout(function(){{ location.reload(); }}, 20000);
</script>"""
    return _render("logs", content)


# ══════════════════════════════════════════════════════════════
#  Runner
# ══════════════════════════════════════════════════════════════
def run_panel(port: int = 8080):
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def start_panel(bot, db, port: int = 8080):
    set_bot(bot, db)
    t = threading.Thread(target=run_panel, args=(port,), daemon=True)
    t.start()
    return t
