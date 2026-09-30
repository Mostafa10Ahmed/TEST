"""
cogs/manga_cleaner.py — Manga & Manhwa Text Cleaning Cog
Discord Bot: MangaSystem

Slash command: /clean_manga
  Parameters:
    drive_url  — Google Drive folder link containing chapter images (required)
    mode       — Quality mode: HQ (default) | FAST
    dilate_iter — Mask dilation iterations (default 3)
    sfx_mode   — SFX removal mode (NORMAL | REMOVE_SFX_BETA)

Enhancements & Fixes:
  1. Per-user active lock & 60s cooldown between commands to prevent race conditions.
  2. Single Master Google Drive folder for batch/multi-chapter output ([Cleaned Batch] {Name}).
  3. Single final Discord completion card with 1 master Google Drive link + @User mention.
  4. Inter-chapter delay (3s for VIP, 8s for Normal Users) between chapters in a batch.
  5. Interactive "Cancel" button to stop processing at any time.
  6. Discord API rate-limit throttling to prevent HTTP 429 dropped response links.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import database
from bot_config import Config
from user_system import user_only, check_and_consume_usage
import user_system

log = logging.getLogger("manga_cleaner")

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
C_BLUE   = discord.Color.from_rgb(88,  101, 242)
C_GREEN  = discord.Color.from_rgb(35,  165,  89)
C_RED    = discord.Color.from_rgb(237,  66,  69)
C_ORANGE = discord.Color.from_rgb(243, 156,  18)

TEMP_ROOT      = Path("temp_cleaner")
MAX_IMAGES     = 200          # safety cap
CHUNK_TIMEOUT  = 60 * 15     # 15 min for large chapters
DRIVE_RE       = re.compile(
    r"https://drive\.google\.com/(?:drive/folders/|open\?id=)([\w-]+)"
)
MAX_ZIP_BATCH        = 10          # أقصى عدد ملفات ZIP في أمر التبييض المتعدد الواحد
DRIVE_PARALLEL_OPS   = 6           # عدد عمليات التحميل/الرفع المتوازية مع Drive
ZIP_CLEANUP_HOURS    = 2.0         # مهلة حذف مجلد "تبييض ZIP" تلقائياً من Drive

# Global semaphore — only 1 cleaning job active on server at a time to prevent resource starvation
CLEANING_SEMAPHORE = asyncio.Semaphore(1)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _extract_folder_id(url: str) -> Optional[str]:
    """Extract Google Drive folder ID from a share URL."""
    m = DRIVE_RE.search(url)
    return m.group(1) if m else None


def _extract_any_drive_id(url: str) -> Optional[str]:
    """Extract any Google Drive ID (folder OR file/ZIP) from a share URL."""
    try:
        from drive_stitch import extract_drive_id
        return extract_drive_id(url)
    except Exception:
        return _extract_folder_id(url)


async def _parallel_drive_ops(items: list, op, concurrency: int = DRIVE_PARALLEL_OPS, on_progress=None) -> None:
    """
    ينفّذ op(service, item) على كل عناصر items بشكل متوازٍ باستخدام مجموعة صغيرة
    من خدمات Drive المستقلة (كل عملية تُنفَّذ بخدمة مخصّصة لها وحدها في لحظتها،
    عبر طابور موارد) — لأن كائن خدمة Drive (httplib2) غير آمن للمشاركة بين عدة
    Threads في نفس اللحظة. هذا يستبدل التحميل/الرفع المتسلسل (ملف تلو الآخر)
    الذي كان السبب الجذري لبطء /clean_manga مع الفصول الكبيرة.
    """
    if not items:
        return
    from drive_stitch import build_drive_service

    loop = asyncio.get_event_loop()
    n = max(1, min(concurrency, len(items)))
    services = await asyncio.gather(*[loop.run_in_executor(None, build_drive_service) for _ in range(n)])
    pool: asyncio.Queue = asyncio.Queue()
    for s in services:
        pool.put_nowait(s)

    done = 0
    done_lock = asyncio.Lock()
    total = len(items)

    async def _worker(item):
        nonlocal done
        service = await pool.get()
        try:
            await loop.run_in_executor(None, lambda: op(service, item))
        finally:
            pool.put_nowait(service)
        async with done_lock:
            done += 1
            if on_progress:
                await on_progress(done, total)

    await asyncio.gather(*[_worker(it) for it in items])


def _make_progress_bar(current: int, total: int) -> str:
    bar_len = 15
    if total <= 0:
        return f"`{'▱' * bar_len} 0%` · `0/0`"
    ratio = min(1.0, max(0.0, current / total))
    filled = round(ratio * bar_len)
    bar = "▰" * filled + "▱" * (bar_len - filled)
    pct = int(ratio * 100)
    return f"`{bar} {pct}%`  ·  `{current}/{total}`"


def _progress_layout(title: str, description: str, progress_bar: str, colour: discord.Color = C_BLUE) -> discord.ui.LayoutView:
    """Build the progress layout using Components v2 Container."""
    layout = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=colour)
    container.add_item(discord.ui.TextDisplay(f"## ⏳ {title}"))
    container.add_item(discord.ui.TextDisplay(progress_bar))
    container.add_item(discord.ui.TextDisplay(f"**{description}**"))
    layout.add_item(container)
    return layout


def _done_layout_batch(
    *,
    user_mention: str = "",
    batch_title: str,
    drive_link: str,
    chapters_count: int,
    total_pages: int,
    elapsed: float,
    total_errors: int,
    expires_hours: Optional[float] = None,
) -> discord.ui.LayoutView:
    """Build the completion layout for a single or multi-chapter cleaning job."""
    layout = discord.ui.LayoutView(timeout=None)
    
    container = discord.ui.Container(accent_color=C_GREEN)
    if user_mention:
        container.add_item(discord.ui.TextDisplay(f"🎉 {user_mention} تم الانتهاء من التبييض بنجاح!"))
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

    container.add_item(discord.ui.TextDisplay(f"## 🧹 اكتملت عملية التبييض: `{batch_title}`"))
    
    bar = "▰" * 15
    if chapters_count > 1:
        progress_line = f"`{bar} 100%`  ·  تمت معالجة `{chapters_count}` فصول (`{total_pages}` صفحة)"
    else:
        progress_line = f"`{bar} 100%`  ·  تمت معالجة `{total_pages}` صفحة"
    container.add_item(discord.ui.TextDisplay(progress_line))
    
    details = f"⏱️ **الزمن المستغرق**: `{elapsed:.1f} ثانية`\n"
    if total_errors:
        details += f"⚠️ **تنبيه**: تم تسجيل `{total_errors}` أخطاء أثناء المعالجة وتخطي الصفحات المتعذرة."
    else:
        details += "الحالة: تم تنظيف كافة الصفحات"
    container.add_item(discord.ui.TextDisplay(details))
    
    if drive_link:
        if chapters_count > 1:
            btn_label = "فتح مجلد الفصول في Google Drive"
            section_desc = "اضغط على الزر لفتح مجلد Google Drive الرئيسي للفصول:"
        else:
            btn_label = "فتح الفصل في Google Drive"
            section_desc = "اضغط على الزر لفتح مجلد Google Drive الخاص بالفصل:"

        btn = discord.ui.Button(
            label=btn_label,
            style=discord.ButtonStyle.link,
            url=drive_link,
            emoji="📁"
        )
        section = discord.ui.Section(
            discord.ui.TextDisplay(section_desc),
            accessory=btn
        )
        container.add_item(section)

    if expires_hours:
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(
            f"⚠️ **تنبيه هام**: هذا المجلد مؤقت وسيتم حذفه تلقائياً من Drive بعد "
            f"**{expires_hours:.0f} ساعة** من الآن — يرجى تحميل/حفظ الملفات الآن."
        ))

    layout.add_item(container)
    return layout


def _error_layout(message: str) -> discord.ui.LayoutView:
    """Build the error layout using Components v2 Container."""
    layout = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=C_RED)
    container.add_item(discord.ui.TextDisplay("## ❌ خطأ في عملية التبييض"))
    container.add_item(discord.ui.TextDisplay(message))
    layout.add_item(container)
    return layout


# ──────────────────────────────────────────────
# Google Drive helpers
# ──────────────────────────────────────────────

async def _download_drive_folder(
    folder_id: str,
    dest: Path,
    bot: commands.Bot,
    on_progress = None,
) -> list[Path]:
    """
    Download all images from a Google Drive folder to `dest`.
    Returns sorted list of downloaded file paths.
    """
    dest.mkdir(parents=True, exist_ok=True)

    try:
        from drive_stitch import build_drive_service, list_folder_images, download_file
    except ImportError:
        from auth_drive import build_drive_service
        raise RuntimeError("drive_stitch not available — check imports")

    loop = asyncio.get_event_loop()

    service = await loop.run_in_executor(None, build_drive_service)
    files = await loop.run_in_executor(None, lambda: list_folder_images(service, folder_id))
    if not files:
        return []

    target_files = files[:MAX_IMAGES]
    total = len(target_files)

    def _download_one(svc, f):
        download_file(svc, f["id"], str(dest / f["name"]))

    await _parallel_drive_ops(target_files, _download_one, on_progress=on_progress)

    paths = [dest / f["name"] for f in target_files]
    return sorted(paths)


async def _get_or_create_user_root_folder(service, user_id: int) -> str:
    """
    يبحث عن (أو ينشئ) مجلد التخزين الدائم الخاص بمستخدم معيّن '<user_id>' داخل
    مجلد التخزين الرئيسي للبوت (Config.GOOGLE_DRIVE_FOLDER_ID)، ليُستخدم كوجهة
    ثابتة لمخرجات أمري /clean_zip و /clean_zip_multi (بما أن هذين الأمرين لا
    يأخذان من المستخدم مجلد وجهة، بعكس /clean_manga).
    """
    root = Config.GOOGLE_DRIVE_FOLDER_ID
    if not root:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID غير مضبوط في إعدادات البوت (.env)")

    loop = asyncio.get_event_loop()
    name = str(user_id)

    def _find():
        q = (
            f"'{root}' in parents and name='{name}' "
            "and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        res = service.files().list(
            q=q, fields="files(id, name)", supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute()
        found = res.get("files", [])
        return found[0]["id"] if found else None

    existing = await loop.run_in_executor(None, _find)
    if existing:
        return existing

    from drive_stitch import create_drive_folder
    return await loop.run_in_executor(None, lambda: create_drive_folder(service, name, root))


async def _schedule_drive_cleanup(user_id: int, folder_id: str, folder_name: str, hours: float = ZIP_CLEANUP_HOURS) -> None:
    """يسجّل مجلد الإخراج في قاعدة البيانات ليُحذف تلقائياً من Drive بعد `hours` ساعة."""
    delete_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)).isoformat()
    try:
        await database.add_drive_cleanup_job(user_id, folder_id, folder_name, delete_at)
    except Exception as exc:
        log.warning("Failed to schedule drive cleanup for folder %s: %s", folder_id, exc)


async def _create_master_clean_folder(parent_folder_id: str, batch_title: str) -> tuple[str, str]:
    """
    Creates a single master folder '[Cleaned Batch] {batch_title}' in Google Drive.
    Falls back to bot storage folder if target folder lacks write permissions.
    Returns (master_folder_id, master_web_link).
    """
    from drive_stitch import build_drive_service, create_drive_folder
    loop = asyncio.get_event_loop()
    service = await loop.run_in_executor(None, build_drive_service)
    clean_batch_name = f"[Cleaned Batch] {batch_title}"
    try:
        master_id = await loop.run_in_executor(None, lambda: create_drive_folder(service, clean_batch_name, parent_folder_id))
    except Exception as exc:
        log.warning("Could not create folder in parent %s (%s), falling back to bot storage folder...", parent_folder_id, exc)
        fallback_parent = Config.GOOGLE_DRIVE_FOLDER_ID or parent_folder_id
        master_id = await loop.run_in_executor(None, lambda: create_drive_folder(service, clean_batch_name, fallback_parent))
    return master_id, f"https://drive.google.com/drive/folders/{master_id}"


async def _upload_to_drive(
    images: list[Path],
    parent_folder_id: str,
    subfolder_name: str,
    on_progress = None,
) -> tuple[str, str]:
    """
    Creates `[Cleaned] {subfolder_name}` subfolder inside `parent_folder_id` in Drive and uploads images.
    Falls back to bot storage folder if target folder lacks write permissions.
    Returns (web_view_url, folder_id) of the new subfolder.
    """
    from drive_stitch import build_drive_service, create_drive_folder, upload_file_to_drive

    loop = asyncio.get_event_loop()

    service = await loop.run_in_executor(None, build_drive_service)
    clean_name = f"[Cleaned] {subfolder_name}"
    try:
        folder_id = await loop.run_in_executor(None, lambda: create_drive_folder(service, clean_name, parent_folder_id))
    except Exception as exc:
        log.warning("Could not create subfolder in parent %s (%s), falling back to bot storage folder...", parent_folder_id, exc)
        fallback_parent = Config.GOOGLE_DRIVE_FOLDER_ID or parent_folder_id
        folder_id = await loop.run_in_executor(None, lambda: create_drive_folder(service, clean_name, fallback_parent))

    def _upload_one(svc, img_path):
        upload_file_to_drive(svc, str(img_path), folder_id, img_path.name)

    await _parallel_drive_ops(images, _upload_one, on_progress=on_progress)

    return f"https://drive.google.com/drive/folders/{folder_id}", folder_id


async def _send_temporary_ping(interaction: discord.Interaction, message_text: str = "تم الانتهاء!"):
    try:
        if interaction.channel:
            ping_msg = await interaction.channel.send(f"{interaction.user.mention} {message_text}")
            await asyncio.sleep(8)
            try:
                await ping_msg.delete()
            except Exception:
                pass
    except Exception as e:
        log.warning("Failed to send temporary ping: %s", e)


async def _send_to_inpainting_space(
    zip_bytes: bytes,
    mode: str,
    session: aiohttp.ClientSession,
    dilate_iter: int = 3,
    remove_sfx: bool = False,
    lang: str = "ALL",
) -> tuple[bytes, int, int, float]:
    """
    Call the Inpainting Space /process_gradio_zip endpoint via gradio_client.
    Returns (clean_zip_bytes, pages_processed, errors, elapsed_s).
    """
    import tempfile
    
    in_temp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    in_temp_path = in_temp.name
    try:
        in_temp.write(zip_bytes)
        in_temp.close()

        def _do_call():
            from gradio_client import Client, handle_file
            headers = {"Authorization": f"Bearer {Config.HF_TOKEN}"} if Config.HF_TOKEN else None
            client = Client(
                Config.INPAINTING_SPACE_URL.rstrip("/"),
                token=Config.HF_TOKEN if Config.HF_TOKEN else None,
                headers=headers,
                httpx_kwargs={"timeout": 600.0}
            )
            result = client.predict(
                file_obj=handle_file(in_temp_path),
                key=Config.INPAINTING_SPACE_KEY,
                dilate_iter=dilate_iter,
                remove_sfx=remove_sfx,
                lang=lang,
                api_name="/process_gradio_zip"
            )
            return result

        result = await asyncio.to_thread(_do_call)
        cleaned_zip_path, log_message = result

        if cleaned_zip_path is None or not os.path.exists(str(cleaned_zip_path)):
            raise RuntimeError(f"Space returned error: {log_message}")

        pages = 0
        errors = 0
        elapsed = 0.0

        m_pages = re.search(r"cleaned\s+(\d+)\s+pages", log_message, re.IGNORECASE)
        if m_pages:
            pages = int(m_pages.group(1))

        m_time = re.search(r"in\s+(\d+(?:\.\d+)?)\s+seconds", log_message, re.IGNORECASE)
        if m_time:
            elapsed = float(m_time.group(1))

        m_errors = re.search(r"Errors:\s+(\d+)", log_message, re.IGNORECASE)
        if m_errors:
            errors = int(m_errors.group(1))

        with open(cleaned_zip_path, "rb") as f:
            data = f.read()

        try:
            os.remove(cleaned_zip_path)
        except Exception:
            pass

        return data, pages, errors, elapsed

    finally:
        try:
            os.remove(in_temp_path)
        except Exception:
            pass


# ──────────────────────────────────────────────
# Dashboard & UI Components
# ──────────────────────────────────────────────

class CancelButton(discord.ui.Button):
    def __init__(self, dashboard: DashboardUI):
        super().__init__(label="⛔ Cancel", style=discord.ButtonStyle.danger)
        self.dashboard = dashboard

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.dashboard.interaction.user.id:
            await interaction.response.send_message("❌ يمكنك فقط إلغاء عملياتك الخاصة.", ephemeral=True)
            return
        self.dashboard.is_cancelled = True
        await interaction.response.send_message("🛑 تم استقبال طلب الإلغاء، سيتم التوقف والتراجع بين الفصول فوراً.", ephemeral=True)


class RetryButton(discord.ui.Button):
    def __init__(self, dashboard: DashboardUI):
        super().__init__(label="🔄 إعادة المحاولة", style=discord.ButtonStyle.primary)
        self.dashboard = dashboard

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("🔄 يرجى تشغيل أمر `/clean_manga` مجدداً للبدء فوراً.", ephemeral=True)


class DashboardUI:
    def __init__(self, interaction: discord.Interaction, chapters: list[dict]):
        self.interaction = interaction
        self.chapters = chapters
        self.statuses = {c['id']: "⚪ في الانتظار" for c in chapters}
        self.progress_bars = {c['id']: "" for c in chapters}
        self.drive_links = {c['id']: None for c in chapters}
        self.is_cancelled = False
        self.start_time = time.time()
        self.last_update_time = 0.0
        
    def generate_layout(self) -> discord.ui.LayoutView:
        layout = discord.ui.LayoutView(timeout=None)
        
        any_failed = any("❌" in s or "خطأ" in s for s in self.statuses.values())
        all_done = all("✅" in s or "اكتمل" in s for s in self.statuses.values())
        
        if self.is_cancelled:
            color = C_ORANGE
            status_badge = "🛑 تم إلغاء العملية بناءً على طلبك"
        elif any_failed:
            color = C_RED
            status_badge = "❌ تعذر التبييض (تم التراجع لحماية الصور)"
        elif all_done:
            color = C_GREEN
            status_badge = "✨ تم التبييض والحفظ بنجاح"
        else:
            color = C_BLUE
            status_badge = "⚡ جاري التبييض والضبط..."
            
        container = discord.ui.Container(accent_color=color)
        container.add_item(discord.ui.TextDisplay("## 🧹 تبييض الفصول"))
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        elapsed = time.time() - self.start_time
        time_label = "⏱️ **الوقت المستغرق**" if (all_done or self.is_cancelled) else "⏱️ **الوقت المنقضي**"
        container.add_item(discord.ui.TextDisplay(f"**الحالة العامة**: {status_badge}\n{time_label}: `{elapsed:.0f} ثانية`"))
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        for i, c in enumerate(self.chapters):
            if i > 0:
                container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
            
            bar_desc = f"\n  {self.progress_bars[c['id']]}" if self.progress_bars[c['id']] else ""
            status_text = (
                f"📂 **الفصل**: `{c['name']}`\n"
                f"⚡ **حالة الفصل**: {self.statuses[c['id']]}{bar_desc}"
            )
            container.add_item(discord.ui.TextDisplay(status_text))

        # Bottom section INSIDE Container
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        if not (all_done or any_failed or self.is_cancelled):
            cancel_btn = CancelButton(self)
            container.add_item(discord.ui.ActionRow(cancel_btn))
        elif any_failed or self.is_cancelled:
            retry_btn = RetryButton(self)
            container.add_item(discord.ui.ActionRow(retry_btn))
            
        layout.add_item(container)
        return layout
        
    async def update(self, chap_id: str, status: str, bar: str = "", drive_link: str = None, force: bool = False):
        if self.is_cancelled and not force: return
        self.statuses[chap_id] = status
        self.progress_bars[chap_id] = bar
        if drive_link:
            self.drive_links[chap_id] = drive_link
            
        now = time.time()
        if not force and (now - self.last_update_time < 1.5):
            return
        self.last_update_time = now

        try:
            await self.interaction.edit_original_response(content=None, embed=None, view=self.generate_layout())
        except Exception:
            pass


class ChapterSelectView(discord.ui.View):
    def __init__(
        self,
        interaction: discord.Interaction,
        subfolders: list[dict],
        cog: MangaCleanerCog,
        mode: str,
        dilate_iter: int,
        remove_sfx: bool,
        parent_folder_id: str,
        batch_title: str,
        lang: str = "ALL"
    ):
        super().__init__(timeout=60)
        self.interaction = interaction
        self.subfolders = subfolders
        self.cog = cog
        self.mode = mode
        self.dilate_iter = dilate_iter
        self.remove_sfx = remove_sfx
        self.parent_folder_id = parent_folder_id
        self.batch_title = batch_title
        self.lang = lang
        
        options = []
        for i, sf in enumerate(subfolders[:25]): # Discord select cap
            options.append(discord.SelectOption(label=sf['name'], value=sf['id']))
            
        self.select = discord.ui.Select(
            placeholder="اختر الفصول المراد تبييضها (يمكنك اختيار عدة فصول)",
            min_values=1,
            max_values=len(options),
            options=options
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_ids = self.select.values
        selected_folders = [sf for sf in self.subfolders if sf['id'] in selected_ids]
        
        dashboard = DashboardUI(self.interaction, selected_folders)
        await dashboard.update(selected_folders[0]['id'], "⏳ جاري التجهيز...", force=True)
        
        async def _run_and_release():
            try:
                await self.cog.process_queue(
                    self.interaction,
                    dashboard,
                    selected_folders,
                    self.mode,
                    self.dilate_iter,
                    self.remove_sfx,
                    self.parent_folder_id,
                    self.batch_title,
                    lang=self.lang
                )
            finally:
                user_system.release_user_lock(self.interaction.user.id)

        asyncio.create_task(_run_and_release())
        self.stop()

    async def on_timeout(self):
        user_system.release_user_lock(self.interaction.user.id)


async def _get_subfolders(folder_id: str) -> list[dict]:
    from drive_stitch import build_drive_service
    loop = asyncio.get_event_loop()
    def _fetch():
        service = build_drive_service()
        q = f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        res = service.files().list(q=q, fields="files(id, name)").execute()
        return res.get('files', [])
    try:
        return await loop.run_in_executor(None, _fetch)
    except Exception:
        return []


# ──────────────────────────────────────────────
# Cog Implementation
# ──────────────────────────────────────────────

class MangaCleanerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        TEMP_ROOT.mkdir(exist_ok=True)
        self.drive_cleanup_loop.start()

    def cog_unload(self):
        self.drive_cleanup_loop.cancel()

    @tasks.loop(minutes=5)
    async def drive_cleanup_loop(self):
        """يحذف دورياً مجلدات Drive المؤقتة (نتائج /clean_zip و /clean_zip_multi)
        التي انتهت مهلتها (ساعتان افتراضياً) لتحرير مساحة تخزين البوت على Drive."""
        try:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            rows = await database.get_due_drive_cleanup_jobs(now, limit=20)
        except Exception as exc:
            log.warning("drive_cleanup_loop: failed to fetch due jobs: %s", exc)
            return
        if not rows:
            return

        from drive_stitch import build_drive_service, delete_drive_file
        loop = asyncio.get_event_loop()
        service = None
        for job_id, user_id, folder_id, folder_name, delete_at in rows:
            try:
                if service is None:
                    service = await loop.run_in_executor(None, build_drive_service)
                await loop.run_in_executor(None, lambda fid=folder_id: delete_drive_file(service, fid))
                log.info("Auto-deleted expired clean folder '%s' (%s) for user %s", folder_name, folder_id, user_id)
            except Exception as exc:
                log.warning("Failed to auto-delete Drive folder %s: %s", folder_id, exc)
            finally:
                await database.mark_drive_cleanup_done(job_id)

    @drive_cleanup_loop.before_loop
    async def _before_drive_cleanup_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="clean_manga", description="تبييض وتنظيف نصوص المانجا من مجلد Google Drive")
    @app_commands.describe(
        drive_url="رابط مجلد Google Drive المحتوي على صور الفصل",
        mode="وضع الجودة (HQ تبييض دقيق | FAST تبييض سريع)",
        dilate_iter="درجة توسيع الماسك (الافتراضي 3)",
        sfx_mode="وضع المؤثرات الصوتية (NORMAL عادية | REMOVE_SFX_BETA إزالة المؤثرات للمشرفين)",
        lang="اللغة المطلوب تبييضها فقط (ALL الكل | EN إنجليزي فقط | KO كوري فقط)"
    )
    @user_only()
    async def clean_manga_cmd(
        self,
        interaction: discord.Interaction,
        drive_url: str,
        mode: str = "HQ",
        dilate_iter: int = 3,
        sfx_mode: str = "NORMAL",
        lang: str = "ALL",
    ):
        user_id = interaction.user.id
        rank = await user_system.get_rank(user_id)

        # ── Check Cooldown & Active Task Lock ──
        allowed_lock, lock_msg = user_system.check_user_cooldown_and_lock(user_id, rank)
        if not allowed_lock:
            await interaction.response.send_message(
                view=_error_layout(lock_msg),
                ephemeral=True
            )
            return

        # ── Check SFX Permission ──────────────
        remove_sfx = False
        if sfx_mode == "REMOVE_SFX_BETA":
            if rank < 3:
                await interaction.response.send_message(
                    view=_error_layout("❌ وضع إزالة المؤثرات الصوتية (SFX [BETA]) متاح فقط لمالك البوت والمشرفين (Admin) حالياً."),
                    ephemeral=True
                )
                return
            remove_sfx = True

        # ── Check Limits & Credits ────────────
        allowed, usage_msg = await user_system.check_and_consume_usage(user_id, "clean")
        if not allowed:
            await interaction.response.send_message(
                view=_error_layout(usage_msg),
                ephemeral=True
            )
            return

        # ── Validate URL ──────────────────────
        folder_id = _extract_folder_id(drive_url)
        if not folder_id:
            await interaction.response.send_message(
                view=_error_layout(
                    "الرابط غير صحيح!\n"
                    "يجب أن يكون رابط **مجلد Google Drive** بصيغة:\n"
                    "`https://drive.google.com/drive/folders/XXXX`"
                ),
                ephemeral=True,
            )
            return

        # ── Validate Space config ─────────────
        if not Config.INPAINTING_SPACE_URL or not Config.INPAINTING_SPACE_KEY:
            await interaction.response.send_message(
                view=_error_layout(
                    "⚙️ لم يُعدّ الـ Inpainting Space بعد.\n"
                    "يرجى إعداد `INPAINTING_SPACE_URL` و `INPAINTING_SPACE_KEY` في الـ .env"
                ),
                ephemeral=True,
            )
            return

        # Acquire active task lock
        user_system.acquire_user_lock(user_id)

        # ── Defer (long operation) ────────────
        await interaction.response.defer(thinking=True)
        if usage_msg:
            try:
                await interaction.followup.send(content=usage_msg, ephemeral=True)
            except Exception:
                pass

        subfolders = await _get_subfolders(folder_id)
        
        # Fetch parent folder name for batch title
        batch_title = "Manga Chapters"
        try:
            from drive_stitch import build_drive_service
            loop = asyncio.get_event_loop()
            service = await loop.run_in_executor(None, build_drive_service)
            meta = await loop.run_in_executor(None, lambda: service.files().get(fileId=folder_id, fields="name").execute())
            batch_title = meta.get('name', 'Manga Chapters')
        except Exception as e:
            log.warning("Drive folder metadata fetch failed for %s: %s", folder_id, e)

        if subfolders:
            view = ChapterSelectView(
                interaction,
                subfolders,
                self,
                mode,
                dilate_iter,
                remove_sfx,
                parent_folder_id=folder_id,
                batch_title=batch_title,
                lang=lang
            )
            await interaction.edit_original_response(
                content="📂 **عثرت على مجلدات فرعية في هذا الرابط!** يرجى تحديد الفصول التي تريد تبييضها وتنظيفها:",
                view=view
            )
            # Note: Lock is held until selection is made or view times out after 60s
        else:
            dashboard = DashboardUI(interaction, [{'id': folder_id, 'name': batch_title}])
            await dashboard.update(folder_id, "⏳ جاري التجهيز...", force=True)
            
            async def _run_single_batch():
                try:
                    await self.process_queue(
                        interaction,
                        dashboard,
                        [{'id': folder_id, 'name': batch_title}],
                        mode,
                        dilate_iter,
                        remove_sfx,
                        parent_folder_id=folder_id,
                        batch_title=batch_title,
                        lang=lang
                    )
                finally:
                    user_system.release_user_lock(user_id)

            asyncio.create_task(_run_single_batch())

    @app_commands.command(name="clean_zip", description="تبييض ملف ZIP واحد مرفوع على Google Drive (بدل مجلد صور)")
    @app_commands.describe(
        drive_url="رابط ملف ZIP على Google Drive (وليس مجلد) — مثال: https://drive.google.com/file/d/XXXX/view",
        mode="وضع الجودة (HQ تبييض دقيق | FAST تبييض سريع)",
        dilate_iter="درجة توسيع الماسك (الافتراضي 3)",
        sfx_mode="وضع المؤثرات الصوتية (NORMAL عادية | REMOVE_SFX_BETA إزالة المؤثرات للمشرفين)",
        lang="اللغة المطلوب تبييضها فقط (ALL الكل | EN إنجليزي فقط | KO كوري فقط)"
    )
    @user_only()
    async def clean_zip_cmd(
        self,
        interaction: discord.Interaction,
        drive_url: str,
        mode: str = "HQ",
        dilate_iter: int = 3,
        sfx_mode: str = "NORMAL",
        lang: str = "ALL",
    ):
        user_id = interaction.user.id
        rank = await user_system.get_rank(user_id)

        allowed_lock, lock_msg = user_system.check_user_cooldown_and_lock(user_id, rank)
        if not allowed_lock:
            await interaction.response.send_message(view=_error_layout(lock_msg), ephemeral=True)
            return

        remove_sfx = False
        if sfx_mode == "REMOVE_SFX_BETA":
            if rank < 3:
                await interaction.response.send_message(
                    view=_error_layout("❌ وضع إزالة المؤثرات الصوتية (SFX [BETA]) متاح فقط لمالك البوت والمشرفين (Admin) حالياً."),
                    ephemeral=True
                )
                return
            remove_sfx = True

        allowed, usage_msg = await user_system.check_and_consume_usage(user_id, "clean")
        if not allowed:
            await interaction.response.send_message(view=_error_layout(usage_msg), ephemeral=True)
            return

        file_id = _extract_any_drive_id(drive_url)
        if not file_id:
            await interaction.response.send_message(
                view=_error_layout(
                    "الرابط غير صحيح!\n"
                    "يجب أن يكون رابط **ملف ZIP** على Google Drive بصيغة:\n"
                    "`https://drive.google.com/file/d/XXXX/view`\n"
                    "(لتبييض مجلد صور كامل استخدم `/clean_manga` بدلاً من ذلك)"
                ),
                ephemeral=True,
            )
            return

        if not Config.INPAINTING_SPACE_URL or not Config.INPAINTING_SPACE_KEY:
            await interaction.response.send_message(
                view=_error_layout(
                    "⚙️ لم يُعدّ الـ Inpainting Space بعد.\n"
                    "يرجى إعداد `INPAINTING_SPACE_URL` و `INPAINTING_SPACE_KEY` في الـ .env"
                ),
                ephemeral=True,
            )
            return
        if not Config.GOOGLE_DRIVE_FOLDER_ID:
            await interaction.response.send_message(
                view=_error_layout("⚙️ لم يُعدّ مجلد تخزين البوت الرئيسي بعد (`GOOGLE_DRIVE_FOLDER_ID` في الـ .env)."),
                ephemeral=True,
            )
            return

        user_system.acquire_user_lock(user_id)
        await interaction.response.defer(thinking=True)
        if usage_msg:
            try:
                await interaction.followup.send(content=usage_msg, ephemeral=True)
            except Exception:
                pass

        from drive_stitch import build_drive_service, get_file_metadata
        loop = asyncio.get_event_loop()
        try:
            service = await loop.run_in_executor(None, build_drive_service)
            meta = await loop.run_in_executor(None, lambda: get_file_metadata(service, file_id))
        except Exception as exc:
            await interaction.edit_original_response(content=None, view=_error_layout(f"تعذّر الوصول إلى الملف على Drive: {exc}"))
            user_system.release_user_lock(user_id)
            return

        meta_name = meta.get("name", "") or ""
        mime = meta.get("mimeType", "") or ""
        is_zip = (
            mime in {"application/zip", "application/x-zip-compressed", "application/octet-stream"}
            or meta_name.lower().endswith(".zip")
        )
        if not is_zip:
            await interaction.edit_original_response(
                content=None,
                view=_error_layout(
                    "الرابط المُرسَل ليس ملف **ZIP**.\n"
                    "لتبييض مجلد صور استخدم أمر `/clean_manga` بدلاً من ذلك."
                ),
            )
            user_system.release_user_lock(user_id)
            return

        zip_title = re.sub(r"\.zip$", "", meta_name, flags=re.IGNORECASE).strip() or "Chapter"

        try:
            user_root = await _get_or_create_user_root_folder(service, user_id)
        except Exception as exc:
            await interaction.edit_original_response(content=None, view=_error_layout(str(exc)))
            user_system.release_user_lock(user_id)
            return

        dashboard = DashboardUI(interaction, [{'id': file_id, 'name': zip_title}])
        await interaction.edit_original_response(content=None, view=dashboard.generate_layout())

        async def _run_single_zip():
            try:
                t_start = time.perf_counter()
                pages, errors, drive_link, folder_id = await self.process_single_zip(
                    interaction, dashboard, file_id, file_id, zip_title, mode, dilate_iter, remove_sfx, user_root, lang=lang
                )
                elapsed = time.perf_counter() - t_start

                if dashboard.is_cancelled:
                    return

                if folder_id:
                    await _schedule_drive_cleanup(user_id, folder_id, zip_title)

                layout = _done_layout_batch(
                    user_mention=interaction.user.mention,
                    batch_title=zip_title,
                    drive_link=drive_link or "",
                    chapters_count=1,
                    total_pages=pages,
                    elapsed=elapsed,
                    total_errors=errors,
                    expires_hours=ZIP_CLEANUP_HOURS if folder_id else None,
                )
                try:
                    await interaction.channel.send(view=layout)
                except Exception as exc:
                    log.warning("Failed to send zip completion card: %s", exc)

                await database.log_event("OK", f"[MangaCleaner-ZIP] cleaned '{zip_title}' ({pages}p) in {elapsed:.0f}s")
            finally:
                user_system.release_user_lock(user_id)

        asyncio.create_task(_run_single_zip())

    @app_commands.command(name="clean_zip_multi", description="تبييض متعدد: عدة ملفات ZIP دفعة واحدة من روابط Google Drive")
    @app_commands.describe(
        drive_urls="روابط ملفات ZIP على Google Drive، افصل بينها بسطر جديد أو مسافة (حتى 10 ملفات)",
        mode="وضع الجودة (HQ تبييض دقيق | FAST تبييض سريع)",
        dilate_iter="درجة توسيع الماسك (الافتراضي 3)",
        sfx_mode="وضع المؤثرات الصوتية (NORMAL عادية | REMOVE_SFX_BETA إزالة المؤثرات للمشرفين)",
        lang="اللغة المطلوب تبييضها فقط (ALL الكل | EN إنجليزي فقط | KO كوري فقط)"
    )
    @user_only()
    async def clean_zip_multi_cmd(
        self,
        interaction: discord.Interaction,
        drive_urls: str,
        mode: str = "HQ",
        dilate_iter: int = 3,
        sfx_mode: str = "NORMAL",
        lang: str = "ALL",
    ):
        user_id = interaction.user.id
        rank = await user_system.get_rank(user_id)

        allowed_lock, lock_msg = user_system.check_user_cooldown_and_lock(user_id, rank)
        if not allowed_lock:
            await interaction.response.send_message(view=_error_layout(lock_msg), ephemeral=True)
            return

        remove_sfx = False
        if sfx_mode == "REMOVE_SFX_BETA":
            if rank < 3:
                await interaction.response.send_message(
                    view=_error_layout("❌ وضع إزالة المؤثرات الصوتية (SFX [BETA]) متاح فقط لمالك البوت والمشرفين (Admin) حالياً."),
                    ephemeral=True
                )
                return
            remove_sfx = True

        raw_links = [l for l in re.split(r"[\s,]+", drive_urls.strip()) if l]
        seen_ids: dict[str, str] = {}
        for link in raw_links:
            fid = _extract_any_drive_id(link)
            if fid and fid not in seen_ids:
                seen_ids[fid] = link
        file_ids = list(seen_ids.keys())[:MAX_ZIP_BATCH]

        if not file_ids:
            await interaction.response.send_message(
                view=_error_layout(
                    "لم يتم العثور على أي رابط ملف ZIP صالح على Google Drive.\n"
                    "أرسل الروابط مفصولة بسطر جديد أو مسافة، مثال:\n"
                    "`https://drive.google.com/file/d/AAA/view`\n"
                    "`https://drive.google.com/file/d/BBB/view`"
                ),
                ephemeral=True,
            )
            return

        allowed, usage_msg = await user_system.check_and_consume_usage(user_id, "clean")
        if not allowed:
            await interaction.response.send_message(view=_error_layout(usage_msg), ephemeral=True)
            return

        if not Config.INPAINTING_SPACE_URL or not Config.INPAINTING_SPACE_KEY:
            await interaction.response.send_message(
                view=_error_layout("⚙️ لم يُعدّ الـ Inpainting Space بعد."), ephemeral=True
            )
            return
        if not Config.GOOGLE_DRIVE_FOLDER_ID:
            await interaction.response.send_message(
                view=_error_layout("⚙️ لم يُعدّ مجلد تخزين البوت الرئيسي بعد (`GOOGLE_DRIVE_FOLDER_ID` في الـ .env)."),
                ephemeral=True,
            )
            return

        user_system.acquire_user_lock(user_id)
        await interaction.response.defer(thinking=True)
        if usage_msg:
            try:
                await interaction.followup.send(content=usage_msg, ephemeral=True)
            except Exception:
                pass

        from drive_stitch import build_drive_service, get_file_metadata
        loop = asyncio.get_event_loop()
        try:
            service = await loop.run_in_executor(None, build_drive_service)
        except Exception as exc:
            await interaction.edit_original_response(content=None, view=_error_layout(f"تعذّر الاتصال بـ Google Drive: {exc}"))
            user_system.release_user_lock(user_id)
            return

        items: list[dict] = []
        skipped = 0
        for fid in file_ids:
            try:
                meta = await loop.run_in_executor(None, lambda fid=fid: get_file_metadata(service, fid))
            except Exception:
                skipped += 1
                continue
            meta_name = meta.get("name", "") or ""
            mime = meta.get("mimeType", "") or ""
            is_zip = (
                mime in {"application/zip", "application/x-zip-compressed", "application/octet-stream"}
                or meta_name.lower().endswith(".zip")
            )
            if not is_zip:
                skipped += 1
                continue
            title = re.sub(r"\.zip$", "", meta_name, flags=re.IGNORECASE).strip() or f"ZIP {len(items) + 1}"
            items.append({"id": fid, "name": title})

        if not items:
            await interaction.edit_original_response(
                content=None,
                view=_error_layout("لا يوجد أي رابط صالح لملف ZIP من ضمن الروابط المُرسَلة."),
            )
            user_system.release_user_lock(user_id)
            return

        try:
            user_root = await _get_or_create_user_root_folder(service, user_id)
        except Exception as exc:
            await interaction.edit_original_response(content=None, view=_error_layout(str(exc)))
            user_system.release_user_lock(user_id)
            return

        batch_title = f"ZIP Batch ({len(items)})"
        try:
            master_id, master_link = await _create_master_clean_folder(user_root, batch_title)
        except Exception as exc:
            await interaction.edit_original_response(content=None, view=_error_layout(f"تعذّر إنشاء مجلد الدفعة: {exc}"))
            user_system.release_user_lock(user_id)
            return

        dashboard = DashboardUI(interaction, items)
        skip_note = f"\n⚠️ تم تجاهل {skipped} رابط غير صالح/ليس ملف ZIP." if skipped else ""
        await interaction.edit_original_response(
            content=f"🚀 بدء تبييض {len(items)} ملف ZIP (تبييض متعدد)...{skip_note}",
            view=dashboard.generate_layout()
        )

        async def _run_zip_batch():
            try:
                await self.process_zip_queue(
                    interaction, dashboard, items, mode, dilate_iter, remove_sfx, master_id, master_link, batch_title, lang=lang
                )
            finally:
                user_system.release_user_lock(user_id)

        asyncio.create_task(_run_zip_batch())

    async def process_queue(
        self,
        interaction: discord.Interaction,
        dashboard: DashboardUI,
        folders: list[dict],
        mode: str,
        dilate_iter: int,
        remove_sfx: bool,
        parent_folder_id: str,
        batch_title: str,
        lang: str = "ALL"
    ):
        user_id = interaction.user.id
        rank = await user_system.get_rank(user_id)

        # VIP (rank >= 2): 3s delay; Normal User: 8s delay between chapters
        inter_chapter_delay = 3 if rank >= 2 else 8

        total_pages_all = 0
        total_errors_all = 0
        t_start_batch = time.perf_counter()

        master_id = parent_folder_id
        master_link = f"https://drive.google.com/drive/folders/{parent_folder_id}"

        is_single_chapter = (len(folders) == 1)
        if not is_single_chapter:
            # Create 1 Master folder for all chapters in Drive
            try:
                await dashboard.update(folders[0]['id'], "📁 جاري إنشاء المجلد الرئيسي للتبييض في Drive...", force=True)
                master_id, master_link = await _create_master_clean_folder(parent_folder_id, batch_title)
            except Exception as e:
                log.warning("Master folder creation failed: %s, using parent folder", e)

        for idx, folder in enumerate(folders):
            if dashboard.is_cancelled:
                log.info("Batch cleaning job cancelled by user %s", user_id)
                break

            # Check and consume usage for additional chapters (idx > 0)
            if idx > 0:
                allowed_ch, credit_msg = await user_system.check_and_consume_usage(user_id, "clean")
                if not allowed_ch:
                    log.warning("User %s ran out of credits/trial at chapter index %s (%s)", user_id, idx, folder['name'])
                    await dashboard.update(
                        folder['id'],
                        f"❌ توقفت العملية (نفاد الرصيد): {credit_msg}",
                        force=True
                    )
                    try:
                        await interaction.followup.send(
                            content=f"⚠️ {interaction.user.mention} توقفت عملية التبييض عند الفصل `{folder['name']}` بسب نفاد رصيد نقاطك أو حدك اليومي المتاح.\nتم تجميع وحفظ الفصول الناجحة السابقة بنجاح.",
                            ephemeral=True
                        )
                    except Exception:
                        pass
                    break

            # Inter-chapter delay if not first chapter
            if idx > 0 and not dashboard.is_cancelled:
                for sec in range(inter_chapter_delay, 0, -1):
                    if dashboard.is_cancelled:
                        break
                    await dashboard.update(
                        folder['id'],
                        f"⏳ مهلة انتظار قبل الفصل التالي ({sec}ث)...",
                        force=True
                    )
                    await asyncio.sleep(1)

            if dashboard.is_cancelled:
                break

            pages, errors, single_clean_link = await self.process_single(
                interaction, dashboard, folder['id'], folder['name'], mode, dilate_iter, remove_sfx, master_id, lang=lang
            )
            total_pages_all += pages
            total_errors_all += errors
            if is_single_chapter and single_clean_link:
                master_link = single_clean_link


        elapsed_total = time.perf_counter() - t_start_batch

        if dashboard.is_cancelled:
            await dashboard.update(folders[0]['id'], "🛑 تم إلغاء عملية التبييض", force=True)
            return

        # Complete batch dashboard UI
        await dashboard.update(
            folders[-1]['id'],
            "✅ اكتمل التبييض بنجاح",
            f"`{'▰' * 15} 100%`",
            drive_link=master_link,
            force=True
        )

        # Post single final completion card + ping in channel
        layout = _done_layout_batch(
            user_mention=interaction.user.mention,
            batch_title=batch_title,
            drive_link=master_link,
            chapters_count=len(folders),
            total_pages=total_pages_all,
            elapsed=elapsed_total,
            total_errors=total_errors_all,
        )
        try:
            await interaction.channel.send(view=layout)
        except Exception as e:
            log.warning("Failed to send batch completion card: %s", e)

        await database.log_event("OK", f"[MangaCleaner] Batch cleaned {len(folders)} ch ({total_pages_all}p) in {elapsed_total:.0f}s")

    async def process_single(
        self,
        interaction: discord.Interaction,
        dashboard: DashboardUI,
        folder_id: str,
        folder_name: str,
        mode: str,
        dilate_iter: int,
        remove_sfx: bool,
        master_folder_id: str,
        lang: str = "ALL"
    ) -> tuple[int, int, Optional[str]]:
        job_id      = uuid.uuid4().hex[:8]
        job_dir     = TEMP_ROOT / f"job_{job_id}"
        clean_dir   = TEMP_ROOT / f"clean_{job_id}"
        t_total     = time.perf_counter()
        pages       = 0
        errors      = 0

        async with CLEANING_SEMAPHORE:
            try:
                async def _update(title: str, desc: str, colour: discord.Color = C_BLUE, progress_bar: str = None, force: bool = False):
                    if not progress_bar:
                        progress_bar = f"`{'▱' * 15} 0%`  ·  جاري العمل..."
                    await dashboard.update(folder_id, f"{title}: {desc}", progress_bar, force=force)

                # ── Step 1 — Download from Drive ──────
                last_update_time = 0.0
                async def on_download_progress(current, total):
                    nonlocal last_update_time
                    now = time.time()
                    if now - last_update_time >= 1.5 or current == total:
                        last_update_time = now
                        bar = _make_progress_bar(current, total)
                        await _update("📥 جاري التحميل", "يتم تحميل صور الفصل من Drive.", colour=C_BLUE, progress_bar=bar)

                await _update("📥 جاري التحميل", "جاري الاتصال والتحقق من الملفات...", colour=C_BLUE, progress_bar=_make_progress_bar(0, 100), force=True)
                images = await _download_drive_folder(folder_id, job_dir, self.bot, on_progress=on_download_progress)
                if not images:
                    await dashboard.update(folder_id, "❌ خطأ: لا توجد صور صالحة", force=True)
                    return 0, 1, None

                if dashboard.is_cancelled: return 0, 0, None

                # ── Step 2 — Pack into ZIP ────────────
                await _update(
                    f"📦 تعبئة {len(images)} صورة",
                    "يتم ضغط الصور وإرسالها للمعالجة.",
                    colour=C_BLUE,
                    progress_bar=_make_progress_bar(len(images), len(images)),
                    force=True
                )
                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_STORED) as zf:
                    for img_path in images:
                        zf.write(img_path, img_path.name)
                zip_bytes = zip_buf.getvalue()

                if dashboard.is_cancelled: return 0, 0, None

                # ── Step 3 — Send to Inpainting Space ─
                yolo_lama_bar = f"`{'▰' * 9}{'▱' * 6} 60%`  ·  جاري المعالجة..."
                await _update(
                    "🤖 AI يعالج النصوص",
                    f"يتم إزالة النصوص بجودة {mode}...",
                    colour=C_BLUE,
                    progress_bar=yolo_lama_bar,
                    force=True
                )

                connector = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(connector=connector) as session:
                    clean_zip_bytes, pages, errors, ai_elapsed = await _send_to_inpainting_space(
                        zip_bytes, mode, session, dilate_iter, remove_sfx, lang
                    )

                if dashboard.is_cancelled: return pages, errors, None

                # ── Step 4 — Extract cleaned images ───
                extract_bar = f"`{'▰' * 12}{'▱' * 3} 80%`  ·  جاري استخراج الصور المبيّضة..."
                await _update(
                    "📦 استخراج الصور المعالجة",
                    "يتم تحضير الصور المستلمة للرفع.",
                    colour=C_BLUE,
                    progress_bar=extract_bar,
                    force=True
                )
                clean_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(clean_zip_bytes)) as czf:
                    czf.extractall(clean_dir)
                clean_images = sorted(clean_dir.rglob("*.*"))
                clean_images = [p for p in clean_images if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]

                if dashboard.is_cancelled: return pages, errors, None

                # ── Step 5 — Upload to Master Drive ───
                last_update_time = 0.0
                async def on_upload_progress(current, total):
                    nonlocal last_update_time
                    now = time.time()
                    if now - last_update_time >= 1.5 or current == total:
                        last_update_time = now
                        bar = _make_progress_bar(current, total)
                        await _update("☁️ رفع الصور النظيفة", "يتم رفع الصور المعالجة إلى Google Drive.", colour=C_BLUE, progress_bar=bar)

                await _update(
                    "☁️ رفع الصور النظيفة",
                    f"يتم رفع {len(clean_images)} صورة...",
                    colour=C_BLUE,
                    progress_bar=_make_progress_bar(0, len(clean_images)),
                    force=True
                )
                
                try:
                    drive_clean_link, _clean_folder_id = await _upload_to_drive(
                        clean_images, master_folder_id, folder_name, on_progress=on_upload_progress
                    )
                except Exception as drive_err:
                    log.warning("Drive upload failed for %s (non-fatal): %s", folder_name, drive_err)
                    drive_clean_link = None

                # ── Step 6 — Chapter Done ──────────────
                err_desc = f" (تخطي {errors} أخطاء)" if errors else ""
                await dashboard.update(
                    folder_id,
                    f"✅ اكتمل التبييض{err_desc}",
                    f"`{'▰' * 15} 100%`  ·  `{pages}/{pages}` صفحة",
                    drive_link=drive_clean_link,
                    force=True
                )
                
                return pages, errors, drive_clean_link

            except Exception as exc:
                log.exception("clean_manga job %s failed: %s", job_id, exc)
                await dashboard.update(folder_id, f"❌ خطأ: {exc}", force=True)
                await database.log_event("ERROR", f"[MangaCleaner] job {job_id}: {exc}")
                return 0, 1, None
            finally:
                for d in (job_dir, clean_dir):
                    if d.exists():
                        shutil.rmtree(d, ignore_errors=True)

    async def process_single_zip(
        self,
        interaction: discord.Interaction,
        dashboard: DashboardUI,
        item_id: str,
        file_id: str,
        zip_title: str,
        mode: str,
        dilate_iter: int,
        remove_sfx: bool,
        upload_parent_id: str,
        lang: str = "ALL",
    ) -> tuple[int, int, Optional[str], Optional[str]]:
        """
        نفس منطق process_single تماماً، لكن المصدر هنا ملف ZIP مرفوع على Drive
        بدل مجلد صور — يُستخدم من أمري /clean_zip و /clean_zip_multi.
        يُرجع (pages, errors, drive_link, created_folder_id).
        """
        job_id    = uuid.uuid4().hex[:8]
        job_dir   = TEMP_ROOT / f"zipjob_{job_id}"
        clean_dir = TEMP_ROOT / f"zipclean_{job_id}"
        pages     = 0
        errors    = 0

        async with CLEANING_SEMAPHORE:
            try:
                async def _update(title: str, desc: str, colour: discord.Color = C_BLUE, progress_bar: str = None, force: bool = False):
                    if not progress_bar:
                        progress_bar = f"`{'▱' * 15} 0%`  ·  جاري العمل..."
                    await dashboard.update(item_id, f"{title}: {desc}", progress_bar, force=force)

                # ── Step 1 — Download & extract the ZIP from Drive ──────
                await _update(
                    "📥 جاري التحميل", "يتم تحميل ملف الـZIP من Drive...",
                    colour=C_BLUE, progress_bar=_make_progress_bar(0, 100), force=True
                )

                job_dir.mkdir(parents=True, exist_ok=True)
                from drive_stitch import build_drive_service, download_file
                loop = asyncio.get_event_loop()
                service = await loop.run_in_executor(None, build_drive_service)

                zip_local_path = job_dir / "source.zip"
                await loop.run_in_executor(None, lambda: download_file(service, file_id, str(zip_local_path)))

                extract_dir = job_dir / "extracted"
                extract_dir.mkdir(parents=True, exist_ok=True)

                def _extract():
                    with zipfile.ZipFile(zip_local_path) as zf:
                        zf.extractall(extract_dir)
                    exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
                    return sorted(p for p in extract_dir.rglob("*.*") if p.suffix.lower() in exts)

                images = await loop.run_in_executor(None, _extract)
                images = images[:MAX_IMAGES]

                if not images:
                    await dashboard.update(item_id, "❌ خطأ: لا توجد صور صالحة داخل ملف الـZIP", force=True)
                    return 0, 1, None, None

                if dashboard.is_cancelled: return 0, 0, None, None

                # ── Step 2 — Pack into ZIP ────────────
                await _update(
                    f"📦 تعبئة {len(images)} صورة",
                    "يتم ضغط الصور وإرسالها للمعالجة.",
                    colour=C_BLUE,
                    progress_bar=_make_progress_bar(len(images), len(images)),
                    force=True
                )
                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_STORED) as zf:
                    for img_path in images:
                        zf.write(img_path, img_path.name)
                zip_bytes = zip_buf.getvalue()

                if dashboard.is_cancelled: return 0, 0, None, None

                # ── Step 3 — Send to Inpainting Space ─
                yolo_lama_bar = f"`{'▰' * 9}{'▱' * 6} 60%`  ·  جاري المعالجة..."
                await _update(
                    "🤖 AI يعالج النصوص",
                    f"يتم إزالة النصوص بجودة {mode}...",
                    colour=C_BLUE,
                    progress_bar=yolo_lama_bar,
                    force=True
                )

                connector = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(connector=connector) as session:
                    clean_zip_bytes, pages, errors, ai_elapsed = await _send_to_inpainting_space(
                        zip_bytes, mode, session, dilate_iter, remove_sfx, lang
                    )

                if dashboard.is_cancelled: return pages, errors, None, None

                # ── Step 4 — Extract cleaned images ───
                extract_bar = f"`{'▰' * 12}{'▱' * 3} 80%`  ·  جاري استخراج الصور المبيّضة..."
                await _update(
                    "📦 استخراج الصور المعالجة",
                    "يتم تحضير الصور المستلمة للرفع.",
                    colour=C_BLUE,
                    progress_bar=extract_bar,
                    force=True
                )
                clean_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(clean_zip_bytes)) as czf:
                    czf.extractall(clean_dir)
                clean_images = sorted(clean_dir.rglob("*.*"))
                clean_images = [p for p in clean_images if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]

                if dashboard.is_cancelled: return pages, errors, None, None

                # ── Step 5 — Upload to Drive (مجلد المستخدم الدائم) ───
                last_update_time = 0.0
                async def on_upload_progress(current, total):
                    nonlocal last_update_time
                    now = time.time()
                    if now - last_update_time >= 1.5 or current == total:
                        last_update_time = now
                        bar = _make_progress_bar(current, total)
                        await _update("☁️ رفع الصور النظيفة", "يتم رفع الصور المعالجة إلى Google Drive.", colour=C_BLUE, progress_bar=bar)

                await _update(
                    "☁️ رفع الصور النظيفة",
                    f"يتم رفع {len(clean_images)} صورة...",
                    colour=C_BLUE,
                    progress_bar=_make_progress_bar(0, len(clean_images)),
                    force=True
                )

                created_folder_id = None
                drive_clean_link = None
                try:
                    drive_clean_link, created_folder_id = await _upload_to_drive(
                        clean_images, upload_parent_id, zip_title, on_progress=on_upload_progress
                    )
                except Exception as drive_err:
                    log.warning("Drive upload failed for %s (non-fatal): %s", zip_title, drive_err)

                # ── Step 6 — Done ──────────────
                err_desc = f" (تخطي {errors} أخطاء)" if errors else ""
                await dashboard.update(
                    item_id,
                    f"✅ اكتمل التبييض{err_desc}",
                    f"`{'▰' * 15} 100%`  ·  `{pages}/{pages}` صفحة",
                    drive_link=drive_clean_link,
                    force=True
                )

                return pages, errors, drive_clean_link, created_folder_id

            except Exception as exc:
                log.exception("clean_zip job %s failed: %s", job_id, exc)
                await dashboard.update(item_id, f"❌ خطأ: {exc}", force=True)
                await database.log_event("ERROR", f"[MangaCleaner-ZIP] job {job_id}: {exc}")
                return 0, 1, None, None
            finally:
                for d in (job_dir, clean_dir):
                    if d.exists():
                        shutil.rmtree(d, ignore_errors=True)

    async def process_zip_queue(
        self,
        interaction: discord.Interaction,
        dashboard: DashboardUI,
        items: list[dict],
        mode: str,
        dilate_iter: int,
        remove_sfx: bool,
        master_id: str,
        master_link: str,
        batch_title: str,
        lang: str = "ALL",
    ):
        """معالجة دفعة ملفات ZIP لأمر /clean_zip_multi — كلها تُرفَع داخل مجلد
        دفعة واحد (master_id) في مساحة تخزين المستخدم الخاصة، ويُجدوَل حذفه
        تلقائياً مرة واحدة فقط بعد اكتمال الدفعة بالكامل."""
        user_id = interaction.user.id
        rank = await user_system.get_rank(user_id)
        inter_delay = 3 if rank >= 2 else 8

        total_pages_all = 0
        total_errors_all = 0
        t_start_batch = time.perf_counter()
        any_uploaded = False

        for idx, item in enumerate(items):
            if dashboard.is_cancelled:
                log.info("ZIP batch job cancelled by user %s", user_id)
                break

            if idx > 0:
                allowed_ch, credit_msg = await user_system.check_and_consume_usage(user_id, "clean")
                if not allowed_ch:
                    log.warning("User %s ran out of credits/trial at zip index %s (%s)", user_id, idx, item['name'])
                    await dashboard.update(item['id'], f"❌ توقفت العملية (نفاد الرصيد): {credit_msg}", force=True)
                    try:
                        await interaction.followup.send(
                            content=(
                                f"⚠️ {interaction.user.mention} توقفت عملية التبييض المتعدد عند "
                                f"`{item['name']}` بسبب نفاد رصيد نقاطك أو حدك اليومي المتاح.\n"
                                "تم حفظ نتائج الملفات الناجحة السابقة بنجاح."
                            ),
                            ephemeral=True
                        )
                    except Exception:
                        pass
                    break

            if idx > 0 and not dashboard.is_cancelled:
                for sec in range(inter_delay, 0, -1):
                    if dashboard.is_cancelled:
                        break
                    await dashboard.update(item['id'], f"⏳ مهلة انتظار قبل الملف التالي ({sec}ث)...", force=True)
                    await asyncio.sleep(1)

            if dashboard.is_cancelled:
                break

            pages, errors, item_link, _folder_id = await self.process_single_zip(
                interaction, dashboard, item['id'], item['id'], item['name'], mode, dilate_iter, remove_sfx, master_id, lang=lang
            )
            total_pages_all += pages
            total_errors_all += errors
            if item_link:
                any_uploaded = True

        elapsed_total = time.perf_counter() - t_start_batch

        if dashboard.is_cancelled:
            await dashboard.update(items[0]['id'], "🛑 تم إلغاء عملية التبييض", force=True)
            return

        await dashboard.update(
            items[-1]['id'],
            "✅ اكتمل التبييض بنجاح",
            f"`{'▰' * 15} 100%`",
            drive_link=master_link,
            force=True
        )

        if any_uploaded:
            await _schedule_drive_cleanup(user_id, master_id, batch_title)

        layout = _done_layout_batch(
            user_mention=interaction.user.mention,
            batch_title=batch_title,
            drive_link=master_link,
            chapters_count=len(items),
            total_pages=total_pages_all,
            elapsed=elapsed_total,
            total_errors=total_errors_all,
            expires_hours=ZIP_CLEANUP_HOURS if any_uploaded else None,
        )
        try:
            await interaction.channel.send(view=layout)
        except Exception as exc:
            log.warning("Failed to send zip-batch completion card: %s", exc)

        await database.log_event(
            "OK", f"[MangaCleaner-ZIP] Batch cleaned {len(items)} zip(s) ({total_pages_all}p) in {elapsed_total:.0f}s"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MangaCleanerCog(bot))

