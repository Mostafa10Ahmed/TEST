import os
import sys

# حقن البروكسي في أعلى الملف لضمان استخدامه من جميع المكتبات
PROXY = os.getenv("PROXY")
if PROXY:
    os.environ["HTTP_PROXY"] = PROXY
    os.environ["HTTPS_PROXY"] = PROXY
    print(f"🌐 [GLOBAL] Proxy injected at startup: {PROXY}")

import discord
from discord.ext import commands
import asyncio
import aiohttp
import datetime
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# --- MONKEY PATCH FOR discord.ui.LayoutView BUTTONS DECORATOR BUG ---
try:
    import discord.ui.view
    orig_init_subclass = discord.ui.LayoutView.__init_subclass__
    def patched_init_subclass(cls):
        super(discord.ui.LayoutView, cls).__init_subclass__()
        children = {}
        callback_children = {}
        for base in reversed(cls.__mro__):
            for name, member in base.__dict__.items():
                if isinstance(member, discord.ui.Item):
                    if member._parent is not None:
                        continue
                    member._rendered_row = member._row
                    children[name] = member
                elif hasattr(member, '__discord_ui_model_type__'):
                    callback_children[name] = member
        children.update(callback_children)
        cls.__view_children_items__ = children
    discord.ui.LayoutView.__init_subclass__ = classmethod(patched_init_subclass)

    # Patch for to_components to auto-wrap standard buttons/selects into ActionRows
    def patched_to_components(self):
        from itertools import groupby
        components = []
        standard_items = []
        
        def group_standard_items(items):
            def key(item) -> int:
                return getattr(item, "_rendered_row", 0) or getattr(item, "row", 0) or 0

            sorted_items = sorted(items, key=key)
            grouped_components = []
            for _, group in groupby(sorted_items, key=key):
                group_dicts = [item.to_component_dict() for item in group]
                if not group_dicts:
                    continue
                grouped_components.append({
                    'type': 1,
                    'components': group_dicts,
                })
            return grouped_components

        for item in self._children:
            comp_dict = item.to_component_dict()
            comp_type = comp_dict.get("type")
            if comp_type in (2, 3, 5, 6, 7, 8):
                standard_items.append(item)
            else:
                if standard_items:
                    components.extend(group_standard_items(standard_items))
                    standard_items = []
                components.append(comp_dict)
                
        if standard_items:
            components.extend(group_standard_items(standard_items))
            
        return components

    discord.ui.LayoutView.to_components = patched_to_components
    print("🩹 [Patch] discord.ui.LayoutView decorator and serialization bug successfully patched.")
except Exception as e:
    print(f"⚠️ [Patch] Failed to patch discord.ui.LayoutView: {e}")
# -------------------------------------------------------------

from bot_config import Config
import database
from keep_alive import keep_alive
from manga_downloader import MangaDownloader
from remote_downloader import RemoteDownloader
from providers.manager import ProviderManager
from user_system import get_rank
from services.worker_sync import sync_custom_data_to_worker
from services.metrics import RuntimeMetrics

C_BLUE   = discord.Color.from_rgb(88, 101, 242)
C_RED    = discord.Color.from_rgb(237, 66, 69)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot          = commands.Bot(command_prefix="!", intents=intents)
downloader   = MangaDownloader()
remote_down  = RemoteDownloader()
bot.remote_down = remote_down
bot.downloader = downloader
provider_mgr = ProviderManager()
bot.provider_mgr = provider_mgr
bot.metrics = RuntimeMetrics()

registered_cache: set = set()
BOT_START_TIME = datetime.datetime.now(datetime.timezone.utc)
bot.start_time = BOT_START_TIME


async def setup_hook():
    await database.init_db()
    await bot.load_extension("radar")
    await bot.load_extension("cogs.general")
    await bot.load_extension("cogs.admin")
    await bot.load_extension("cogs.tracker_v3")
    await bot.load_extension("cogs.downloads")
    await bot.load_extension("cogs.manga_cleaner")
    await bot.load_extension("cogs.manga_translator")
    await bot.load_extension("cogs.search")
    await bot.load_extension("cogs.personal_tracker")
    await database.log_event("OK", "Bot initialized and DB ready")
    
    # تهيئة خادم Suwayomi في الخلفية
    try:
        from providers.suwayomi_client import SuwayomiClient
        s_client = SuwayomiClient()
        asyncio.create_task(s_client.bootstrap())
        print("🚀 [Startup] Suwayomi-Server bootstrap task started in background")
    except Exception as e:
        print(f"⚠️ [Startup] Failed to start Suwayomi bootstrap: {e}")

bot.setup_hook = setup_hook

