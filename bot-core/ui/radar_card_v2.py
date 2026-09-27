from __future__ import annotations

import asyncio
import contextlib
import datetime
import re

import discord

import database
from ui.components_v2 import add_storage_link_block, add_cover_media
from user_system import check_rank


C_BLUE   = discord.Color.from_rgb(88, 101, 242)
C_GOLD   = discord.Color.from_rgb(255, 184, 0)
C_GREEN  = discord.Color.from_rgb(35, 165, 89)
C_RED    = discord.Color.from_rgb(242, 63, 66)
C_PURPLE = discord.Color.from_rgb(124, 92, 252)
LOGO_BOT = "https://cdn.discordapp.com/embed/avatars/0.png"


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


def _series_name(url: str) -> str:
    if "?" in url:
        url = url.split("?")[0]
    if "#" in url:
        url = url.split("#")[0]
    parts = [p for p in url.rstrip("/").split("/") if p]
    ignored = {"status", "detail", "chapters", "list", "webtoon", "manga", "series"}
    while parts and parts[-1].lower() in ignored:
        parts.pop()
    name = parts[-1].replace("-", " ").replace("_", " ").title() if parts else "Series"
    name = re.sub(r'\s+[0-9a-f]{6,}\s*$', '', name, flags=re.I).strip()
    return name or "Series"


def _lbl(num: float) -> str:
    try:
        return str(int(num)) if float(num).is_integer() else str(num)
    except Exception:
        return str(num)


class TrackerCardButton(discord.ui.Button):
    def __init__(self, label: str, style: discord.ButtonStyle, custom_id: str, callback_func):
        super().__init__(label=label, style=style, custom_id=custom_id)
        self.callback_func = callback_func

    async def callback(self, interaction: discord.Interaction):
        await self.callback_func(interaction)


def build_tracker_card_layout(
    bot: discord.Client,
    series_url: str,
    chapter_num: float,
    chapter_url: str,
    locked: bool,
    *,
    note: str | None = None,
    paused: bool = False,
    view: TrackerCardV2View | None = None,
    cover_url: str | None = None,
    series_title: str | None = None,
    use_layout_api: bool = True,
    dl_link: str | None = None,
) -> discord.ui.View:
    series_name = series_title or _series_name(series_url)
    domain = _domain(series_url)

    # Check if domain has auth cookies configured
    provider_mgr = getattr(bot, "provider_mgr", None)
    has_auth = provider_mgr.has_auth_cookies(series_url) if provider_mgr else False

    if paused:
        color = C_PURPLE
        status_icon = "⏸️"
        status_txt = "متوقف"
    elif locked:
        if has_auth:
            color = C_GREEN
            status_icon = "🔓"
            status_txt = "مقفل (كوكيز متوفرة — جاهز للتحميل)"
        else:
            color = C_RED
            status_icon = "🔒"
            status_txt = "مقفل (يتطلب كوكيز — استخدم /auth_panel)"
    else:
        color = C_GOLD
        status_icon = "🟢"
        status_txt = "متاح"

    # Use the provided view or construct a new LayoutView
    v = view if view is not None else TrackerCardV2View()
    v.clear_items()
    
    # Premium discord.Embed layout
    title_text = f"📖 {series_name} — Chapter {_lbl(chapter_num)}"
    embed = discord.Embed(
        title=title_text,
        url=chapter_url,
        color=color
    )
    
    desc_lines = [
        "**✨ تحديث جديد متوفر الآن!**\n",
        f"🌐 **المصدر**: `{domain}`",
        f"📖 **الفصل**: [Ch. {_lbl(chapter_num)}]({chapter_url})",
        f"📚 **السلسلة**: [{series_name}]({series_url})",
        f"⚡ **الحالة**: {status_icon} {status_txt}",
    ]
    if note:
        desc_lines.append(f"ℹ️ **ملاحظة**: {note}")
        
    embed.description = "\n".join(desc_lines)
    
    if cover_url:
        embed.set_image(url=cover_url)
        embed.set_thumbnail(url=LOGO_BOT)
        
    embed.set_footer(text=f"Scan Group: {domain} • Custom Alert System")
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    
    v.embed = embed

    if use_layout_api:
        # Fallback/Backward compatibility for clients that only use discord.ui.Container Layout:
        container = discord.ui.Container(accent_color=color)
        if cover_url:
            add_cover_media(container, cover_url)
        container.add_item(discord.ui.TextDisplay(f"# {title_text}"))
        container.add_item(discord.ui.TextDisplay(
            f"**✨ تحديث جديد متوفر الآن!**\n\n"
            f"🌐 **المصدر**: `{domain}`\n"
            f"📖 **الفصل**: [Ch. {_lbl(chapter_num)}]({chapter_url})\n"
            f"📚 **السلسلة**: [{series_name}]({series_url})\n"
            f"⚡ **الحالة**: {status_icon} {status_txt}"
        ))
        if note:
            container.add_item(discord.ui.TextDisplay(f"ℹ️ **تحديث**: {note}"))

        # Re-add buttons
        row1 = discord.ui.ActionRow(
            TrackerCardButton("🔄 تحديث", discord.ButtonStyle.secondary, "radar_v2_refresh", v.refresh_btn),
            TrackerCardButton("⬇️ تحميل", discord.ButtonStyle.success, "radar_v2_download", v.download_btn),
        )
        row2 = discord.ui.ActionRow(
            TrackerCardButton("📚 تصفح الفصول", discord.ButtonStyle.primary, "radar_v2_choose", v.choose_btn),
            TrackerCardButton("⏸️ إيقاف/تشغيل", discord.ButtonStyle.secondary, "radar_v2_pause", v.pause_btn),
            TrackerCardButton("⏰ تذكير", discord.ButtonStyle.secondary, "radar_v2_remind", v.remind_btn),
            TrackerCardButton("🔔 اشتراك", discord.ButtonStyle.primary, "radar_v2_subscribe", v.subscribe_btn),
        )
        container.add_item(row1)
        container.add_item(row2)
        v.add_item(container)
    else:
        # Standard View layout (without Container wrapper) for general channel messages
        row1 = discord.ui.ActionRow()
        if dl_link:
            row1.add_item(discord.ui.Button(label="Google Drive", url=dl_link, style=discord.ButtonStyle.link))
        row1.add_item(TrackerCardButton("🔄 تحديث", discord.ButtonStyle.secondary, "radar_v2_refresh", v.refresh_btn))
        row1.add_item(TrackerCardButton("⬇️ تحميل", discord.ButtonStyle.success, "radar_v2_download", v.download_btn))
        
        row2 = discord.ui.ActionRow(
            TrackerCardButton("📚 تصفح الفصول", discord.ButtonStyle.primary, "radar_v2_choose", v.choose_btn),
            TrackerCardButton("⏸️ إيقاف/تشغيل", discord.ButtonStyle.secondary, "radar_v2_pause", v.pause_btn),
            TrackerCardButton("⏰ تذكير", discord.ButtonStyle.secondary, "radar_v2_remind", v.remind_btn),
            TrackerCardButton("🔔 اشتراك", discord.ButtonStyle.primary, "radar_v2_subscribe", v.subscribe_btn),
        )
        v.add_item(row1)
        v.add_item(row2)

    return v


