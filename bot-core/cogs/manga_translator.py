"""
cogs/manga_translator.py — Manga Text Extraction Cog
Discord Bot: MangaSystem

Slash commands: /extract, /settings
"""

from __future__ import annotations

import asyncio
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
from discord.ext import commands

import database
from bot_config import Config
from user_system import user_only
import user_system

# Modular Imports
from utils.translator_docs import create_docx, create_plain_text
from ui.translator_ui import (
    _error_layout, _make_progress_bar, ChapterSelectView, SettingsView, DashboardUI
)

log = logging.getLogger("manga_translator")

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
TEMP_ROOT      = Path("temp_translator")
MAX_IMAGES     = 200          # safety cap
DRIVE_RE       = re.compile(
    r"https://drive\.google\.com/(?:drive/folders/|open\?id=)([\w-]+)"
)

TRANSLATOR_SEMAPHORE = asyncio.Semaphore(1)


# ──────────────────────────────────────────────
# Google Drive Helpers
# ──────────────────────────────────────────────

def _extract_folder_id(url: str) -> Optional[str]:
    m = DRIVE_RE.search(url)
    if m: return m.group(1)
    parts = url.strip("/").split("/")
    for part in reversed(parts):
        if len(part) >= 25 and re.match(r'^[a-zA-Z0-9_-]+$', part):
            return part
    return None


async def _download_drive_folder_concurrent(folder_id: str, dest: Path, on_progress) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    from drive_stitch import build_drive_service, list_folder_images, download_file
    loop = asyncio.get_event_loop()
    service = await loop.run_in_executor(None, build_drive_service)
    files = await loop.run_in_executor(None, lambda: list_folder_images(service, folder_id))
    if not files: return []
    
    target_files = files[:MAX_IMAGES]
    total = len(target_files)
    completed = 0
    paths = []
    
    async def dl_task(f):
        nonlocal completed
        out_path = dest / f["name"]
        await loop.run_in_executor(None, lambda: download_file(service, f["id"], str(out_path)))
        completed += 1
        paths.append(out_path)
        if on_progress: await on_progress(completed, total)
        
    await asyncio.gather(*(dl_task(f) for f in target_files))
    return sorted(paths)


