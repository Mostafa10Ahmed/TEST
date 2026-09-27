from __future__ import annotations

import asyncio
import datetime
import os
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

import database
from bot_config import Config
from download_ui import LOGO_DRIVE, LOGO_GOFILE
from ui.components_v2 import (
    build_download_completed_layout,
    build_progress_layout,
    chapter_label_from_title,
    display_series_name,
    format_chapter_line,
)
from providers.lekmanga_provider import CloudflareBlockedError
from user_system import vip_only


C_BLUE = discord.Color.from_rgb(88, 101, 242)
C_GREEN = discord.Color.from_rgb(87, 242, 135)
C_RED = discord.Color.from_rgb(237, 66, 69)

ERROR_CODE_MESSAGES = {
    "timeout": "⏱️ انتهت مهلة التنفيذ على Worker. حاول مرة أخرى أو قلّل حجم المهمة.",
    "download_failed": "📥 فشل تحميل الصور من المصدر. تحقق من الرابط أو جرّب مصدرًا آخر.",
    "stitch_failed": "🧵 فشل دمج الصور (SmartStitch). جرّب إعدادات أقل أو رابطًا مختلفًا.",
    "upload_failed": "☁️ فشل الرفع (Gofile/Catbox/Drive). حاول لاحقًا أو غيّر وجهة الرفع.",
    "internal_error": "⚠️ حدث خطأ داخلي في Worker. أعد المحاولة بعد لحظات.",
}


async def _edit_completion_v2(
    msg: discord.WebhookMessage,
    *,
    series_name: str,
    series_url: str,
    chapter_line: str = "",
    main_link: str | None,
    provider: str = "",
    cover_url: str | None = None,
    multi_folder: bool = False,
) -> None:
    layout = build_download_completed_layout(
        series_name=series_name,
        series_url=series_url,
        chapter_line=chapter_line,
        main_link=main_link,
        provider=provider,
        cover_url=cover_url,
        multi_folder=multi_folder,
    )
    await msg.edit(embed=None, view=layout)


async def _fetch_series_cover(bot: commands.Bot, url: str) -> str | None:
    pm = getattr(bot, "provider_mgr", None)
    if not pm:
        return None
    try:
        cover = await pm.get_series_cover(url)
        if cover and str(cover).startswith("http"):
            return str(cover)
    except Exception:
        pass
    return None


def _completion_labels(url: str, title: str, series_title: str = None) -> tuple[str, str]:
    """(series_name, chapter_line) for download completion UI."""
    series = series_title or display_series_name(url, fallback=title)
    if title and title.lower() not in ("manga_chapter", "chapter"):
        if "_" in title and re.search(r"ch", title, re.I):
            series = series_title or display_series_name(url, fallback=title.rsplit("_", 1)[0].replace("_", " ").title())
    ch = chapter_label_from_title(title, url)
    return series, format_chapter_line(single=ch)


def hf_error_message(result: dict) -> str:
    code = result.get("error_code")
    base = ERROR_CODE_MESSAGES.get(code, "❌ فشلت المهمة على Worker.")
    details = (result.get("message") or result.get("error_details") or "").strip()
    if details:
        return f"{base}\n\nالتفاصيل: `{details[:180]}`"
    return base


def is_media_url(url: str) -> bool:
    media_domains = [
        "youtube.com", "youtu.be", "tiktok.com", "instagram.com", 
        "twitter.com", "x.com", "facebook.com", "fb.watch", "reddit.com",
        "pinterest.com", "vimeo.com", "twitch.tv", "reels"
    ]
    url_lower = url.lower()
    return any(domain in url_lower for domain in media_domains)


class DownloadsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="stitch_drive", description="دمج وقص الصور من مجلد Google Drive")
    @vip_only()
    async def stitch_drive_cmd(
        self,
        interaction: discord.Interaction,
        drive_url: str,
        title: str = "chapter",
        width: int = 800,
        height: int = 14500,
        sensitivity: int = 90,
        destination: str = "Gofile",
    ):
        downloader = getattr(self.bot, "downloader")
        remote_down = getattr(self.bot, "remote_down")
        metrics = getattr(self.bot, "metrics", None)
        started_at = time.perf_counter()

        width = max(200, min(4000, width))
        height = max(3000, min(50000, height))
        sensitivity = max(1, min(100, sensitivity))

        if not Config.GOOGLE_SERVICE_ACCOUNT_JSON:
            return await interaction.response.send_message(
                "❌ **Google Drive غير مضبوط.** أضف `GOOGLE_SERVICE_ACCOUNT_JSON` في `.env`.",
                ephemeral=True,
            )

        await interaction.response.defer()
        state = {"phase": "⏳ جاري التجهيز...", "pct": 0, "link": None, "color": C_BLUE}

        def build_layout():
            bar = downloader.create_progress_bar(state["pct"], 100)
            return build_progress_layout(
                title=f"🧵 SmartStitch — {title}",
                phase=state["phase"],
                progress_bar=bar,
                counter=f"{state['pct']}%",
                provider=destination if destination == "Drive" else "Google Drive",
                color=state["color"],
            )

        msg = await interaction.followup.send(view=build_layout())
        last_upd = 0.0

        async def pcb(cur, tot, txt):
            nonlocal last_upd
            pct = min(100, int(cur * 100 / max(tot, 1))) if tot else cur
            state["phase"] = txt
            state["pct"] = pct
            now = asyncio.get_running_loop().time()
            if now - last_upd < 1.5 and pct < 100:
                return
            last_upd = now
            try:
                await msg.edit(embed=None, view=build_layout())
            except Exception:
                pass

        try:
            if destination != "Drive" and remote_down.is_enabled:
                state["phase"] = "🖥️ إرسال إلى HF Worker..."
                await msg.edit(embed=None, view=build_layout())
                job = await remote_down.start_stitch(drive_url, title, width, height, sensitivity)
                if "error" in job:
                    raise Exception(f"Worker Error: {job['error']}")
                result = await remote_down.wait_for_job(job["job_id"], progress_callback=pcb)
                if result.get("status") == "completed":
                    link = result.get("result")
                    prov = "Gofile" if link and "gofile.io" in link else "HF Space"
                    await _edit_completion_v2(
                        msg,
                        series_name=title,
                        series_url=link or drive_url,
                        chapter_line=f"**SmartStitch** · `{title}`",
                        main_link=link,
                        provider=prov,
                    )
                    if metrics:
                        metrics.inc("stitch_ok")
                        metrics.add_download_duration(time.perf_counter() - started_at)
                    return
                raise Exception(hf_error_message(result))

            from drive_stitch import stitch_from_drive

            final = await stitch_from_drive(
                drive_url=drive_url,
                title=title,
                target_height=height,
                target_width=width,
                sensitivity=sensitivity,
                progress_callback=pcb,
            )
            if not final or not os.path.exists(final):
                state["phase"] = "❌ فشل المعالجة"
                state["color"] = C_RED
                await msg.edit(embed=None, view=build_layout())
                return

            size_mb = os.path.getsize(final) / (1024 * 1024)
            state["phase"] = f"📤 رفع الملف ({size_mb:.1f} MB)..."
            await msg.edit(embed=None, view=build_layout())

            link = None
            if destination == "Drive":
                if Config.GOOGLE_DRIVE_FOLDER_ID:
                    link = await downloader.upload_to_gdrive(
                        final, f"{title}_stitched.zip", progress_callback=pcb
                    )
                if not link:
                    destination = "Gofile"  # Fallback if Drive upload failed

            if not link:
                for pname, pfn in [
                    ("Gofile", lambda f: downloader.upload_to_gofile(f, progress_callback=pcb)),
                    ("Catbox", lambda f: downloader.upload_to_catbox(f, progress_callback=pcb)),
                ]:
                    if destination == pname or (destination not in ("Gofile", "Catbox") and pname == "Gofile"):
                        link = await pfn(final)
                        if link:
                            break
            downloader.cleanup(final)

            if link:
                prov = "Google Drive" if destination == "Drive" else ("Gofile" if "gofile.io" in link else "Catbox")
                await _edit_completion_v2(
                    msg,
                    series_name=title,
                    series_url=link,
                    chapter_line=f"**SmartStitch** · `{title}`",
                    main_link=link,
                    provider=prov,
                )
                await database.log_event("OK", f"DriveStitch done for user {interaction.user.id}: {title} to {prov}")
                if metrics:
                    metrics.inc("stitch_ok")
                    metrics.add_download_duration(time.perf_counter() - started_at)
            else:
                state["phase"] = "❌ فشل رفع الملف"
                state["color"] = C_RED
                await msg.edit(embed=None, view=build_layout())
                if metrics:
                    metrics.inc("stitch_fail")
        except Exception as e:
            state["phase"] = f"❌ خطأ: {str(e)[:100]}"
            state["color"] = C_RED
            try:
                await msg.edit(embed=None, view=build_layout())
            except Exception:
                pass
            await database.log_event("ERROR", f"DriveStitch error for {interaction.user.id}: {e}")
            if metrics:
                metrics.inc("stitch_fail")

    @app_commands.command(name="download", description="تنزيل فصول المانجا أو الوسائط (صوت/فيديو)")
    @app_commands.describe(
        url="رابط الفصل أو الفيديو/الصوت",
        title="اسم المجلد أو السلسلة (اختياري)",
        destination="وجهة الرفع (Auto / Gofile / Catbox / Discord)",
        audio_only="تنزيل الصوت فقط؟ (للفيديوهات)"
    )
    @vip_only()
    async def download_cmd(
        self,
        interaction: discord.Interaction,
        url: str,
        title: str = "Manga_Chapter",
        destination: str = "Auto",
        audio_only: bool = False,
    ):
        url = url.strip()
        if is_media_url(url):
            await self._download_media_internal(interaction, url, audio_only)
        else:
            await self._download_chapter_internal(interaction, url, title, destination)

    async def _download_chapter_internal(self, interaction: discord.Interaction, url: str, title: str = "Manga_Chapter", destination: str = "Auto"):
        downloader = getattr(self.bot, "downloader")
        remote_down = getattr(self.bot, "remote_down")
        metrics = getattr(self.bot, "metrics", None)
        started_at = time.perf_counter()
        await interaction.response.defer()

        # Check if comix.to series URL is passed instead of chapter URL
        if "comix.to" in url.lower() and "/title/" in url.lower() and not re.search(r"/\d+-chapter-", url.lower()):
            em = discord.Embed(
                title=f"📥 Downloading — {title}",
                description="**Status:** ❌ خطأ\n\nيبدو أنك أدخلت رابط السلسلة الرئيسي بدلاً من رابط الفصل.\nيرجى استخدام رابط الفصل المباشر.\n\nمثال لرابط فصل: `https://comix.to/title/.../...-chapter-...`",
                color=C_RED,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            em.set_author(name="Downloader — Comix", icon_url=interaction.user.display_avatar.url)
            em.set_footer(text=f"Requested by {interaction.user.name}", icon_url=interaction.user.display_avatar.url)
            await interaction.followup.send(embed=em)
            return


        # Get series title concurrently
        series_title_task = None
        pm = getattr(self.bot, "provider_mgr", None)
        if pm:
            series_title_task = asyncio.create_task(pm.get_series_title(url))

        async def get_scraped_title():
            if series_title_task:
                try:
                    return await asyncio.wait_for(series_title_task, timeout=5)
                except Exception:
                    pass
            return None

        DISCORD_LIMIT_MB = 10.0

        if destination == "Auto":
            destination = await database.get_setting("default_upload_dest", "Auto")

        state = {
            "phase": "🔄 تهيئة",
            "progress": downloader.create_progress_bar(0, 1),
            "counter": "0/1",
            "size": "—",
            "provider": "—",
            "link": None,
            "color": C_BLUE,
        }

        def build_layout():
            return build_progress_layout(
                title=f"📥 Downloading — {title}",
                phase=state["phase"],
                progress_bar=state["progress"],
                counter=state["counter"],
                provider=state["provider"],
                color=state["color"],
            )

        msg = await interaction.followup.send(view=build_layout())
        last_upd = 0.0

        async def pcb(cur, tot, txt):
            nonlocal last_upd
            state["phase"] = txt
            state["progress"] = downloader.create_progress_bar(cur, tot)
            state["counter"] = f"{cur}/{tot}"
            now = asyncio.get_running_loop().time()
            if now - last_upd < 1.25 and cur < tot:
                return
            last_upd = now
            try:
                await msg.edit(embed=None, view=build_layout())
            except Exception:
                pass

        try:
            if remote_down.is_enabled:
                try:
                    state["phase"] = "🖥️ إرسال إلى HF Worker..."
                    await msg.edit(embed=None, view=build_layout())
                    job = await remote_down.start_download(url, title)
                    if "error" not in job:
                        result = await remote_down.wait_for_job(job["job_id"], progress_callback=pcb)
                        if result.get("status") == "completed":
                            link = result.get("result")
                            prov = "Google Drive" if link and "drive.google.com" in link else ("Gofile" if link and "gofile.io" in link else "HF Space")
                            series_title = await get_scraped_title()
                            series, ch_line = _completion_labels(url, title, series_title=series_title)
                            cover = await _fetch_series_cover(self.bot, url)
                            await _edit_completion_v2(
                                msg,
                                series_name=series,
                                series_url=url,
                                chapter_line=ch_line,
                                main_link=link,
                                provider=prov,
                                cover_url=cover,
                            )
                            if metrics:
                                metrics.inc("download_ok")
                                metrics.add_download_duration(time.perf_counter() - started_at)
                            return
                except Exception as worker_err:
                    print(f"[Downloads] Remote worker failed: {worker_err}. Falling back to local execution...")

            result = await downloader.download_and_stitch(url, title, progress_callback=pcb, upload_dest=destination)
            if result and result.get("type") in ("drive_folder", "gofile", "catbox"):
                prov = result.get("type").replace("_", " ").title()
                if prov == "Drive Folder":
                    prov = "Google Drive"
                series_title = await get_scraped_title()
                series, ch_line = _completion_labels(url, title, series_title=series_title)
                cover = await _fetch_series_cover(self.bot, url)
                await _edit_completion_v2(
                    msg,
                    series_name=series,
                    series_url=url,
                    chapter_line=ch_line,
                    main_link=result["link"],
                    provider=prov,
                    cover_url=cover,
                )
                if metrics:
                    metrics.inc("download_ok")
                    metrics.add_download_duration(time.perf_counter() - started_at)
                return

            if result and result.get("type") == "local_zip":
                final = result["link"]
                size_mb = (os.path.getsize(final) if os.path.isfile(final) else 0) / (1024 * 1024)
                state["size"] = f"{size_mb:.2f} MB"
                upload_list = []
                if destination == "Auto":
                    upload_list = [("Gofile", downloader.upload_to_gofile), ("Catbox", downloader.upload_to_catbox)]
                elif destination == "Gofile":
                    upload_list = [("Gofile", downloader.upload_to_gofile)]
                elif destination == "Catbox":
                    upload_list = [("Catbox", downloader.upload_to_catbox)]
                elif destination == "Discord":
                    if size_mb <= DISCORD_LIMIT_MB:
                        state["phase"] = "📤 Uploading to Discord"
                        state["provider"] = "Discord"
                        await msg.edit(embed=None, view=build_layout())
                        await interaction.followup.send(content=f"✅ {interaction.user.mention} جاهز!", file=discord.File(final))
                        state["phase"] = "✅ Download Completed"
                        state["color"] = C_GREEN
                        await msg.edit(embed=None, view=build_layout())
                        downloader.cleanup(final)
                        if metrics:
                            metrics.inc("download_ok")
                            metrics.add_download_duration(time.perf_counter() - started_at)
                        return
                    destination = "Auto"

                if destination != "Discord":
                    link = prov = None
                    for pname, pfn in upload_list:
                        state["phase"] = f"☁️ Uploading to {pname}..."
                        state["provider"] = pname
                        await msg.edit(embed=None, view=build_layout())
                        link = await pfn(final)
                        if link:
                            prov = pname
                            break
                    if link:
                        series_title = await get_scraped_title()
                        series, ch_line = _completion_labels(url, title, series_title=series_title)
                        cover = await _fetch_series_cover(self.bot, url)
                        await _edit_completion_v2(
                            msg,
                            series_name=series,
                            series_url=url,
                            chapter_line=ch_line,
                            main_link=link,
                            provider=prov or "",
                            cover_url=cover,
                        )
                        if metrics:
                            metrics.inc("download_ok")
                            metrics.add_download_duration(time.perf_counter() - started_at)
                    else:
                        state["phase"] = "❌ Upload Failed"
                        state["color"] = C_RED
                        await msg.edit(embed=None, view=build_layout())
                        if metrics:
                            metrics.inc("download_fail")
                downloader.cleanup(final)
            else:
                state["phase"] = "❌ فشل التحميل أو الدمج"
                state["color"] = C_RED
                await msg.edit(embed=None, view=build_layout())
                if metrics:
                    metrics.inc("download_fail")
        except CloudflareBlockedError:
            state["phase"] = "⛔ محجوب بـ Cloudflare"
            state["color"] = C_RED
            await msg.edit(embed=None, view=build_layout())
            await interaction.followup.send(embed=discord.Embed(
                title="⛔ lekmanga.net — التحميل غير متاح",
                description="المصدر محمي بـ Cloudflare Bot Management. استخدم مصدر مانجا آخر مدعوم.",
                color=C_RED,
            ), ephemeral=True)
            if metrics:
                metrics.inc("download_fail")
        except Exception as e:
            state["phase"] = f"❌ خطأ: {str(e)[:80]}"
            state["color"] = C_RED
            try:
                await msg.edit(embed=None, view=build_layout())
            except Exception:
                pass
            if metrics:
                metrics.inc("download_fail")

    async def _download_media_internal(
        self, interaction: discord.Interaction, url: str, audio_only: bool = False
    ):
        await interaction.response.defer(thinking=True)
        
        embed = discord.Embed(
            title="📥 جاري تنزيل الوسائط...",
            description=f"رابط الطلب: {url}\nالرجاء الانتظار قليلاً.",
            color=C_BLUE
        )
        msg = await interaction.followup.send(embed=embed)
        
        import shutil
        import uuid
        from services.media_downloader import download_media_async
        
        out_dir = os.path.join("temp_downloads", f"media_{uuid.uuid4().hex[:8]}")
        format_type = "audio" if audio_only else "video"
        
        try:
            res = await download_media_async(url, out_dir, format_type)
            if not res:
                embed.title = "❌ فشل التنزيل"
                embed.description = "فشل تنزيل الوسائط من الرابط المدخل. تأكد من صحة الرابط أو حاول مرة أخرى."
                embed.color = C_RED
                await msg.edit(embed=embed)
                shutil.rmtree(out_dir, ignore_errors=True)
                return
                
            file_path = res["file_path"]
            file_size = res["size"]
            file_title = res["title"]
            
            # Max upload limit for Discord is 25MB
            max_discord_size = 25 * 1024 * 1024
            
            if file_size <= max_discord_size:
                embed.title = "📤 جاري الرفع إلى Discord..."
                await msg.edit(embed=embed)
                
                file = discord.File(file_path, filename=os.path.basename(file_path))
                try:
                    await msg.delete()
                except Exception:
                    pass
                await interaction.followup.send(content=f"✅ **تم تحميل الملف بنجاح:** `{file_title}`", file=file)
            else:
                embed.title = "☁️ جاري الرفع إلى التخزين السحابي..."
                embed.description = f"حجم الملف ({file_size / 1024 / 1024:.2f} MB) يتجاوز حد ديسكورد.\nجاري الرفع الآن..."
                await msg.edit(embed=embed)
                
                downloader = getattr(self.bot, "downloader", None)
                upload_link = None
                upload_type = ""
                
                if downloader:
                    destination = await database.get_setting("default_upload_dest", "Auto")
                    
                    last_upd = 0.0
                    async def pcb(cur, tot, txt):
                        nonlocal last_upd
                        now = asyncio.get_running_loop().time()
                        if now - last_upd < 1.5 and cur < 100:
                            return
                        last_upd = now
                        try:
                            embed.description = f"حجم الملف ({file_size / 1024 / 1024:.2f} MB) يتجاوز حد ديسكورد.\n{txt} ({cur}%)"
                            await msg.edit(embed=embed)
                        except Exception:
                            pass
                    
                    # 1. محاولة Google Drive أولاً إذا كانت الوجهة المحددة هي Drive أو Auto
                    if destination in ("Drive", "Auto") and Config.GOOGLE_DRIVE_FOLDER_ID:
                        try:
                            upload_link = await downloader.upload_to_gdrive(
                                file_path, os.path.basename(file_path), progress_callback=pcb
                            )
                            if upload_link:
                                upload_type = "Google Drive"
                        except Exception as gde:
                            print(f"[MediaDownloadUpload] GDrive upload failed: {gde}")
                            
                    # 2. محاولة Catbox إذا لم يُرفع وكان الحجم مناسباً
                    if not upload_link and file_size <= 200 * 1024 * 1024:
                        try:
                            upload_link = await downloader.upload_to_catbox(file_path, progress_callback=pcb)
                            if upload_link:
                                upload_type = "Catbox"
                        except Exception as ce:
                            print(f"[MediaDownloadUpload] Catbox upload failed: {ce}")
                            
                    # 3. محاولة Gofile كبديل أخير
                    if not upload_link:
                        try:
                            upload_link = await downloader.upload_to_gofile(file_path, progress_callback=pcb)
                            if upload_link:
                                upload_type = "Gofile"
                        except Exception as ge:
                            print(f"[MediaDownloadUpload] Gofile upload failed: {ge}")
                            
                if upload_link:
                    embed.title = "✅ تم التنزيل والرفع بنجاح!"
                    embed.description = (
                        f"🎥 **العنوان**: `{file_title}`\n"
                        f"⚖️ **الحجم**: `{file_size / 1024 / 1024:.2f} MB`\n"
                        f"🔗 **رابط التحميل المباشر ({upload_type})**: [{upload_type} Download Link]({upload_link})"
                    )
                    embed.color = C_GREEN
                    await msg.edit(embed=embed)
                else:
                    embed.title = "❌ فشل الرفع السحابي"
                    embed.description = f"تم تنزيل الملف بنجاح ولكن فشل رفعه إلى السحابة. حجم الملف: `{file_size / 1024 / 1024:.2f} MB`"
                    embed.color = C_RED
                    await msg.edit(embed=embed)
                    
        except Exception as ex:
            embed.title = "⚠️ حدث خطأ غير متوقع"
            embed.description = f"التفاصيل: `{str(ex)[:200]}`"
            embed.color = C_RED
            try:
                await msg.edit(embed=embed)
            except Exception:
                pass
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(DownloadsCog(bot))