def build_tracker_batch_card_layout(
    bot: discord.Client,
    series_url: str,
    chapters: list[dict],
    *,
    note: str | None = None,
    paused: bool = False,
    view: TrackerCardV2View | None = None,
    cover_url: str | None = None,
    series_title: str | None = None,
    use_layout_api: bool = True,
    dl_link: str | None = None,
) -> discord.ui.View:
    series_name = series_title or _series_name(series_url)
    domain = _domain(series_url)

    color = C_PURPLE if paused else C_GOLD
    
    v = view if view is not None else TrackerCardV2View()
    v.clear_items()
    
    # Premium discord.Embed layout
    title_text = f"📚 {series_name} — دفعة فصول جديدة"
    embed = discord.Embed(
        title=title_text,
        url=series_url,
        color=color
    )
    
    lines = []
    for item in chapters[:15]:
        status_icon = "🔒" if item.get("locked") else "🟢"
        ch_num = item["num"]
        ch_url = item.get("url") or item.get("chapter_url") or series_url
        lines.append(f"• [Ch. {_lbl(ch_num)}]({ch_url}) ({status_icon})")
    if len(chapters) > 15:
        lines.append(f"• ...وغيرها {len(chapters) - 15} فصول إضافية")
        
    desc_lines = [
        f"**✨ تم تتبع دفعة فصول جديدة من المصدر `{domain}`!**\n",
        "\n".join(lines),
        f"\n📚 **السلسلة**: [{series_name}]({series_url})"
    ]
    if note:
        desc_lines.append(f"ℹ️ **تحديث**: {note}")
        
    embed.description = "\n".join(desc_lines)
    
    if cover_url:
        embed.set_image(url=cover_url)
        embed.set_thumbnail(url=LOGO_BOT)
        
    embed.set_footer(text=f"Scan Group: {domain} • Custom Alert System")
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    
    v.embed = embed

    if use_layout_api:
        container = discord.ui.Container(accent_color=color)
        if cover_url:
            add_cover_media(container, cover_url)
        container.add_item(discord.ui.TextDisplay(f"# {title_text}"))
        
        container.add_item(discord.ui.TextDisplay(
            f"**✨ تم تتبع دفعة فصول جديدة من المصدر `{domain}`!**\n\n" + 
            "\n".join(lines) + "\n\n"
            f"📚 **السلسلة**: [{series_name}]({series_url})"
        ))
        if note:
            container.add_item(discord.ui.TextDisplay(f"ℹ️ **تحديث**: {note}"))

        # Re-add buttons
        row1 = discord.ui.ActionRow(
            TrackerCardButton("🔄 تحديث", discord.ButtonStyle.secondary, "radar_v2_refresh", v.refresh_btn),
            TrackerCardButton("⬇️ تحميل", discord.ButtonStyle.success, "radar_v2_download", v.download_btn),
        )
        row2 = discord.ui.ActionRow(
            TrackerCardButton("📚 تصفح الفصول", discord.ButtonStyle.primary, "radar_v2_choose", v.choose_btn),
            TrackerCardButton("⏸️ إيقاف/تشغيل", discord.ButtonStyle.secondary, "radar_v2_pause", v.pause_btn),
            TrackerCardButton("⏰ تذكير", discord.ButtonStyle.secondary, "radar_v2_remind", v.remind_btn),
            TrackerCardButton("🔔 اشتراك", discord.ButtonStyle.primary, "radar_v2_subscribe", v.subscribe_btn),
        )
        container.add_item(row1)
        container.add_item(row2)
        v.add_item(container)
    else:
        # Standard View layout (without Container wrapper) for general channel messages
        row1 = discord.ui.ActionRow()
        if dl_link:
            row1.add_item(discord.ui.Button(label="Google Drive", url=dl_link, style=discord.ButtonStyle.link))
        row1.add_item(TrackerCardButton("🔄 تحديث", discord.ButtonStyle.secondary, "radar_v2_refresh", v.refresh_btn))
        row1.add_item(TrackerCardButton("⬇️ تحميل", discord.ButtonStyle.success, "radar_v2_download", v.download_btn))
        
        row2 = discord.ui.ActionRow(
            TrackerCardButton("📚 تصفح الفصول", discord.ButtonStyle.primary, "radar_v2_choose", v.choose_btn),
            TrackerCardButton("⏸️ إيقاف/تشغيل", discord.ButtonStyle.secondary, "radar_v2_pause", v.pause_btn),
            TrackerCardButton("⏰ تذكير", discord.ButtonStyle.secondary, "radar_v2_remind", v.remind_btn),
            TrackerCardButton("🔔 اشتراك", discord.ButtonStyle.primary, "radar_v2_subscribe", v.subscribe_btn),
        )
        v.add_item(row1)
        v.add_item(row2)

    return v