async def _upload_to_drive(file_path: str, parent_folder_id: str, file_name: str, mime_type: str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document") -> str:
    from drive_stitch import build_drive_service
    from googleapiclient.http import MediaFileUpload
    loop = asyncio.get_event_loop()
    def _do_upload():
        service = build_drive_service()
        file_metadata = {'name': file_name, 'parents': [parent_folder_id]}
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return f"https://drive.google.com/file/d/{file.get('id')}/view?usp=sharing"
    return await loop.run_in_executor(None, _do_upload)


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
# Cog
# ──────────────────────────────────────────────

class MangaTranslatorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        TEMP_ROOT.mkdir(exist_ok=True)

    @app_commands.command(name="extract_settings", description="إعدادات وترتيب نصوص الاستخراج الشخصية")
    @user_only()
    async def settings_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        profiles = await database.get_user_profiles(interaction.user.id)
        view = SettingsView(interaction, interaction.user.id, profiles)
        await interaction.followup.send(
            content="⚙️ **إعدادات استخراج النصوص الشخصية:**",
            view=view.generate_main_layout(),
            ephemeral=True
        )

    @app_commands.command(name="extract", description="استخراج وقراءة نصوص المانجا من مجلد Google Drive")
    @app_commands.describe(
        drive_url="رابط مجلد Google Drive المحتوي على صور الفصل",
        lang="لغة الترجمة والاستخراج (auto / ar / en / ja / ko / zh)"
    )
    @user_only()
    async def extract_cmd(self, interaction: discord.Interaction, drive_url: str, lang: str = "auto"):
        # ── Check Limits & Credits ────────────
        allowed, usage_msg = await user_system.check_and_consume_usage(interaction.user.id, "extract")
        if not allowed:
            return await interaction.response.send_message(view=_error_layout(usage_msg), ephemeral=True)

        folder_id = _extract_folder_id(drive_url)
        if not folder_id:
            return await interaction.response.send_message(view=_error_layout("الرابط غير صحيح!"), ephemeral=True)

        if not getattr(Config, "INPAINTING_SPACE_URL", None):
            return await interaction.response.send_message(view=_error_layout("لم يُعدّ الـ Inpainting Space في .env"), ephemeral=True)

        await interaction.response.defer(thinking=True)
        if usage_msg:
            try:
                await interaction.followup.send(content=usage_msg, ephemeral=True)
            except Exception:
                pass

        # Fetch user's active settings
        active_settings = await database.get_active_user_settings(interaction.user.id)

        subfolders = await _get_subfolders(folder_id)
        if subfolders:
            # Show Dropdown
            view = ChapterSelectView(interaction, subfolders, self, lang)
            await interaction.edit_original_response(
                content="📂 **عثرت على مجلدات فرعية في هذا الرابط!** يرجى تحديد الفصول التي تريد استخراجها:",
                view=view
            )
        else:
            # Process as single folder
            from drive_stitch import build_drive_service
            loop = asyncio.get_event_loop()
            service = await loop.run_in_executor(None, build_drive_service)
            meta = await loop.run_in_executor(None, lambda: service.files().get(fileId=folder_id, fields="name").execute())
            name = meta.get('name', 'Chapter')
            
            dashboard = DashboardUI(interaction, [{'id': folder_id, 'name': name}])
            await dashboard.update(folder_id, "⏳ جاري التجهيز...")
            await self.process_queue(interaction, dashboard, [{'id': folder_id, 'name': name}], lang, active_settings)


    async def process_queue(self, interaction: discord.Interaction, dashboard: DashboardUI, folders: list[dict], lang: str, settings: dict):
        for folder in folders:
            if dashboard.is_cancelled: break
            await self.process_single(interaction, dashboard, folder['id'], folder['name'], lang, settings)


    async def process_single(self, interaction: discord.Interaction, dashboard: DashboardUI, folder_id: str, folder_name: str, lang: str, settings: dict):
        job_id = uuid.uuid4().hex[:8]
        job_dir = TEMP_ROOT / f"ocr_{job_id}"
        t_total = time.perf_counter()

        async with TRANSLATOR_SEMAPHORE:
            try:
                # 1. Download from Drive concurrently
                last_update = 0.0
                async def progress_cb(cur, tot):
                    nonlocal last_update
                    now = time.time()
                    if now - last_update >= 1.5 or cur == tot:
                        last_update = now
                        await dashboard.update(folder_id, "📥 جاري التحميل...", _make_progress_bar(cur, tot))

                await dashboard.update(folder_id, "📥 الاتصال بـ Drive...")
                images = await _download_drive_folder_concurrent(folder_id, job_dir, progress_cb)
                
                if not images:
                    await dashboard.update(folder_id, "❌ فشل (لا توجد صور)")
                    return

                # 2 & 3. Batch to AI (Chunking with DB Cache and Retry logic)
                chunk_size = 25
                chunks = [images[i:i+chunk_size] for i in range(0, len(images), chunk_size)]
                
                cached_results = []
                uncached_chunks = []
                for idx, chunk in enumerate(chunks):
                    cached_data = await database.get_cached_chunk(folder_id, lang, idx)
                    if cached_data:
                        cached_results.append((idx, cached_data.get("pages", [])))
                    else:
                        uncached_chunks.append((idx, chunk))

                remove_sfx_val = "true" if settings.get("remove_sfx") else "false"
                connected_slashes_val = "true" if settings.get("connected_slashes") else "false"

                async def process_chunk_with_retry(chunk_idx, img_chunk):
                    # Double-check cache lookup
                    cached_data = await database.get_cached_chunk(folder_id, lang, chunk_idx)
                    if cached_data:
                        return chunk_idx, cached_data.get("pages", [])

                    zip_buf = io.BytesIO()
                    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_STORED) as zf:
                        for img_path in img_chunk:
                            zf.write(img_path, img_path.name)
                    
                    data = aiohttp.FormData()
                    data.add_field("file", zip_buf.getvalue(), filename=f"chunk_{chunk_idx}.zip", content_type="application/zip")
                    headers = {"X-API-Key": Config.INPAINTING_SPACE_KEY}
                    
                    # 3 retries with backoff
                    for attempt in range(1, 4):
                        try:
                            connector = aiohttp.TCPConnector(ssl=False)
                            async with aiohttp.ClientSession(connector=connector) as session:
                                async with session.post(
                                    f"{Config.INPAINTING_SPACE_URL.rstrip('/')}/process_ocr_zip?lang={lang}&remove_sfx={remove_sfx_val}&connected_slashes={connected_slashes_val}",
                                    headers=headers, data=data, timeout=aiohttp.ClientTimeout(total=300)
                                ) as resp:
                                    if resp.status != 200:
                                        raise RuntimeError(f"Server returned status code {resp.status}")
                                    res = await resp.json()
                                    # Save to cache
                                    await database.save_cached_chunk(folder_id, lang, chunk_idx, res)
                                    return chunk_idx, res.get("pages", [])
                        except Exception as e:
                            log.warning("Attempt %d failed for chunk %d: %s", attempt, chunk_idx, e)
                            if attempt == 3:
                                raise RuntimeError(f"Failing after 3 attempts on chunk {chunk_idx}: {e}")
                            await asyncio.sleep(attempt * 3)

                if uncached_chunks:
                    # Update progress view
                    total_done = len(cached_results)
                    total_chunks = len(chunks)
                    await dashboard.update(folder_id, "🤖 AI يقرأ النصوص...", _make_progress_bar(total_done, total_chunks))
                    
                    chunk_tasks = [process_chunk_with_retry(idx, chunk) for idx, chunk in uncached_chunks]
                    new_results = await asyncio.gather(*chunk_tasks)
                    chunk_results = cached_results + new_results
                else:
                    chunk_results = cached_results
                
                # Sort results by chunk index to maintain order
                chunk_results.sort(key=lambda x: x[0])
                
                all_pages = []
                current_page = 1
                for _, pages in chunk_results:
                    for p in pages:
                        p["page_num"] = current_page
                        all_pages.append(p)
                        current_page += 1
                        
                result_json = {"pages": all_pages}

                # 4. Generate DOCX / TXT based on format preference
                await dashboard.update(folder_id, "📝 توليد الملفات المعالجة...", f"`{'▰' * 13}{'▱' * 2} 90%`")
                
                output_fmt = settings.get("output_format", "BOTH")
                settings_with_lang = settings.copy() if settings else {}
                settings_with_lang["target_lang"] = lang
                docx_buf = create_docx(folder_name, result_json.get("pages", []), settings_with_lang)
                txt_buf = create_plain_text(folder_name, result_json.get("pages", []), settings_with_lang)
                
                docx_path = job_dir / f"{folder_name}.docx"
                txt_path = job_dir / f"{folder_name}.txt"
                
                with open(docx_path, "wb") as f:
                    f.write(docx_buf.getvalue())
                with open(txt_path, "wb") as f:
                    f.write(txt_buf.getvalue())

                # 5. Upload to EXTRACTIONS folder if configured, else parent folder
                drive_link = ""
                extractions_id = getattr(Config, "EXTRACTIONS_FOLDER_ID", None)
                target_drive_id = extractions_id if extractions_id else folder_id
                
                if target_drive_id:
                    try:
                        if output_fmt in ("DOCX", "BOTH"):
                            await _upload_to_drive(str(docx_path), target_drive_id, f"{folder_name}.docx")
                        if output_fmt in ("TXT", "BOTH"):
                            await _upload_to_drive(
                                str(txt_path),
                                target_drive_id,
                                f"{folder_name}.txt",
                                mime_type="text/plain",
                            )
                        drive_link = f"https://drive.google.com/drive/folders/{target_drive_id}"
                    except Exception as e:
                        log.warning("Failed to upload extraction files to drive: %s", e)

                # 6. Send directly to Discord Chat (filtering attachments by format preference)
                files_to_send = []
                if output_fmt in ("DOCX", "BOTH"):
                    files_to_send.append(discord.File(str(docx_path), filename=f"{folder_name}.docx"))
                if output_fmt in ("TXT", "BOTH"):
                    files_to_send.append(discord.File(str(txt_path), filename=f"{folder_name}.txt"))

                try:
                    await interaction.channel.send(
                        content=f"🎉 **{folder_name}**: تم الانتهاء بنجاح!",
                        files=files_to_send
                    )
                except Exception as e:
                    log.error("Failed to send files to discord: %s", e)

                await dashboard.update(folder_id, "✅ اكتمل الاستخراج", f"`{'▰' * 15} 100%`", drive_link=drive_link)
                await database.log_event("OK", f"[MangaTranslator] Extracted {folder_name} in {time.perf_counter() - t_total:.0f}s")

            except Exception as exc:
                log.exception("extract job %s failed: %s", job_id, exc)
                await dashboard.update(folder_id, f"❌ خطأ: {exc}")
            finally:
                if job_dir.exists():
                    shutil.rmtree(job_dir, ignore_errors=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MangaTranslatorCog(bot))
