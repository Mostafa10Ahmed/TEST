"""Discord Components v2 — ARCANE-style completion & panel helpers."""
from __future__ import annotations

import re

import discord

from download_ui import LOGO_DRIVE, LOGO_GOFILE

C_DONE = discord.Color.from_rgb(35, 165, 89)
C_RUN = discord.Color.from_rgb(88, 101, 242)
C_FAIL = discord.Color.from_rgb(242, 63, 66)
C_PANEL = discord.Color.from_rgb(255, 193, 7)


def display_series_name(url: str, fallback: str = "") -> str:
    """اسم السلسلة من الرابط (يتخطى segment الفصل إن وُجد)."""
    if url:
        url_lower = url.lower()
        if "comic.naver.com" in url_lower:
            from urllib.parse import urlparse, parse_qs
            try:
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                if "titleid" in qs:
                    return f"Naver Webtoon {qs['titleid'][0]}"
                elif "titleId" in qs:
                    return f"Naver Webtoon {qs['titleId'][0]}"
            except Exception:
                pass
            return "Naver Webtoon"

        if "?" in url:
            url = url.split("?")[0]
        if "#" in url:
            url = url.split("#")[0]
            
    parts = [p for p in (url or "").rstrip("/").split("/") if p]
    if not parts:
        return fallback or "Manga"
        
    ignored = {"status", "detail", "chapters", "list", "webtoon", "manga", "series"}
    while parts and parts[-1].lower() in ignored:
        parts.pop()
        
    if not parts:
        return fallback or "Manga"
        
    slug = parts[-1]
    if re.search(r"chapter|ch-?\d|episode|ep-?\d", slug, re.I) and len(parts) > 1:
        parent_idx = -2
        while len(parts) + parent_idx >= 0 and parts[parent_idx].lower() in ignored:
            parent_idx -= 1
        if len(parts) + parent_idx >= 0:
            slug = parts[parent_idx]
            
    name = slug.replace("-", " ").replace("_", " ").title()
    name = re.sub(r"\s+[0-9a-f]{6,}\s*$", "", name, flags=re.I).strip()
    return name or fallback or "Manga"


def chapter_label_from_title(title: str, url: str = "") -> str:
    """رقم/اسم الفصل للعرض بدون تكرار 'Chapter Chapter'."""
    t = (title or "").strip()
    if t and t.lower() not in ("manga_chapter", "chapter", "manga"):
        if re.search(r"ch[._\s-]?\d", t, re.I):
            m = re.search(r"(\d+(?:\.\d+)?)", t)
            return m.group(1) if m else t
        return t
    parts = [p for p in (url or "").rstrip("/").split("/") if p]
    for seg in reversed(parts):
        m = re.search(r"chapter[-_]?(\d+(?:\.\d+)?)", seg, re.I)
        if m:
            return m.group(1)
        m = re.search(r"(\d+(?:\.\d+)?)", seg)
        if m and "title" not in seg.lower():
            return m.group(1)
    return "—"


def format_chapter_line(
    *,
    count: int = 1,
    start: str = "",
    end: str = "",
    single: str = "",
) -> str:
    if count > 1 and start and end:
        return (
            f"**{count} Chapters: Chapter {start} → Chapter {end}** "
            f"**({count} CHPS)**"
        )
    if single and single != "—":
        return f"**Chapter {single}**"
    return ""


def add_cover_media(container: discord.ui.Container, cover_url: str | None) -> None:
    u = (cover_url or "").strip()
    if u.startswith("http"):
        container.add_item(
            discord.ui.MediaGallery(discord.MediaGalleryItem(media=u))
        )


def add_select_row(container: discord.ui.Container, select: discord.ui.Select) -> None:
    row = discord.ui.ActionRow()
    row.add_item(select)
    container.add_item(row)


def storage_dest_from_link(link: str, provider: str = "") -> tuple[str | None, str, str]:
    prov = provider or ""
    low = (link or "").lower()
    if "gofile.io" in low or "gofile" in prov.lower():
        return "Gofile", "Gofile", LOGO_GOFILE
    if "drive.google.com" in low or "drive" in prov.lower():
        return "Drive", "Google Drive", LOGO_DRIVE
    return None, prov or "Download", ""