# ─────────────────────────────────────────────────────────
#  Reminder Modal
# ─────────────────────────────────────────────────────────

class ReminderModal(discord.ui.Modal, title="⏰ تذكير"):
    duration = discord.ui.TextInput(
        label="بعد كم؟ (مثال: 5h15m أو 5:15 أو 45m)",
        placeholder="5h15m",
        required=True,
        max_length=20,
    )

    def __init__(self, message_id: int):
        super().__init__()
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        secs = parse_duration_to_seconds(str(self.duration.value))
        if not secs or secs < 60:
            await interaction.response.send_message("❌ مدة غير صالحة (أقل شيء دقيقة).", ephemeral=True)
            return

        card = await database.get_tracker_card(self.message_id)
        if not card:
            await interaction.response.send_message("❌ هذه الرسالة قديمة/غير مدعومة.", ephemeral=True)
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        notify_at = now + datetime.timedelta(seconds=secs)
        await database.add_radar_reminder(
            message_id=self.message_id,
            tracker_id=int(card["tracker_id"]),
            guild_id=int(card["guild_id"]),
            channel_id=int(card["channel_id"]),
            user_id=int(interaction.user.id),
            notify_at_iso=notify_at.isoformat(),
        )
        h = secs // 3600
        m = (secs % 3600) // 60
        time_str = f"{h}h {m}m" if h else f"{m}m"
        await interaction.response.send_message(
            f"✅ تم ضبط التذكير بعد **{time_str}**.",
            ephemeral=True,
        )