_orig_close = bot.close
async def close():
    if hasattr(bot, "provider_mgr") and bot.provider_mgr:
        try:
            await bot.provider_mgr.close_http_session()
        except Exception:
            pass
    if hasattr(bot, "remote_down") and bot.remote_down:
        try:
            await bot.remote_down.close()
        except Exception:
            pass
    await _orig_close()

bot.close = close


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    await database.log_event("OK", f"Logged in as {bot.user.name}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands.")
        await database.log_event("OK", f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Failed to sync: {e}")
        await database.log_event("ERROR", f"Sync failed: {e}")
    
    # مزامنة المواقع المخصصة وبيانات تسجيل الدخول مع الـ Worker عند التشغيل
    await sync_custom_data_to_worker(bot, database)
    
    print("Bot ready.")

    import web_panel
    web_panel.set_bot(bot, database)


@bot.event
async def on_close():
    """إغلاق الموارد عند إيقاف البوت"""
    print("Closing resources...")
    await downloader.close_session()
    await remote_down.close()
    await provider_mgr.close_http_session()
    await database.log_event("OK", "Bot shutdown and resources closed")
    await database.close_db()


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # تسجيل تلقائي عند أول رسالة (بدون إيقاف المعالجة)
    if message.author.id not in registered_cache:
        await get_rank(message.author.id, auto_register=True)
        registered_cache.add(message.author.id)

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    await database.log_event("WARN", f"Command error: {error}")


if __name__ == "__main__":
    # 1. Initialize DB and close it to avoid event-loop connection locking
    try:
        asyncio.run(database.init_db())
        asyncio.run(database.close_db())
        print("✅ [Startup] Database initialized successfully.")
    except Exception as db_err:
        print(f"⚠️ [Startup] Database initialization failed: {db_err}")

    # 2. Start the Keep-Alive Web Panel immediately on the specified PORT
    # This ensures MangaSystem port check passes even if the Discord bot fails/delays
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting Web Panel on port {port}...")
    keep_alive(bot=None, db=database, port=port)

    # 3. Verify DISCORD_TOKEN
    if not Config.DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN مفقود")
        try:
            asyncio.run(database.log_event("ERROR", "DISCORD_TOKEN مفقود. يرجى إعداد توكن البوت في متغيرات البيئة (Secrets)."))
        except Exception as log_ex:
            print(f"Could not log missing token to DB: {log_ex}")
        
        print("Waiting for DISCORD_TOKEN to be configured... Web panel remains active.")
        import time
        while True:
            time.sleep(3600)

    # 4. Start the Discord Bot
    try:
        if Config.PROXY:
            print(f"🚀 Starting Bot with manual proxy injection: {Config.PROXY}")
            
            proxy_auth = None
            if "@" in Config.PROXY:
                try:
                    # استخراج اليوزر والباسورد من الرابط
                    auth_part = Config.PROXY.split("@")[0].split("//")[-1]
                    user, pwd = auth_part.split(":")
                    proxy_auth = aiohttp.BasicAuth(user, pwd)
                    print(f"🔑 Proxy auth configured for: {user}")
                except: pass

            async def manual_run():
                try:
                    async with bot:
                        print("🛠️ Injecting proxy into HTTPClient...")
                        original_request = bot.http.request
                        
                        async def proxied_request(*args, **kwargs):
                            kwargs['proxy'] = Config.PROXY
                            kwargs['proxy_auth'] = proxy_auth
                            return await original_request(*args, **kwargs)
                        
                        bot.http.request = proxied_request
                        print("📡 Attempting to login to Discord via Proxy...")
                        await bot.start(Config.DISCORD_TOKEN)
                        print("✅ Bot.start() completed.")
                except Exception as ex:
                    print(f"🔥 Error inside manual_run: {ex}")
                    raise ex

            try:
                print("🔄 Starting asyncio.run(manual_run)...")
                asyncio.run(manual_run())
            except Exception as e:
                print(f"❌ Manual Run Error: {e}")
                # محاولة أخيرة تقليدية
                bot.run(Config.DISCORD_TOKEN)
        else:
            print("Starting Bot...")
            bot.run(Config.DISCORD_TOKEN)
    except Exception as e:
        import traceback
        import time
        print(f"FATAL ERROR: {e}")
        try:
            asyncio.run(database.log_event("ERROR", f"FATAL ERROR: {e}"))
        except Exception as log_ex:
            print(f"Could not log fatal error to DB: {log_ex}")
            
        if "Cannot connect to host discord.com" in str(e):
            print("💡 TIP: Hugging Face might be blocking Discord. Try using a PROXY in your .env file.")
            try:
                asyncio.run(database.log_event("WARN", "TIP: Discord connection blocked. Try using a PROXY in your environment variables."))
            except: pass
            
        traceback.print_exc()
        print("Keeping process alive so the Web Panel remains active for troubleshooting...")
        while True:
            time.sleep(3600)