def _storage_button_label(dest_key: str | None, multi_folder: bool) -> str:
    if multi_folder and dest_key == "Gofile":
        return "Open folder in Gofile"
    if multi_folder and dest_key == "Drive":
        return "Open folder in Google Drive"
    if dest_key in ("Drive", "Gofile"):
        return "Open chapter folder"
    return "Open download"


def add_storage_link_block(
    container: discord.ui.Container,
    *,
    link: str,
    provider: str = "",
    multi_folder: bool = False,
) -> None:
    """
    صف ARCANE: شعار + نص يسار، زر رابط يمين (Section + Button accessory).
    """
    dest_key, dest_label, logo = storage_dest_from_link(link, provider)
    if not link or not dest_key:
        return

    folder_txt = (
        f"{dest_label} — Chapter Folder"
        if multi_folder
        else f"{dest_label} — Chapter"
    )
    btn_lbl = _storage_button_label(dest_key, multi_folder)

    if logo:
        line = f"![{dest_label}]({logo}) [**{folder_txt}**]({link})"
    else:
        line = f"**[{folder_txt}]({link})**"

    container.add_item(
        discord.ui.Section(
            discord.ui.TextDisplay(line),
            accessory=discord.ui.Button(
                label=btn_lbl,
                url=link,
                style=discord.ButtonStyle.link,
            ),
        )
    )


def build_download_completed_container(
    *,
    series_name: str,
    series_url: str,
    chapter_line: str = "",
    main_link: str | None = None,
    provider: str = "",
    cover_url: str | None = None,
    multi_folder: bool = False,
    failed_line: str | None = None,
    color: discord.Color | None = None,
) -> discord.ui.Container:
    """حاوية إكمال التحميل — ترتيب مثل مرجع ARCANE."""
    container = discord.ui.Container(accent_color=color or C_DONE)

    container.add_item(discord.ui.TextDisplay("## ✅  Download completed"))

    add_cover_media(container, cover_url)

    if series_url and series_url.startswith("http"):
        container.add_item(
            discord.ui.TextDisplay(f"### [{series_name}]({series_url})")
        )
    else:
        container.add_item(discord.ui.TextDisplay(f"### {series_name}"))

    if chapter_line:
        container.add_item(discord.ui.TextDisplay(chapter_line))

    if main_link:
        container.add_item(
            discord.ui.Separator(
                visible=True,
                spacing=discord.SeparatorSpacing.small,
            )
        )
        add_storage_link_block(
            container, link=main_link, provider=provider, multi_folder=multi_folder
        )

    if failed_line:
        container.add_item(
            discord.ui.Separator(
                visible=True,
                spacing=discord.SeparatorSpacing.small,
            )
        )
        container.add_item(discord.ui.TextDisplay(failed_line))

    return container


def build_download_completed_layout(
    *,
    series_name: str,
    series_url: str,
    chapter_line: str = "",
    description: str = "",
    main_link: str | None = None,
    provider: str = "",
    cover_url: str | None = None,
    multi_folder: bool = False,
    failed_line: str | None = None,
    color: discord.Color | None = None,
) -> discord.ui.LayoutView:
    line = chapter_line or description
    layout = discord.ui.LayoutView(timeout=None)
    layout.add_item(
        build_download_completed_container(
            series_name=series_name,
            series_url=series_url,
            chapter_line=line,
            main_link=main_link,
            provider=provider,
            cover_url=cover_url,
            multi_folder=multi_folder,
            failed_line=failed_line,
            color=color,
        )
    )
    return layout


def build_progress_layout(
    *,
    title: str,
    phase: str,
    progress_bar: str,
    counter: str = "",
    provider: str = "—",
    color: discord.Color | None = None,
) -> discord.ui.LayoutView:
    layout = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=color or C_RUN)
    container.add_item(discord.ui.TextDisplay(f"## ⏳  {title}\n**{phase}**"))
    body = f"`{progress_bar}`"
    if counter:
        body += f"  ·  `{counter}`"
    container.add_item(discord.ui.TextDisplay(body))
    container.add_item(discord.ui.TextDisplay(f"-# Provider: **{provider}**"))
    _, dest_label, logo = storage_dest_from_link("", provider)
    if logo:
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(f"Uploading to **{dest_label}**"),
                accessory=discord.ui.Thumbnail(media=logo),
            )
        )
    layout.add_item(container)
    return layout