def parse_duration_to_seconds(text: str) -> int | None:
    raw = (text or "").strip().lower().replace(" ", "")
    if not raw:
        return None

    m = re.fullmatch(r"(\d+):(\d+)", raw)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60

    m = re.fullmatch(r"(\d+)h(?:(\d+)m)?", raw)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2) or 0) * 60

    m = re.fullmatch(r"(\d+)m", raw)
    if m:
        return int(m.group(1)) * 60

    m = re.fullmatch(r"(\d+)s", raw)
    if m:
        return int(m.group(1))

    return None


# ─────────────────────────────────────────────────────────
#  Chapter picker (Browse)
# ─────────────────────────────────────────────────────────

class RadarChapterSelect(discord.ui.Select):
    def __init__(self, chapters_page: list[tuple[float, dict]]):
        self._items = chapters_page
        options: list[discord.SelectOption] = []
        for num, info in chapters_page[:25]:
            locked = bool(info.get("locked"))
            prefix = "🔒" if locked else "🟢"
            options.append(discord.SelectOption(
                label=f"{prefix} Ch. {_lbl(num)}",
                value=str(num),
                description="مقفل — انتظر أو استخدم Premium" if locked else "متاح للتحميل",
            ))
        super().__init__(placeholder="اختر فصلاً للتحميل…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        picked_num = float(self.values[0])
        info = next((i for n, i in self._items if float(n) == picked_num), None)
        if not info:
            await interaction.response.send_message("❌ الفصل غير موجود.", ephemeral=True)
            return
        ch_url = info.get("url") or ""
        locked = bool(info.get("locked"))
        icon = "🔒" if locked else "🟢"
        await interaction.response.send_message(
            f"{icon} **الفصل `{_lbl(picked_num)}`**\n"
            f"{'⚠️ هذا الفصل مقفل حالياً.' if locked else '✅ متاح للتحميل.'}\n"
            + (f"🔗 {ch_url}" if ch_url else ""),
            ephemeral=True,
        )


class RadarChapterPickerView(discord.ui.View):
    def __init__(self, chapters: list[tuple[float, dict]], page: int = 0, per_page: int = 20):
        super().__init__(timeout=600)
        self.chapters = chapters
        self.page = page
        self.per_page = per_page
        self.total_pages = max(1, (len(chapters) + per_page - 1) // per_page)
        self._rebuild()

    def _page_items(self) -> list[tuple[float, dict]]:
        start = self.page * self.per_page
        return self.chapters[start:start + self.per_page]

    def _rebuild(self) -> None:
        self.clear_items()
        items = self._page_items()
        if items:
            self.add_item(RadarChapterSelect(items))
        self.add_item(self.PrevButton(self))
        self.add_item(self.NextButton(self))

    def build_embed(self) -> discord.Embed:
        em = discord.Embed(
            title="📚 اختيار فصل",
            color=C_BLUE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        items = self._page_items()
        if not items:
            em.description = "لا توجد فصول."
        else:
            lines = []
            for num, info in items:
                prefix = "🔒" if info.get("locked") else "🟢"
                lines.append(f"{prefix} **Ch. {_lbl(num)}**")
            em.description = "\n".join(lines)
        em.set_footer(text=f"صفحة {self.page + 1} / {self.total_pages}")
        return em

    class PrevButton(discord.ui.Button):
        def __init__(self, parent: "RadarChapterPickerView"):
            super().__init__(label="◀ السابق", style=discord.ButtonStyle.secondary)
            self.parent = parent
            self.disabled = parent.page <= 0

        async def callback(self, interaction: discord.Interaction) -> None:
            self.parent.page = max(0, self.parent.page - 1)
            self.parent._rebuild()
            await interaction.response.edit_message(embed=self.parent.build_embed(), view=self.parent)

    class NextButton(discord.ui.Button):
        def __init__(self, parent: "RadarChapterPickerView"):
            super().__init__(label="التالي ▶", style=discord.ButtonStyle.secondary)
            self.parent = parent
            self.disabled = parent.page >= parent.total_pages - 1

        async def callback(self, interaction: discord.Interaction) -> None:
            self.parent.page = min(self.parent.total_pages - 1, self.parent.page + 1)
            self.parent._rebuild()
            await interaction.response.edit_message(embed=self.parent.build_embed(), view=self.parent)


# ─────────────────────────────────────────────────────────
#  Main Tracker Card View
# ─────────────────────────────────────────────────────────

class TrackerCardV2View(discord.ui.LayoutView):
    """
    Persistent LayoutView مرتبط بـ message_id لكل بطاقة تتبع.
    الأزرار في صفين داخل Container.
    """

    def __init__(self):
        super().__init__(timeout=None)
        self._busy: set[int] = set()
        self.embed: discord.Embed | None = None
        
        # Register buttons for persistence
        container = discord.ui.Container()
        row1 = discord.ui.ActionRow(
            TrackerCardButton("🔄 تحديث", discord.ButtonStyle.secondary, "radar_v2_refresh", self.refresh_btn),
            TrackerCardButton("⬇️ تحميل", discord.ButtonStyle.success, "radar_v2_download", self.download_btn),
        )
        row2 = discord.ui.ActionRow(
            TrackerCardButton("📚 تصفح الفصول", discord.ButtonStyle.primary, "radar_v2_choose", self.choose_btn),
            TrackerCardButton("⏸️ إيقاف/تشغيل", discord.ButtonStyle.secondary, "radar_v2_pause", self.pause_btn),
            TrackerCardButton("⏰ تذكير", discord.ButtonStyle.secondary, "radar_v2_remind", self.remind_btn),
            TrackerCardButton("🔔 اشتراك", discord.ButtonStyle.primary, "radar_v2_subscribe", self.subscribe_btn),
        )
        container.add_item(row1)
        container.add_item(row2)
        self.add_item(container)

    async def _load_card(self, interaction: discord.Interaction) -> dict | None:
        if not interaction.message:
            return None
        card = await database.get_tracker_card(interaction.message.id)
        if card:
            return card

        # Fallback: Parse card details from message if DB record is missing
        print(f"[Radar Card Fallback] Card {interaction.message.id} not found in DB. Executing parsing fallback...")
        
        # 1. Parse Series Title from message
        series_title = None
        
        # Check embeds
        for emb in interaction.message.embeds:
            if emb.title:
                m = re.search(r"(?:New Chapter Release|New Chapters Release)\s*—\s*(.+)", emb.title, re.I)
                if m:
                    series_title = m.group(1).strip()
                    break
                    
        # Check content
        if not series_title and interaction.message.content:
            m = re.search(r"(?:New Chapter Release|New Chapters Release)\s*—\s*(.+)", interaction.message.content, re.I)
            if m:
                series_title = m.group(1).strip()

        if not series_title:
            print("[Radar Card Fallback] Failed to parse series title from message.")
            return None

        # 2. Search database by series title or similar title
        db = await database._get_db()
        tracker_row = None
        # Try exact title match
        async with db.execute("SELECT * FROM trackers WHERE title=?", (series_title,)) as cursor:
            tracker_row = await cursor.fetchone()
        
        # Try partial title match if exact not found
        if not tracker_row:
            async with db.execute("SELECT * FROM trackers WHERE title LIKE ?", (f"%{series_title}%",)) as cursor:
                tracker_row = await cursor.fetchone()
                
        # Try domain matching from any link buttons
        if not tracker_row:
            for row in interaction.message.components:
                for child in row.children:
                    # If it's a link button
                    if hasattr(child, "url") and child.url:
                        domain = _domain(child.url)
                        if domain:
                            async with db.execute("SELECT * FROM trackers WHERE url LIKE ?", (f"%{domain}%",)) as cursor:
                                rows = await cursor.fetchall()
                                if len(rows) == 1:
                                    tracker_row = rows[0]
                                    break

        if not tracker_row:
            print(f"[Radar Card Fallback] No matching tracker found in DB for title '{series_title}'.")
            return None

        # Map tracker row
        tid, gid, cid, url, last_ch, custom_msg, iv_hours, last_checked, dl_en, title, paused, mention_str = tracker_row[:12]
        
        # 3. Parse Chapter Number
        chapter_num = last_ch
        # Check embeds
        for emb in interaction.message.embeds:
            for field in emb.fields:
                if "chapter" in field.name.lower() or "الفصل" in field.name:
                    m = re.search(r"(\d+(?:\.\d+)?)", field.value)
                    if m:
                        chapter_num = float(m.group(1))
                        break
            if emb.description:
                m = re.search(r"(?:Ch\.|Chapter|الفصل)\s*(\d+(?:\.\d+)?)", emb.description, re.I)
                if m:
                    chapter_num = float(m.group(1))

        # Check content
        if interaction.message.content:
            m = re.search(r"(?:Ch\.|Chapter|الفصل)\s*(\d+(?:\.\d+)?)", interaction.message.content, re.I)
            if m:
                chapter_num = float(m.group(1))

        # 4. Fetch/Reconstruct Chapter URL
        chapter_url = url
        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        if provider_mgr:
            try:
                rich = await provider_mgr.get_chapters_with_lock_info(url)
                if rich and chapter_num in rich:
                    info = rich[chapter_num]
                    chapter_url = info.get("url") if isinstance(info, dict) else str(info)
            except Exception as e:
                print(f"[Radar Card Fallback] Error fetching chapter info from provider: {e}")

        # Reconstruct the card dict
        reconstructed = {
            "message_id": interaction.message.id,
            "tracker_id": tid,
            "guild_id": gid,
            "channel_id": cid,
            "url": url,
            "chapter_num": chapter_num,
            "chapter_url": chapter_url,
            "locked": False,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "batch_data": None
        }
        
        # Save back to database
        await database.upsert_tracker_card(
            message_id=int(reconstructed["message_id"]),
            tracker_id=int(reconstructed["tracker_id"]),
            guild_id=int(reconstructed["guild_id"]),
            channel_id=int(reconstructed["channel_id"]),
            url=str(reconstructed["url"]),
            chapter_num=float(reconstructed["chapter_num"]),
            chapter_url=str(reconstructed["chapter_url"]),
            locked=0,
        )
        
        print(f"[Radar Card Fallback] Reconstructed card for {title} Ch.{chapter_num} successfully.")
        return reconstructed

    async def _require_vip(self, interaction: discord.Interaction) -> bool:
        return await check_rank(interaction, 2)

    # ── Button callbacks ───────────────────────────────────

    async def refresh_btn(self, interaction: discord.Interaction) -> None:
        if not await self._require_vip(interaction):
            return
        card = await self._load_card(interaction)
        if not card:
            await interaction.response.send_message("❌ هذه الرسالة غير مدعومة.", ephemeral=True)
            return

        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        if not provider_mgr:
            await interaction.response.send_message("❌ Provider manager غير متوفر.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            rich = await provider_mgr.get_chapters_with_lock_info(card["url"])
            if not rich:
                await interaction.followup.send("❌ ما قدرت أجيب الفصول.", ephemeral=True)
                return
            latest = max(rich.keys())
            info = rich.get(latest) or {}
            ch_url = info.get("url") if isinstance(info, dict) else str(info)
            locked = bool(info.get("locked")) if isinstance(info, dict) else False

            # Check if tracker is paused
            tracker = await database.get_tracker(int(card["tracker_id"]), int(card["guild_id"]))
            paused = bool(int(tracker[9] or 0)) if tracker and len(tracker) > 9 else False
            series_title = tracker[10] if tracker and len(tracker) > 10 else None

            # Fetch cover url dynamically
            cover_url = None
            try:
                cover_url = await asyncio.wait_for(provider_mgr.get_series_cover(card["url"]), timeout=5)
            except Exception:
                pass

            layout = build_tracker_card_layout(
                interaction.client, card["url"], float(latest), ch_url, locked,
                note="✅ تم التحديث الآن", paused=paused, view=self, cover_url=cover_url,
                series_title=series_title
            )
            await interaction.message.edit(embed=None, view=layout)
            await database.upsert_tracker_card(
                message_id=int(card["message_id"]),
                tracker_id=int(card["tracker_id"]),
                guild_id=int(card["guild_id"]),
                channel_id=int(card["channel_id"]),
                url=str(card["url"]),
                chapter_num=float(latest),
                chapter_url=str(ch_url),
                locked=1 if locked else 0,
            )
            await interaction.followup.send("✅ تم تحديث البطاقة.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Refresh فشل: `{str(e)[:220]}`", ephemeral=True)

    async def download_btn(self, interaction: discord.Interaction) -> None:
        if not await self._require_vip(interaction):
            return
        card = await self._load_card(interaction)
        if not card:
            await interaction.response.send_message("❌ هذه الرسالة غير مدعومة.", ephemeral=True)
            return

        if card.get("batch_data"):
            import json
            try:
                batch_list = json.loads(card["batch_data"])
                chapters = [(float(c["num"]), {"url": c["url"], "locked": c.get("locked", False)}) for c in batch_list]
                view = RadarChapterPickerView(chapters, page=0, per_page=20)
                await interaction.response.send_message(
                    content="📚 اختر فصلاً من الدفعة لتحميله:",
                    embed=view.build_embed(),
                    view=view,
                    ephemeral=True
                )
                return
            except Exception as e:
                print(f"[Radar Card v2] Batch load error: {e}")
            
        if interaction.message.id in self._busy:
            await interaction.response.send_message("⏳ فيه تحميل شغال بالفعل لهذه البطاقة.", ephemeral=True)
            return

        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        remote_down = getattr(interaction.client, "remote_down", None)
        downloader = getattr(interaction.client, "downloader", None)
        if not downloader:
            await interaction.response.send_message("❌ Downloader غير متوفر.", ephemeral=True)
            return

        is_locked = bool(card.get("locked"))
        has_auth = provider_mgr.has_auth_cookies(card["url"]) if provider_mgr else False

        self._busy.add(interaction.message.id)
        await interaction.response.defer(ephemeral=True)

        tracker = await database.get_tracker(int(card["tracker_id"]), int(card["guild_id"]))
        series_title = tracker[10] if tracker and len(tracker) > 10 else None

        # Get cover url once for all builds
        cover_url = None
        if provider_mgr:
            try:
                cover_url = await asyncio.wait_for(provider_mgr.get_series_cover(card["url"]), timeout=5)
            except Exception:
                pass

        # Choose the initial status text based on lock and auth state
        if is_locked:
            if has_auth:
                initial_note = "📥 جاري التحميل باستخدام الكوكيز المحفوظة..."
            else:
                initial_note = "⚠️ الفصل مقفل (بدون كوكيز) · جاري المحاولة..."
        else:
            initial_note = "📥 جاري البدء..."

        progress = {"step": initial_note, "pct": 0}

        async def pcb(cur: int, tot: int, txt: str) -> None:
            pct = int((cur / max(tot, 1)) * 100)
            progress["pct"] = max(0, min(100, pct))
            progress["step"] = txt[:80]
            try:
                layout = build_tracker_card_layout(
                    interaction.client, card["url"],
                    float(card["chapter_num"]), str(card["chapter_url"]),
                    bool(card["locked"]),
                    note=f"📥 {progress['step']} · {progress['pct']}%",
                    view=self,
                    cover_url=cover_url,
                    series_title=series_title
                )
                await interaction.message.edit(embed=None, view=layout)
            except Exception:
                pass

        try:
            s_title = series_title or _series_name(card['url'])
            s_title_clean = re.sub(r'[\\/*?:"<>|]', "", s_title).strip()
            title = f"{s_title_clean}_Ch_{_lbl(float(card['chapter_num']))}"
            result_link = None

            if remote_down and getattr(remote_down, "is_enabled", False):
                job = await remote_down.start_download(str(card["chapter_url"]), title)
                if "error" in job:
                    raise RuntimeError(job["error"])
                result = await remote_down.wait_for_job(job["job_id"], progress_callback=pcb)
                if result.get("status") != "completed":
                    raise RuntimeError(result.get("message") or "Worker failed")
                result_link = result.get("result")
            else:
                def_up = await database.get_setting("default_upload_dest", "Auto")
                res = await downloader.download_and_stitch(
                    str(card["chapter_url"]), title,
                    upload_dest=def_up, progress_callback=pcb,
                )
                if res and res.get("link"):
                    result_link = res["link"]

            if result_link:
                layout = build_tracker_card_layout(
                    interaction.client, card["url"],
                    float(card["chapter_num"]), str(card["chapter_url"]),
                    bool(card["locked"]),
                    note=f"✅ تم التحميل: {result_link}",
                    view=self,
                    cover_url=cover_url,
                    series_title=series_title
                )
                await interaction.message.edit(embed=None, view=layout)
                await interaction.followup.send(f"✅ نتيجة التحميل:\n{result_link}", ephemeral=True)
            else:
                raise RuntimeError("ما حصلت على رابط نتيجة.")
        except Exception as e:
            err_msg = str(e)[:180]
            if is_locked and not has_auth:
                err_msg = "فصل مقفل. يرجى إضافة كوكيز للموقع عبر /auth_panel لتخطي الحماية."
            
            layout = build_tracker_card_layout(
                interaction.client, card["url"],
                float(card["chapter_num"]), str(card["chapter_url"]),
                bool(card["locked"]),
                note=f"❌ فشل التحميل: {err_msg}",
                view=self,
                cover_url=cover_url,
                series_title=series_title
            )
            # Try to edit color in layout to C_RED manually by accessing accent_color
            for child in layout.walk_children():
                if isinstance(child, discord.ui.Container):
                    child.accent_color = C_RED
            with contextlib.suppress(Exception):
                await interaction.message.edit(embed=None, view=layout)
            await interaction.followup.send(f"❌ Download فشل: `{str(e)[:220]}`", ephemeral=True)
        finally:
            self._busy.discard(interaction.message.id)

    async def choose_btn(self, interaction: discord.Interaction) -> None:
        if not await self._require_vip(interaction):
            return
        card = await self._load_card(interaction)
        if not card:
            await interaction.response.send_message("❌ هذه الرسالة غير مدعومة.", ephemeral=True)
            return
        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        if not provider_mgr:
            await interaction.response.send_message("❌ Provider manager غير متوفر.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            rich = await provider_mgr.get_chapters_with_lock_info(card["url"])
            if not rich:
                await interaction.followup.send("❌ ما قدرت أجيب الفصول.", ephemeral=True)
                return
            chapters = sorted(
                [
                    (float(k), v if isinstance(v, dict) else {"url": str(v), "locked": False})
                    for k, v in rich.items()
                ],
                key=lambda x: x[0],
                reverse=True,
            )
            view = RadarChapterPickerView(chapters, page=0, per_page=20)
            await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ فشل عرض الفصول: `{str(e)[:220]}`", ephemeral=True)

    async def pause_btn(self, interaction: discord.Interaction) -> None:
        if not await self._require_vip(interaction):
            return
        card = await self._load_card(interaction)
        if not card:
            await interaction.response.send_message("❌ هذه الرسالة غير مدعومة.", ephemeral=True)
            return
        tracker = await database.get_tracker(int(card["tracker_id"]), int(card["guild_id"]))
        if not tracker:
            await interaction.response.send_message("❌ المتتبع لم يعد موجود.", ephemeral=True)
            return
        paused = int(tracker[9] or 0)
        new_paused = 0 if paused else 1
        series_title = tracker[10] if tracker and len(tracker) > 10 else None
        await database.set_tracker_paused(int(card["tracker_id"]), int(card["guild_id"]), new_paused)
        note = "⏸️ تم إيقاف التتبع." if new_paused else "▶️ تم تشغيل التتبع."
        
        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        cover_url = None
        if provider_mgr:
            try:
                cover_url = await asyncio.wait_for(provider_mgr.get_series_cover(str(card["url"])), timeout=5)
            except Exception:
                pass

        try:
            layout = build_tracker_card_layout(
                interaction.client, str(card["url"]),
                float(card["chapter_num"]), str(card["chapter_url"]),
                bool(card["locked"]), note=note, paused=bool(new_paused),
                view=self, cover_url=cover_url, series_title=series_title
            )
            await interaction.message.edit(embed=None, view=layout)
        except Exception:
            pass
        await interaction.response.send_message("✅ تم التحديث.", ephemeral=True)

    async def remind_btn(self, interaction: discord.Interaction) -> None:
        if not await self._require_vip(interaction):
            return
        if not interaction.message:
            await interaction.response.send_message("❌ رسالة غير صالحة.", ephemeral=True)
            return
        await interaction.response.send_modal(ReminderModal(interaction.message.id))

    async def subscribe_btn(self, interaction: discord.Interaction) -> None:
        card = await self._load_card(interaction)
        if not card:
            await interaction.response.send_message("❌ هذه الرسالة غير مدعومة.", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ هذا الأمر داخل سيرفر فقط.", ephemeral=True)
            return

        # Fetch tracker details to get title
        tracker = await database.get_tracker(int(card["tracker_id"]), int(card["guild_id"]))
        if not tracker:
            await interaction.response.send_message("❌ المتتبع غير موجود.", ephemeral=True)
            return
            
        title = tracker[10] if len(tracker) > 10 and tracker[10] else _series_name(card["url"])
        role_name = f"🔔 Sub: {title[:80]}"
        
        # Check if role exists, if not, create it
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            await interaction.response.defer(ephemeral=True)
            try:
                role = await guild.create_role(
                    name=role_name,
                    mentionable=True,
                    reason=f"Auto-created subscription role for manga tracker: {title}"
                )
            except Exception as e:
                await interaction.followup.send(f"❌ فشل إنشاء دور الاشتراك: `{e}`", ephemeral=True)
                return
        else:
            await interaction.response.defer(ephemeral=True)

        member = guild.get_member(interaction.user.id)
        if not member:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except Exception:
                pass
                
        if not member:
            await interaction.followup.send("❌ تعذر تحديد بيانات العضو في هذا السيرفر.", ephemeral=True)
            return

        if role in member.roles:
            try:
                await member.remove_roles(role, reason="Unsubscribed from manga updates")
                await interaction.followup.send(f"🔕 تم إلغاء اشتراكك في تنبيهات **{title}** وسحب رول `{role.name}`.", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ فشل إلغاء الاشتراك: `{e}`", ephemeral=True)
        else:
            try:
                await member.add_roles(role, reason="Subscribed to manga updates")
                await interaction.followup.send(f"🔔 تم اشتراكك بنجاح في تنبيهات **{title}** وحصلت على رول `{role.name}`!", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ فشل الاشتراك: `{e}`", ephemeral=True)