# ── Interactive Help System (v2 Components) ─────────────────────────

class InteractiveHelpView(discord.ui.LayoutView):
    def __init__(self, bot: discord.Client, user_rank: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_rank = user_rank
        self.current_tab = "user"
        self._build_sync_tab("user")

    def _build_sync_tab(self, tab_name: str):
        self.clear_items()
        self.current_tab = tab_name
        
        if tab_name == "user":
            color = discord.Color.from_rgb(56, 189, 248)  # User sky blue
            title = "👥 أوامر الأعضاء العامين (User)"
            desc = (
                "📡 `/tracker`\n"
                "-# فتح لوحة تتبع المانجا التفاعلية (Guild Trackers & Personal Trackers).\n\n"
                "📖 `/help`\n"
                "-# لوحة المساعدة والخدمات التفاعلية الموحدة.\n\n"
                "🔍 `/search`\n"
                "-# بحث عن مانجا/مانهوا وفتح لوحة التحكم الذكية."
            )
        elif tab_name == "vip":
            color = discord.Color.from_rgb(99, 102, 241)  # VIP indigo
            title = "⭐ أوامر الأعضاء المتميزين (VIP)"
            if self.user_rank >= 2:
                desc = (
                    "📖 `/manga_panel`\n"
                    "-# لوحة تحكم تفاعلية لتصفح وتحميل الفصول مباشرة.\n\n"
                    "📥 `/download`\n"
                    "-# تحميل الفصول ومقاطع الفيديو/الصوت تلقائياً بالتعرف على الرابط.\n\n"
                    "🧵 `/stitch_drive`\n"
                    "-# دمج فصول المانجا المرفوعة على Google Drive.\n\n"
                    "✂️ `/extract`\n"
                    "-# استخراج نصوص صفحات الفصول والترجمة الذكية."
                )
            else:
                desc = (
                    "🔒 **هذا القسم مخصص للأعضاء VIP فقط.**\n"
                    "-# تواصل مع إدارة السيرفر للترقية والحصول على صلاحيات التحميل والدمج الذكي."
                )
        elif tab_name == "admin":
            color = discord.Color.from_rgb(255, 184, 0)  # Admin gold
            title = "🛡️ أوامر الإدارة والتحكم (Admin Dashboard)"
            if self.user_rank >= 3:
                desc = (
                    "📡 `/tracker`\n"
                    "-# لوحة التحكم الموحدة للتتبع (إضافة، تعديل، إيقاف/استئناف، فحص، اشتراك، حذف، واستيراد/تصدير JSON).\n\n"
                    "👑 `/admin`\n"
                    "-# لوحة التحكم الإدارية الشاملة (جدول أداء المزودات Health Matrix، قياس أداء Worker، وزر الفحص والإصلاح System Heal)."
                )
            else:
                desc = (
                    "🔒 **هذا القسم مخصص للأدمن والمطورين فقط.**\n"
                    "-# لا تملك صلاحيات كافية للوصول للوحة التحكم الإدارية."
                )
        else:
            return


        container = discord.ui.Container(accent_color=color)
        container.add_item(discord.ui.TextDisplay(f"# {title}\n\n{desc}"))
        self._add_buttons(container)
        self.add_item(container)

    def _add_buttons(self, container):
        btn_user = discord.ui.Button(
            label="أوامر عامة",
            style=discord.ButtonStyle.primary if self.current_tab == "user" else discord.ButtonStyle.secondary,
            emoji="👥"
        )
        async def _cb_user(interaction):
            await interaction.response.defer()
            self._build_sync_tab("user")
            await interaction.followup.edit_message(message_id=interaction.message.id, view=self)
        btn_user.callback = _cb_user

        vip_emoji = "⭐" if self.user_rank >= 2 else "🔒"
        btn_vip = discord.ui.Button(
            label="أوامر VIP",
            style=discord.ButtonStyle.success if self.current_tab == "vip" else discord.ButtonStyle.secondary,
            emoji=vip_emoji
        )
        async def _cb_vip(interaction):
            await interaction.response.defer()
            self._build_sync_tab("vip")
            await interaction.followup.edit_message(message_id=interaction.message.id, view=self)
        btn_vip.callback = _cb_vip

        admin_emoji = "🛡️" if self.user_rank >= 3 else "🔒"
        btn_admin = discord.ui.Button(
            label="الإدارة",
            style=discord.ButtonStyle.danger if self.current_tab == "admin" else discord.ButtonStyle.secondary,
            emoji=admin_emoji
        )
        async def _cb_admin(interaction):
            await interaction.response.defer()
            self._build_sync_tab("admin")
            await interaction.followup.edit_message(message_id=interaction.message.id, view=self)
        btn_admin.callback = _cb_admin

        btn_status = discord.ui.Button(
            label="📊 حالة البوت والخدمات",
            style=discord.ButtonStyle.primary if self.current_tab == "status" else discord.ButtonStyle.secondary,
        )
        async def _cb_status(interaction):
            await interaction.response.defer()
            await self._build_status_tab()
            await interaction.followup.edit_message(message_id=interaction.message.id, view=self)
        btn_status.callback = _cb_status

        btn_provs = discord.ui.Button(
            label="🌐 المواقع المدعومة",
            style=discord.ButtonStyle.primary if self.current_tab == "providers" else discord.ButtonStyle.secondary,
        )
        async def _cb_provs(interaction):
            await interaction.response.defer()
            await self._build_provs_tab()
            await interaction.followup.edit_message(message_id=interaction.message.id, view=self)
        btn_provs.callback = _cb_provs

        container.add_item(discord.ui.ActionRow(btn_user, btn_vip, btn_admin))
        container.add_item(discord.ui.ActionRow(btn_status, btn_provs))

    async def _build_status_tab(self):
        import datetime
        import database
        from bot_config import Config
        
        self.clear_items()
        self.current_tab = "status"
        
        color = discord.Color.from_rgb(87, 242, 135)  # Green status
        container = discord.ui.Container(accent_color=color)
        container.add_item(discord.ui.TextDisplay("# 📊 حالة النظام والخدمات"))
        
        start_time = getattr(self.bot, "start_time", datetime.datetime.now(datetime.timezone.utc))
        uptime = str(datetime.datetime.now(datetime.timezone.utc) - start_time).split(".")[0]
        trackers = await database.get_tracker_count()
        users = await database.get_user_count()
        remote_down = getattr(self.bot, "remote_down", None)
        metrics = getattr(self.bot, "metrics", None)
        m = metrics.snapshot() if metrics else {}
        
        hf_health = None
        if remote_down and remote_down.is_enabled:
            try:
                hf_health = await remote_down.get_worker_health()
            except Exception:
                pass

        bot_status = "🟢 يعمل"
        gf_st = "🟢 مضبوط" if Config.GOFILE_TOKEN else "⚪ غير مضبوط"
        worker_status = "🟢 مفعّل" if remote_down and remote_down.is_enabled else "⚪ غير مفعّل"
        if hf_health and "error" in hf_health:
            worker_status = f"🟠 مشكلة ({hf_health['error'][:24]})"

        queue_info = ""
        cap_info = ""
        if hf_health and "error" not in hf_health:
            queue_info = f"Running `{hf_health.get('running_jobs', 0)}` | Queued `{hf_health.get('queued_jobs', 0)}`"
            cap_info = f"`{hf_health.get('max_concurrent_jobs', 0)}` workers"

        status_grid = (
            f"🤖 **البوت**: {bot_status}  ·  ☁️ **Gofile**: {gf_st}\n"
            f"🖥️ **Worker**: {worker_status}\n"
        )
        if queue_info or cap_info:
            status_grid += f"⚙️ **السعة**: `{cap_info or '—'}`  ·  📥 **الطابور**: `{queue_info or '—'}`\n"
            
        container.add_item(discord.ui.TextDisplay(status_grid))
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        metrics_text = (
            f"⏱️ **وقت التشغيل**: `{uptime}`\n"
            f"📡 **الرادار**: `{trackers}` متتبّع  ·  👥 **الأعضاء**: `{users}` مستخدم\n"
        )
        container.add_item(discord.ui.TextDisplay(metrics_text))
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        avg_dl = m.get("download_avg_sec", 0.0)
        perf_text = (
            f"🔎 **البحث**: `✅ {m.get('search_ok', 0)} | ❌ {m.get('search_fail', 0)}`\n"
            f"📥 **التحميل**: `✅ {m.get('download_ok', 0)} | ❌ {m.get('download_fail', 0)}`\n"
            f"🧵 **الدمج**: `✅ {m.get('stitch_ok', 0)} | ❌ {m.get('stitch_fail', 0)}`\n"
            f"⚡ **متوسط وقت التحميل**: `{avg_dl:.1f}s`"
        )
        container.add_item(discord.ui.TextDisplay(perf_text))
        
        self._add_buttons(container)
        self.add_item(container)

    async def _build_provs_tab(self):
        import database
        
        self.clear_items()
        self.current_tab = "providers"
        
        color = discord.Color.from_rgb(32, 178, 170)  # Teal
        container = discord.ui.Container(accent_color=color)
        container.add_item(discord.ui.TextDisplay("# 🌐 المواقع المدعومة"))
        
        custom_sites = await database.get_custom_sites()
        
        providers_text = (
            "📚 **مواقع بـ API مخصص**:\n"
            "- MangaDex • Comick • MangaFire • MangaPlus • Bato\n"
            "- Webtoons • Naver • AsuraScans • WeebCentral • TCBScans\n"
            "- VortexScans • MangaPill • Manganato • **Shinigami**\n\n"
            "🇸🇦 **مواقع عربية**:\n"
            "- **LekManga** • Mangalek • 3asq • Manga-ar • Gmanga • Arabsama\n\n"
            "⚡ **مواقع Madara/WordPress (150+ موقع)**:\n"
            "- Toonily • Zinmanga • Flamescans • Reaperscans • Leviatanscans\n\n"
            "🤖 **مستخرج عام (Generic)**:\n"
            "- يدعم سحب الصور من أي موقع مانجا آخر غير مدرج تلقائياً."
        )
        container.add_item(discord.ui.TextDisplay(providers_text))
        if custom_sites:
            custom_txt = "  ".join(f"`{d[0]}`" for d in custom_sites[:15])
            container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
            container.add_item(discord.ui.TextDisplay(f"➕ **مواقع مضافة يدوياً ({len(custom_sites)})**:\n{custom_txt[:500]}"))
            
        self._add_buttons(container)
        self.add_item(container)


# ── Premium Status Dashboard Layout ──────────────────────────────────

def build_status_layout(
    *,
    bot_status: str,
    gofile_status: str,
    worker_status: str,
    queue_info: str,
    cap_info: str,
    trackers_count: int,
    uptime: str,
    users_count: int,
    rank_label: str,
    search_ok: int,
    search_fail: int,
    download_ok: int,
    download_fail: int,
    stitch_ok: int,
    stitch_fail: int,
    avg_dl_sec: float,
) -> discord.ui.LayoutView:
    layout = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=C_RUN)
    
    container.add_item(discord.ui.TextDisplay("# 📊 حالة النظام والخدمات"))
    
    status_grid = (
        f"🤖 **البوت**: {bot_status}  ·  ☁️ **Gofile**: {gofile_status}\n"
        f"🖥️ **Worker**: {worker_status}\n"
    )
    if queue_info or cap_info:
        status_grid += f"⚙️ **السعة**: `{cap_info or '—'}`  ·  📥 **الطابور**: `{queue_info or '—'}`\n"
        
    container.add_item(discord.ui.TextDisplay(status_grid))
    container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
    
    metrics_text = (
        f"⏱️ **وقت التشغيل**: `{uptime}`\n"
        f"📡 **الرادار**: `{trackers_count}` متتبّع  ·  👥 **الأعضاء**: `{users_count}` مستخدم\n"
        f"🎖️ **رتبتك**: **{rank_label}**\n"
    )
    container.add_item(discord.ui.TextDisplay(metrics_text))
    container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
    
    perf_text = (
        f"🔎 **البحث**: `✅ {search_ok} | ❌ {search_fail}`\n"
        f"📥 **التحميل**: `✅ {download_ok} | ❌ {download_fail}`\n"
        f"🧵 **الدمج**: `✅ {stitch_ok} | ❌ {stitch_fail}`\n"
        f"⚡ **متوسط وقت التحميل**: `{avg_dl_sec:.1f}s`"
    )
    container.add_item(discord.ui.TextDisplay(perf_text))
    
    layout.add_item(container)
    return layout

