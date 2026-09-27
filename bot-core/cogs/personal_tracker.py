"""Personal tracker cog — per-user /mytrack with components v2."""
from __future__ import annotations

import asyncio
import datetime
import json
import logging

import database
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from services.intervals import format_check_interval, parse_check_interval
from services.adaptive_poller import AdaptivePoller
from services.feed_watcher import FeedWatcher
from ui.personal_tracker_v2 import (
    C_GOLD, C_GREY, C_GREEN, C_PURPLE,
    build_series_container, build_list_container,
    build_empty_panel, build_notification_container, build_new_chapter_container,
    display_name, chapter_label,
)
from user_system import user_only

logger = logging.getLogger("PersonalTracker")

MIN_INTERVAL = 1
MAX_TRACKERS_PER_USER = 30
MAX_CONSECUTIVE_FAILURES = 5
LOOP_MAX_PER_CYCLE = 20
LOOP_DELAY_BETWEEN = 2


class PButton(discord.ui.Button):
    def __init__(self, label: str, style: discord.ButtonStyle, custom_id: str, tracker_id: int, callback_func, row: int = 0):
        super().__init__(label=label, style=style, custom_id=custom_id, row=row)
        self.tracker_id = tracker_id
        self._cb = callback_func

    async def callback(self, interaction: discord.Interaction):
        await self._cb(interaction, self.tracker_id)


class DeleteConfirmView(discord.ui.View):
    def __init__(self, cog, tracker_id: int, user_id: int, tracker_title: str):
        super().__init__(timeout=60)
        self.cog = cog
        self.tracker_id = tracker_id
        self.user_id = user_id
        self.tracker_title = tracker_title

    @discord.ui.button(label="🗑️ تأكيد الحذف", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await database.delete_user_tracker(self.tracker_id, self.user_id)
        await interaction.response.edit_message(content=f"✅ تم حذف **{self.tracker_title}** من متابعاتك.", view=None)
        await self.cog._update_panel(self.user_id)

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ تم إلغاء عملية الحذف.", view=None)


class AddTrackerModal(ui.Modal):
    def __init__(self, user_id: int):
        super().__init__(title="➕ إضافة متابعة جديدة", timeout=300)
        self._user_id = user_id
        self._result_data: dict | None = None

        self.url_input = ui.TextInput(
            label="رابط السلسلة",
            placeholder="https://...",
            required=True,
            max_length=500,
        )
        self.last_chapter_input = ui.TextInput(
            label="آخر فصل قرأته",
            placeholder="0",
            required=True,
            max_length=10,
            default="0",
        )
        self.interval_input = ui.TextInput(
            label="⏱ الفحص (مثال: 30m, 2h, 90)",
            placeholder="30m  (أقل شيء 5m)",
            required=True,
            max_length=10,
            default="30m",
        )
        self.auto_download_input = ui.TextInput(
            label="📥 تحميل تلقائي؟ (نعم/لا)",
            placeholder="نعم",
            required=True,
            max_length=5,
            default="نعم",
        )
        self.mention_input = ui.TextInput(
            label="📢 منشن عند التحديث؟ (نعم/لا)",
            placeholder="نعم",
            required=True,
            max_length=5,
            default="نعم",
        )
        self.add_item(self.url_input)
        self.add_item(self.last_chapter_input)
        self.add_item(self.interval_input)
        self.add_item(self.auto_download_input)
        self.add_item(self.mention_input)

    def _validate_url(self, raw: str) -> str | None:
        raw = raw.strip()
        if not raw.startswith(("http://", "https://")):
            return None
        if len(raw) > 500:
            return None
        blacklist = ["porn", "hentai", "sex", "xxx", "adult"]
        if any(b in raw.lower() for b in blacklist):
            return None
        return raw

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        input_url = self._validate_url(self.url_input.value)
        if not input_url:
            await interaction.followup.send("❌ الرابط غير صالح (يجب أن يبدأ بـ http://)", ephemeral=True)
            return

        existing = await database.get_user_trackers(self._user_id)
        if any(t["url"].rstrip("/") == input_url.rstrip("/") for t in existing):
            await interaction.followup.send("❌ هذه السلسلة مضافة مسبقًا في متابعاتك", ephemeral=True)
            return

        resolved_url = input_url
        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        if provider_mgr:
            try:
                resolved_url = await provider_mgr.resolve_series_url_async(input_url)
            except Exception as e:
                logger.debug(f"Failed to resolve url: {e}")

        if any(t["url"].rstrip("/") == resolved_url.rstrip("/") for t in existing):
            await interaction.followup.send("❌ هذه السلسلة مضافة مسبقًا في متابعاتك", ephemeral=True)
            return

        url = resolved_url

        try:
            last_ch = float(self.last_chapter_input.value.strip())
        except (ValueError, TypeError):
            await interaction.followup.send("❌ آخر فصل يجب أن يكون رقمًا", ephemeral=True)
            return
        if last_ch < 0:
            await interaction.followup.send("❌ رقم الفصل لا يمكن أن يكون سالبًا", ephemeral=True)
            return

        try:
            interval = parse_check_interval(self.interval_input.value.strip())
        except ValueError:
            await interaction.followup.send("❌ فترة الفحص غير صالحة. استخدم مثلاً: 30m, 2h, 1.5h, 90", ephemeral=True)
            return
        interval = max(MIN_INTERVAL, min(1440, interval))

        auto_dl = self.auto_download_input.value.strip().lower() in ("نعم", "yes", "y", "1", "true")
        mention = self.mention_input.value.strip().lower() in ("نعم", "yes", "y", "1", "true")

        count = await database.get_user_tracker_count(self._user_id)
        if count >= MAX_TRACKERS_PER_USER:
            await interaction.followup.send(f"❌ لا يمكنك إضافة أكثر من {MAX_TRACKERS_PER_USER} متابعة", ephemeral=True)
            return

        existing = await database.get_user_trackers(self._user_id)
        if any(t["url"] == url for t in existing):
            await interaction.followup.send("❌ هذه السلسلة مضافتة مسبقًا في متابعاتك", ephemeral=True)
            return

        title = None
        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        if provider_mgr:
            try:
                title = await asyncio.wait_for(provider_mgr.get_series_title(url), timeout=15)
            except Exception:
                pass
        if not title:
            title = display_name(url)

        remote_down = getattr(interaction.client, "remote_down", None)
        cover_url = None
        if remote_down and remote_down.is_enabled:
            try:
                cover_url = await asyncio.wait_for(remote_down.radar_cover(url), timeout=15)
            except Exception:
                pass
        if not cover_url:
            provider_mgr = getattr(interaction.client, "provider_mgr", None)
            if provider_mgr:
                try:
                    cover_url = await asyncio.wait_for(provider_mgr.get_series_cover(url), timeout=15)
                except Exception:
                    pass

        await database.add_user_tracker(
            user_id=self._user_id,
            url=url,
            title=title,
            last_chapter=last_ch,
            interval_minutes=interval,
            auto_download=1 if auto_dl else 0,
            mention_on_update=1 if mention else 0,
            cover_url=cover_url,
        )

        self._result_data = {"title": title, "url": url, "cover_url": cover_url}
        await self._ask_for_channel(interaction)

    async def _ask_for_channel(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            return

        embed = discord.Embed(
            title="✅ تم إضافة المتابعة بنجاح!",
            description=f"📌 **السلسلة**: **[{self._result_data['title']}]({self._result_data['url']})**\n\n🔔 هل ترغب في تعيين روم مخصص للإشعارات والتحميل التلقائي؟\n\n💡 *اختر القناة من القائمة المنسدلة بالأسفل أو اضغط **تخطي** للإنهاء.*",
            color=C_GREEN,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        if self._result_data.get("cover_url"):
            embed.set_thumbnail(url=self._result_data["cover_url"])
        view = ChannelSelectView(self._user_id, self._result_data)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class ChannelSelectView(ui.View):
    def __init__(self, user_id: int, result_data: dict):
        super().__init__(timeout=120)
        self._user_id = user_id
        self._result_data = result_data
        self.add_item(ChannelSelect(user_id, result_data))

    @ui.button(label="⏭️ تخطي", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="✅ تمت الإضافة بدون روم إشعارات.", view=None, embed=None)
        cog = interaction.client.get_cog("PersonalTrackerCog")
        if cog:
            await cog._update_panel(self._user_id)


class ChannelSelect(ui.ChannelSelect):
    def __init__(self, user_id: int, result_data: dict):
        super().__init__(
            placeholder="اختر روم الإشعارات...",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
        )
        self._user_id = user_id
        self._result_data = result_data

    async def callback(self, interaction: discord.Interaction):
        if not self.values:
            return
        channel = self.values[0]
        trackers = await database.get_user_trackers(self._user_id)
        for t in reversed(trackers):
            if t["url"] == self._result_data["url"]:
                await database.update_user_tracker(t["id"], self._user_id, notification_channel_id=str(channel.id))
                break
        await interaction.response.edit_message(
            content=f"✅ تم تعيين روم الإشعارات إلى {channel.mention}", view=None, embed=None,
        )
        cog = interaction.client.get_cog("PersonalTrackerCog")
        if cog:
            await cog._update_panel(self._user_id)


class SettingsModal(ui.Modal):
    def __init__(self, tracker_id: int, user_id: int, current_title: str, current_interval: int, current_auto: bool, current_mention: bool):
        super().__init__(title="⚙️ إعدادات المتابعة", timeout=300)
        self._tracker_id = tracker_id
        self._user_id = user_id
        self._current_auto = current_auto
        self._current_mention = current_mention

        self.title_input = ui.TextInput(
            label="✏️ اسم التتبع",
            required=False,
            max_length=100,
        )
        self.title_input.default = current_title
        self.interval_input = ui.TextInput(
            label="⏱ الفحص (مثال: 30m, 2h, 90)",
            placeholder="30m",
            required=True,
            max_length=10,
        )
        self.interval_input.default = format_check_interval(current_interval)
        self.add_item(self.title_input)
        self.add_item(self.interval_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            interval = parse_check_interval(self.interval_input.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ فترة الفحص غير صالحة. استخدم مثلاً: 30m, 2h, 1.5h, 90", ephemeral=True)
            return
        interval = max(MIN_INTERVAL, min(1440, interval))

        new_title = self.title_input.value.strip() or None

        updates = {
            "interval_minutes": interval,
            "auto_download": 1 if self._current_auto else 0,
            "mention_on_update": 1 if self._current_mention else 0,
        }
        if new_title:
            updates["title"] = new_title

        await database.update_user_tracker(self._tracker_id, self._user_id, **updates)
        await interaction.response.defer(ephemeral=True)
        fmt = format_check_interval(interval)
        desc = (
            f"⚙️ **التعديلات الجديدة:**\n"
            f"├─ ✏️ **الاسم**: `{new_title or 'بدون تغيير'}`\n"
            f"├─ ⏱️ **فترة الفحص**: كل `{fmt}`\n"
            f"├─ 📥 **التحميل التلقائي**: `{'✅ نشط' if self._current_auto else '❌ معطل'}`\n"
            f"└─ 📢 **تنبيه بالمنشن**: `{'✅ مفعل' if self._current_mention else '❌ معطل'}`"
        )
        embed = discord.Embed(
            title="✅ تم تحديث إعدادات السلسلة",
            description=desc,
            color=C_GREEN,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        cog = interaction.client.get_cog("PersonalTrackerCog")
        if cog:
            await cog._update_panel(self._user_id)


class TrackerChannelSelectView(ui.View):
    def __init__(self, tracker_id: int, user_id: int):
        super().__init__(timeout=120)
        self._tracker_id = tracker_id
        self._user_id = user_id
        self.add_item(TrackerChannelSelect(tracker_id, user_id))

    @ui.button(label="⏭️ إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="✅ لم يتغير شيء.", view=None, embed=None)


class TrackerChannelSelect(ui.ChannelSelect):
    def __init__(self, tracker_id: int, user_id: int):
        super().__init__(
            placeholder="اختر روم الإشعارات...",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
        )
        self._tracker_id = tracker_id
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if not self.values:
            return
        channel = self.values[0]
        await database.update_user_tracker(self._tracker_id, self._user_id, notification_channel_id=str(channel.id))
        await interaction.response.edit_message(
            content=f"✅ تم تعيين روم الإشعارات إلى {channel.mention}", view=None, embed=None,
        )
        cog = interaction.client.get_cog("PersonalTrackerCog")
        if cog:
            await cog._update_panel(self._user_id)


class RemindModal(ui.Modal):
    def __init__(self, tracker_id: int, user_id: int):
        super().__init__(title="⏰ تذكير", timeout=300)
        self._tracker_id = tracker_id
        self._user_id = user_id
        self.duration = ui.TextInput(
            label="بعد كم؟ (مثال: 5h15m أو 45m أو 2h)",
            placeholder="5h15m",
            required=True,
            max_length=20,
        )
        self.add_item(self.duration)

    async def on_submit(self, interaction: discord.Interaction):
        secs = _parse_duration(str(self.duration.value))
        if not secs or secs < 60:
            await interaction.response.send_message("❌ مدة غير صالحة (أقل شيء دقيقة).", ephemeral=True)
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        notify_at = now + datetime.timedelta(seconds=secs)
        try:
            await database.add_radar_reminder(
                message_id=0, tracker_id=self._tracker_id,
                guild_id=0, channel_id=0, user_id=self._user_id,
                notify_at_iso=notify_at.isoformat(),
            )
        except Exception:
            pass
        h = secs // 3600
        m = (secs % 3600) // 60
        time_str = f"{h}h {m}m" if h else f"{m}m"
        await interaction.response.send_message(f"✅ تم ضبط التذكير بعد **{time_str}**.", ephemeral=True)


def _parse_duration(text: str) -> int | None:
    import re
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


class PTChapterSelect(discord.ui.Select):
    def __init__(self, chapters_page: list[tuple[float, dict]], parent_view):
        self._items = chapters_page
        self._parent = parent_view
        options = []
        for num, info in chapters_page[:25]:
            locked = bool(info.get("locked"))
            prefix = "🔒" if locked else "🟢"
            unlock_time = info.get("unlock_time")
            desc = "مقفل" if locked else "متاح"
            if locked and unlock_time:
                try:
                    rem = float(unlock_time) - datetime.datetime.now(datetime.timezone.utc).timestamp()
                    if rem > 0:
                        h = int(rem // 3600)
                        m = int((rem % 3600) // 60)
                        desc = f"يفتح بعد {h}h {m}m" if h else f"يفتح بعد {m}m"
                except Exception:
                    pass
            options.append(discord.SelectOption(
                label=f"{prefix} Ch. {chapter_label(num)}",
                value=str(num),
                description=desc[:100],
            ))
        super().__init__(placeholder="اختر فصلاً…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        self._parent._selected = float(self.values[0])
        await interaction.response.defer()


class ChapterInputModal(ui.Modal):
    def __init__(self, parent_view):
        super().__init__(title="✏️ إدخال رقم الفصل", timeout=120)
        self._parent = parent_view
        self.chapter_input = ui.TextInput(
            label="رقم الفصل",
            placeholder="مثال: 71",
            required=True,
            max_length=10,
        )
        self.add_item(self.chapter_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            num = float(self.chapter_input.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ رقم غير صالح.", ephemeral=True)
            return
        item = next((i for n, i in self._parent._chapters if float(n) == num), None)
        if not item:
            await interaction.response.send_message(f"❌ الفصل {num} غير موجود.", ephemeral=True)
            return
        ch_url = item.get("url") or ""
        if not ch_url:
            await interaction.response.send_message("❌ لا يوجد رابط للفصل.", ephemeral=True)
            return
        await self._parent._do_download(interaction, ch_url)


class PTChapterPickerView(discord.ui.View):
    def __init__(self, chapters: list[tuple[float, dict]], cog, tracker_id: int, series_url: str = "", page: int = 0, per_page: int = 20):
        super().__init__(timeout=600)
        self._chapters = chapters
        self._cog = cog
        self._tracker_id = tracker_id
        self._url = series_url
        self._page = page
        self._per_page = per_page
        self._total_pages = max(1, (len(chapters) + per_page - 1) // per_page)
        self._selected: float | None = None
        self._rebuild()

    def _page_items(self) -> list[tuple[float, dict]]:
        start = self._page * self._per_page
        return self._chapters[start:start + self._per_page]

    def _rebuild(self):
        self.clear_items()
        items = self._page_items()
        if items:
            self.add_item(PTChapterSelect(items, self))
        dl_btn = discord.ui.Button(label="⬇️ تحميل", style=discord.ButtonStyle.success, row=1)
        dl_btn.callback = self._download_callback
        self.add_item(dl_btn)
        manual_btn = discord.ui.Button(label="✏️ يدوي", style=discord.ButtonStyle.secondary, row=1)
        manual_btn.callback = self._manual_callback
        self.add_item(manual_btn)
        prev = discord.ui.Button(label="◀ السابق", style=discord.ButtonStyle.secondary, disabled=self._page <= 0, row=2)
        prev.callback = self._prev_callback
        self.add_item(prev)
        next_btn = discord.ui.Button(label="التالي ▶", style=discord.ButtonStyle.secondary, disabled=self._page >= self._total_pages - 1, row=2)
        next_btn.callback = self._next_callback
        self.add_item(next_btn)

    def build_embed(self) -> discord.Embed:
        em = discord.Embed(title="📚 تصفح الفصول", color=C_GOLD, timestamp=datetime.datetime.now(datetime.timezone.utc))
        items = self._page_items()
        if not items:
            em.description = "لا توجد فصول."
        else:
            lines = []
            for num, info in items:
                prefix = "🔒" if info.get("locked") else "🟢"
                lines.append(f"{prefix} **Ch. {chapter_label(num)}**")
            em.description = "\n".join(lines)
        em.set_footer(text=f"صفحة {self._page + 1} / {self._total_pages}")
        return em

    async def _download_callback(self, interaction: discord.Interaction):
        try:
            if self._selected is None:
                await interaction.response.send_message("❌ اختر فصلاً أولاً من القائمة أو استخدم الزر '✏️ يدوي'.", ephemeral=True)
                return
            item = next((i for n, i in self._page_items() if float(n) == self._selected), None)
            if not item:
                await interaction.response.send_message("❌ الفصل غير موجود في هذه الصفحة.", ephemeral=True)
                return
            ch_url = item.get("url") or ""
            if not ch_url:
                await interaction.response.send_message("❌ لا يوجد رابط للفصل.", ephemeral=True)
                return
            await self._do_download(interaction, ch_url)
        except Exception as e:
            try:
                await interaction.response.send_message(f"❌ خطأ: {str(e)[:200]}", ephemeral=True)
            except discord.errors.InteractionResponded:
                await interaction.followup.send(f"❌ خطأ: {str(e)[:200]}", ephemeral=True)

    async def _manual_callback(self, interaction: discord.Interaction):
        modal = ChapterInputModal(self)
        await interaction.response.send_modal(modal)

    async def _do_download(self, interaction: discord.Interaction, ch_url: str):
        await self._cog._cog_do_download(interaction, self._tracker_id, ch_url)

    async def _prev_callback(self, interaction: discord.Interaction):
        self._page = max(0, self._page - 1)
        self._selected = None
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next_callback(self, interaction: discord.Interaction):
        self._page = min(self._total_pages - 1, self._page + 1)
        self._selected = None
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class TrackerSelect(ui.Select):
    def __init__(self, user_id: int, trackers: list[dict], cog, covers: dict, lock_cache: dict):
        self._user_id = user_id
        self._all_trackers = trackers
        self._cog = cog
        self._all_covers = covers
        self._lock_cache = lock_cache

        options = []
        for t in trackers[:25]:
            lc = lock_cache.get(t["id"], {})
            locked = lc.get("locked", False)
            paused = t["paused"]
            status = "🔒" if locked else ("⏸️" if paused else "🟢")
            lbl = (t["title"] or "بدون عنوان")[:80]
            has_ch = bool(t.get("notification_channel_id"))
            desc = f"Ch.{chapter_label(t['last_chapter'])} {'🔔' if has_ch else '🔕'}"
            options.append(discord.SelectOption(label=lbl, description=desc, emoji=status, value=str(t["id"])))

        super().__init__(placeholder="اختر متابعة...", options=options, min_values=1, max_values=1, custom_id=f"pt_sel_{user_id}")

    async def callback(self, interaction: discord.Interaction):
        tracker_id = int(self.values[0])
        new_view = PersonalTrackerView(
            self._user_id, self._all_trackers, self._cog,
            covers=self._all_covers, lock_cache=self._lock_cache,
            mode="detail", tracker_id=tracker_id,
        )
        await interaction.response.edit_message(view=new_view)


def get_prediction_string(pattern_json: str | None) -> str:
    if not pattern_json:
        return ""
    try:
        pattern = json.loads(pattern_json)
        day = pattern.get("day", -1)
        hour = pattern.get("hour", -1)
        confidence = pattern.get("confidence", 0.0)
        
        if day == -1 or hour == -1 or confidence <= 0.0:
            return ""
            
        weekdays_ar = {
            0: "الإثنين",
            1: "الثلاثاء",
            2: "الأربعاء",
            3: "الخميس",
            4: "الجمعة",
            5: "السبت",
            6: "الأحد"
        }
        
        day_str = weekdays_ar.get(day, "")
        if not day_str:
            return ""
            
        is_pm = hour >= 12
        hour_12 = hour % 12
        if hour_12 == 0:
            hour_12 = 12
        
        period = "مساءً" if is_pm else "صباحاً"
        time_str = f"{hour_12:02d}:00 {period}"
        confidence_pct = int(confidence * 100)
        
        return f"{day_str} الساعة {time_str} ({confidence_pct}% ثقة)"
    except Exception:
        return ""


class PersonalTrackerView(ui.LayoutView):
    SELECT_LIMIT = 25

    def __init__(self, user_id: int, trackers: list[dict], cog: PersonalTrackerCog,
                 covers: dict[int, str | None] | None = None, lock_cache: dict[int, dict] | None = None,
                 mode: str = "list", tracker_id: int | None = None, select_page: int = 0):
        super().__init__(timeout=None)
        self._user_id = user_id
        self._all_trackers = trackers
        self._cog = cog
        self._all_covers = covers or {}
        self._lock_cache = lock_cache or {}
        self._mode = mode

        if mode == "detail" and tracker_id is not None:
            self._build_detail(tracker_id)
        else:
            self._build_list(select_page)

    def _build_list(self, select_page: int = 0):
        if not self._all_trackers:
            self.add_item(build_empty_panel())
            return

        total = len(self._all_trackers)
        total_select_pages = max(1, (total + self.SELECT_LIMIT - 1) // self.SELECT_LIMIT)
        
        # 1. Build paginated list summary
        page_start = select_page * self.SELECT_LIMIT
        page_end = page_start + self.SELECT_LIMIT
        page_trackers = self._all_trackers[page_start:page_end]
        
        list_cont = build_list_container(
            page_trackers=page_trackers,
            page=select_page,
            total_pages=total_select_pages,
            total_count=total,
            lock_cache=self._lock_cache
        )
        self.add_item(list_cont)

        # 2. Dropdown Selector
        select_cont = discord.ui.Container(accent_color=C_GREY)
        select = TrackerSelect(self._user_id, page_trackers, self._cog, self._all_covers, self._lock_cache)
        select_cont.add_item(ui.ActionRow(select))
        self.add_item(select_cont)

        # 3. Navigation Buttons (if paginated)
        if total_select_pages > 1:
            nav_cont = discord.ui.Container(accent_color=C_GREY)
            nav_btns = []
            if select_page > 0:
                nav_btns.append(
                    PButton("◀️ السابق", discord.ButtonStyle.secondary, "pt_sprev", 0, self._make_select_page_handler(select_page - 1))
                )
            if select_page < total_select_pages - 1:
                nav_btns.append(
                    PButton("التالي ▶️", discord.ButtonStyle.secondary, "pt_snext", 0, self._make_select_page_handler(select_page + 1))
                )
            if nav_btns:
                nav_cont.add_item(ui.ActionRow(*nav_btns))
            self.add_item(nav_cont)

        # 4. Action buttons
        action_row = ui.ActionRow(
            PButton("➕ إضافة تتبع", discord.ButtonStyle.success, "pt_add", 0, self._cog._add_handler),
            PButton("🔄 فحص الكل", discord.ButtonStyle.primary, "pt_refresh_all", 0, self._cog._refresh_all_handler),
            PButton("📦 استيراد الرادار", discord.ButtonStyle.secondary, "pt_import_radar", 0, self._cog._import_from_radar_handler),
        )
        action_cont = discord.ui.Container(accent_color=C_GOLD)
        action_cont.add_item(action_row)
        self.add_item(action_cont)

    def _make_select_page_handler(self, page: int):
        async def handler(interaction: discord.Interaction, _tid: int):
            new_view = PersonalTrackerView(
                self._user_id, self._all_trackers, self._cog,
                covers=self._all_covers, lock_cache=self._lock_cache,
                mode="list", select_page=page,
            )
            await interaction.response.edit_message(view=new_view)
        return handler

    def _build_detail(self, tracker_id: int):
        t = next((t2 for t2 in self._all_trackers if t2["id"] == tracker_id), None)
        if not t:
            self._build_list()
            return

        lc = self._lock_cache.get(t["id"], {})
        pred = get_prediction_string(t.get("release_pattern"))
        container = build_series_container(
            tracker_id=t["id"],
            user_id=t["user_id"],
            url=t["url"],
            title=t["title"],
            last_chapter=t["last_chapter"],
            interval_minutes=t["interval_minutes"],
            auto_download=bool(t["auto_download"]),
            paused=bool(t["paused"]),
            notification_channel=f"<#{t['notification_channel_id']}>" if t.get("notification_channel_id") else "",
            mention_on_update=bool(t["mention_on_update"]),
            cover_url=self._all_covers.get(t["id"]),
            locked=lc.get("locked", False),
            unlock_time=lc.get("unlock_time"),
            latest_chapter=lc.get("latest_chapter"),
            predicted_release=pred,
        )
        house_row = ui.ActionRow(
            PButton("🏠", discord.ButtonStyle.secondary, "pt_back", 0, self._handle_back),
        )
        container._children.insert(0, house_row)
        row1 = ui.ActionRow(
            PButton("🔄 فحص", discord.ButtonStyle.primary, f"pt_ref_{t['id']}", t["id"], self._cog._refresh_handler),
            PButton("📚 تصفح", discord.ButtonStyle.secondary, f"pt_browse_{t['id']}", t["id"], self._cog._browse_handler),
            PButton("⏸️ إيقاف" if not t["paused"] else "▶️ تشغيل", discord.ButtonStyle.secondary, f"pt_pause_{t['id']}", t["id"], self._cog._pause_handler),
            PButton("⏰ تذكير", discord.ButtonStyle.secondary, f"pt_remind_{t['id']}", t["id"], self._cog._remind_handler),
        )
        row2 = ui.ActionRow(
            PButton("🔔 روم", discord.ButtonStyle.secondary, f"pt_channel_{t['id']}", t["id"], self._cog._channel_handler),
            PButton("⚙️ إعدادات", discord.ButtonStyle.secondary, f"pt_settings_{t['id']}", t["id"], self._cog._settings_handler),
            PButton("❌ حذف", discord.ButtonStyle.danger, f"pt_delete_{t['id']}", t["id"], self._cog._delete_handler),
        )
        container.add_item(row1)
        container.add_item(row2)
        self.add_item(container)

    async def _handle_back(self, interaction: discord.Interaction, _tracker_id: int):
        new_view = PersonalTrackerView(
            self._user_id, self._all_trackers, self._cog,
            covers=self._all_covers, lock_cache=self._lock_cache,
            mode="list",
        )
        await interaction.response.edit_message(view=new_view)


class PersonalTrackerCog(commands.Cog):
    """Tracking personal series per user with components v2."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active_downloads: set[tuple[int, float]] = set()
        self._notified_chapters: set[tuple[int, float]] = set()
        self._consecutive_failures: dict[int, int] = {}
        self._panel_lock = asyncio.Lock()
        self._last_panel_update: dict[int, float] = {}
        self._last_notified_cleanup: float = 0
        self._lock_cache: dict[int, dict] = {}
        self.adaptive_poller = AdaptivePoller()
        self.feed_watcher = FeedWatcher()
        self._download_semaphore = asyncio.Semaphore(3)
        self._cover_cache: dict[str, tuple[str | None, datetime.datetime]] = {}
        self._bg_task = self.bg_loop.start()

    async def _get_or_fetch_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        ch = self.bot.get_channel(channel_id)
        if not ch:
            try:
                ch = await self.bot.fetch_channel(channel_id)
            except Exception as e:
                logger.warning(f"[PersonalTracker] Failed to fetch channel {channel_id}: {e}")
                ch = None
        return ch

    async def _cog_do_download(self, interaction: discord.Interaction, tracker_id: int, ch_url: str):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        tracker = await database.get_user_tracker(tracker_id, interaction.user.id)
        title = (tracker["title"] or "مجهول")[:50] if tracker else "مجهول"
        remote_down = getattr(interaction.client, "remote_down", None)
        if not remote_down or not remote_down.is_enabled:
            await interaction.followup.send("❌ Worker التحميل غير متاح.", ephemeral=True)
            return
        try:
            result = await asyncio.wait_for(remote_down.start_download(ch_url, title, job_type="manga"), timeout=30)
            if not result or not result.get("job_id"):
                await interaction.followup.send(f"❌ فشل بدء التحميل: {result.get('error', 'غير معروف') if result else 'غير معروف'}", ephemeral=True)
                return
            await interaction.followup.send(f"⏳ تم إرسال أمر التحميل لـ **{title}** — انتظر رسالة الإكمال.", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("❌ انتهت مهلة بدء التحميل.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ خطأ: `{str(e)[:150]}`", ephemeral=True)

    async def _get_notification_target(self, tracker: dict) -> discord.TextChannel | discord.User | None:
        uid = tracker["user_id"]
        channel_id = tracker.get("notification_channel_id")
        if channel_id:
            try:
                ch = await self._get_or_fetch_channel(int(channel_id))
                if ch:
                    return ch
            except Exception as e:
                logger.warning(f"[PersonalTracker] Failed to get specific channel {channel_id}: {e}")
        panel_msg_data = await database.get_panel_message(uid)
        if panel_msg_data:
            try:
                ch = await self._get_or_fetch_channel(int(panel_msg_data["channel_id"]))
                if ch:
                    return ch
            except Exception as e:
                logger.warning(f"[PersonalTracker] Failed to get panel channel {panel_msg_data.get('channel_id')}: {e}")
        try:
            user = self.bot.get_user(uid)
            if not user:
                user = await self.bot.fetch_user(uid)
            return user
        except Exception as e:
            logger.warning(f"[PersonalTracker] Failed to get user {uid} for DM fallback: {e}")
        return None

    async def _send_notification_to_target(self, target, content: str | None, view: discord.ui.View | None) -> bool:
        if not target:
            return False
        try:
            await target.send(content=content, view=view)
            return True
        except discord.Forbidden:
            logger.warning(f"[PersonalTracker] Permission Forbidden sending notification to {target}")
        except Exception as e:
            logger.warning(f"[PersonalTracker] Failed to send notification to {target}: {e}")
        return False

    def cog_unload(self):
        self._bg_task.cancel()

    @tasks.loop(minutes=1)
    async def bg_loop(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            all_active = await database.get_all_active_user_trackers()
        except Exception as e:
            logger.warning(f"Failed to fetch active user trackers: {e}")
            return

        if not all_active:
            return

        provider_mgr = getattr(self.bot, "provider_mgr", None)
        if not provider_mgr:
            return
        
        self.feed_watcher.provider_manager = provider_mgr

        due = await self.adaptive_poller.get_due_trackers(all_active, now)
        if not due:
            return

        # Group due trackers by URL
        from collections import defaultdict
        grouped_due = defaultdict(list)
        for t in due:
            grouped_due[t["url"]].append(t)

        checked = 0
        now_ts = now.timestamp()
        for url, trackers in grouped_due.items():
            if checked >= LOOP_MAX_PER_CYCLE:
                break

            await self._check_shared_url(url, trackers, provider_mgr, now, now_ts)
            checked += 1
            await asyncio.sleep(LOOP_DELAY_BETWEEN)

        self._cleanup_notified_set(now)

    def _cleanup_notified_set(self, now: datetime.datetime):
        now_ts = now.timestamp()
        if now_ts - self._last_notified_cleanup < 172800:
            return
        self._last_notified_cleanup = now_ts
        self._notified_chapters.clear()

    @bg_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    async def _check_tracker(self, tracker: dict, provider_mgr, now: datetime.datetime, now_ts: float):
        await self._check_shared_url(tracker["url"], [tracker], provider_mgr, now, now_ts)

    async def _check_shared_url(self, url: str, trackers: list[dict], provider_mgr, now: datetime.datetime, now_ts: float):
        representative = trackers[0]
        check_method = representative.get("check_method", "scrape")
        detected_method = self.feed_watcher.detect_check_method(url)
        if detected_method != check_method and check_method == "scrape":
            check_method = detected_method

        max_last_chapter = max((float(t.get("last_chapter", 0.0)) for t in trackers), default=0.0)

        rich = None
        latest = None
        latest_info = None
        success = False

        try:
            quick_res = await self.feed_watcher.quick_check_individual(url, max_last_chapter, check_method)
            if quick_res:
                latest = quick_res["latest"]
                new_chapters = quick_res["new_chapters"]
                if new_chapters:
                    latest_info = {
                        "url": new_chapters[-1]["url"],
                        "locked": new_chapters[-1]["locked"]
                    }
                success = True
            else:
                rich = await asyncio.wait_for(provider_mgr.get_chapters_with_lock_info(url), timeout=20)
                if rich:
                    latest = max(rich.keys())
                    latest_info = rich.get(latest, {})
                success = True
        except asyncio.TimeoutError:
            for tracker in trackers:
                tid = tracker["id"]
                uid = tracker["user_id"]
                priority = tracker.get("priority", "normal")
                heat_score = tracker.get("heat_score", 50.0)
                consecutive_failures = tracker.get("consecutive_failures", 0)
                failures = consecutive_failures + 1

                new_heat = self.adaptive_poller.update_heat(heat_score, "error")
                await database.update_user_tracker_polling_state(tid, new_heat, priority, check_method, failures)

                if failures >= MAX_CONSECUTIVE_FAILURES:
                    await database.update_user_tracker(tid, uid, paused=1)
                    target = await self._get_notification_target(tracker)
                    if target:
                        try:
                            await target.send(content=f"<@{uid}> ⚠️ **{tracker['title']}**: تم إيقاف التتبع بسبب {MAX_CONSECUTIVE_FAILURES} محاولات فاشلة متتالية (الموقع لا يستجيب).")
                        except Exception as e:
                            logger.warning(f"Failed to send disable notification: {e}")
            return
        except Exception as e:
            logger.warning(f"Error checking shared URL {url}: {e}")
            for tracker in trackers:
                tid = tracker["id"]
                priority = tracker.get("priority", "normal")
                heat_score = tracker.get("heat_score", 50.0)
                consecutive_failures = tracker.get("consecutive_failures", 0)
                failures = consecutive_failures + 1

                new_heat = self.adaptive_poller.update_heat(heat_score, "error")
                await database.update_user_tracker_polling_state(tid, new_heat, priority, check_method, failures)
            return

        if success and latest is not None:
            for tracker in trackers:
                tid = tracker["id"]
                uid = tracker["user_id"]
                priority = tracker.get("priority", "normal")
                heat_score = tracker.get("heat_score", 50.0)
                old = float(tracker.get("last_chapter", 0))

                if latest > old:
                    dedup_key = (tid, latest)
                    if dedup_key not in self._notified_chapters:
                        self._notified_chapters.add(dedup_key)
                        await self._notify_new_chapter(tracker, latest, latest_info)

                    new_heat = self.adaptive_poller.update_heat(heat_score, "new_chapter")
                    release_pattern = tracker.get("release_pattern")
                    new_pattern = self.adaptive_poller.learn_schedule(release_pattern, now)

                    await database.update_user_tracker(tid, uid, last_chapter=latest, last_checked=now.isoformat())
                    await database.update_user_tracker_polling_state(tid, new_heat, priority, check_method, 0)
                    await database.update_user_tracker_release_pattern(tid, new_pattern, now.isoformat())

                    auto_dl = bool(tracker.get("auto_download"))
                    chapter_url = latest_info.get("url", "") if isinstance(latest_info, dict) else ""
                    if auto_dl and chapter_url:
                        locked = latest_info.get("locked", False) if isinstance(latest_info, dict) else False
                        has_cookies = provider_mgr.has_auth_cookies(url) if provider_mgr else False
                        if not locked or has_cookies:
                            await self._try_auto_download(tracker, latest, chapter_url, tracker["title"])
                else:
                    new_heat = self.adaptive_poller.update_heat(heat_score, "no_change")
                    await database.update_user_tracker(tid, uid, last_checked=now.isoformat())
                    await database.update_user_tracker_polling_state(tid, new_heat, priority, check_method, 0)

    async def _notify_new_chapter(self, tracker: dict, chapter_num: float, chapter_info: dict):
        uid = tracker["user_id"]
        url = tracker["url"]
        title = tracker["title"]
        mention = bool(tracker.get("mention_on_update"))
        auto_dl = bool(tracker.get("auto_download"))

        chapter_url = chapter_info.get("url", "") if isinstance(chapter_info, dict) else ""
        locked = chapter_info.get("locked", False) if isinstance(chapter_info, dict) else False

        # Check if domain has auth cookies
        provider_mgr = getattr(self.bot, "provider_mgr", None)
        has_cookies = provider_mgr.has_auth_cookies(url) if provider_mgr else False

        # Resolve custom alerts/mentions
        extra_mentions = []
        notify_user = tracker.get("notify_user_id")
        notify_role = tracker.get("notify_role_id")
        if notify_user:
            extra_mentions.append(f"<@{notify_user}>")
        if notify_role:
            extra_mentions.append(f"<@&{notify_role}>")

        custom_msg = tracker.get("custom_message")
        content_lines = []
        if mention:
            mention_parts = [f"<@{uid}>"] + extra_mentions
            content_lines.append(" ".join(mention_parts))
        if custom_msg:
            content_lines.append(custom_msg)
        else:
            content_lines.append(f"📢 **{title}** — فصل جديد: **{chapter_label(chapter_num)}**")
        content_str = "\n".join(content_lines)

        cover_url = tracker.get("cover_url")
        if not cover_url:
            cover_url = await self._fetch_single_cover(url)

        # Use the new container builder for release alerts
        container = build_new_chapter_container(
            series_name=title,
            series_url=url,
            chapter_num=chapter_num,
            chapter_url=chapter_url,
            locked=locked,
            has_cookies=has_cookies,
            cover_url=cover_url,
        )
        layout = discord.ui.LayoutView(timeout=None)
        layout.add_item(container)

        # Define button callbacks
        async def dl_callback(interaction: discord.Interaction, tracker_id: int):
            await self._cog_do_download(interaction, tracker_id, chapter_url)

        # Add before-download buttons: ⬇️⚙️🔄
        row = ui.ActionRow(
            PButton("⬇️ تحميل", discord.ButtonStyle.success, f"pt_alert_dl_{tracker['id']}_{chapter_num}", tracker["id"], dl_callback),
            PButton("⚙️ إعدادات", discord.ButtonStyle.secondary, f"pt_alert_set_{tracker['id']}", tracker["id"], self._settings_handler),
            PButton("🔄 فحص", discord.ButtonStyle.primary, f"pt_alert_ref_{tracker['id']}", tracker["id"], self._refresh_handler),
        )
        layout.add_item(row)

        target = await self._get_notification_target(tracker)
        sent = False
        if target:
            sent = await self._send_notification_to_target(target, content_str, layout)

        if not sent and isinstance(target, discord.TextChannel):
            try:
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                if user:
                    dm_content = f"⚠️ (فشل إرسال الإشعار في القناة المخصصة)\n{content_str}"
                    await self._send_notification_to_target(user, dm_content, layout)
            except Exception as e:
                logger.warning(f"[PersonalTracker] DM fallback failed for user {uid}: {e}")

        # Auto download if enabled and the chapter is free OR we have cookies to unlock it
        if auto_dl and chapter_url:
            if not locked or has_cookies:
                await self._try_auto_download(tracker, chapter_num, chapter_url, title)

        await database.update_user_tracker(tracker["id"], uid, last_chapter=chapter_num)

    async def _try_auto_download(self, tracker: dict, chapter_num: float, chapter_url: str, title: str):
        tid = tracker["id"]
        uid = tracker["user_id"]
        dl_key = (tid, chapter_num)
        if dl_key in self._active_downloads:
            return
        self._active_downloads.add(dl_key)

        async with self._download_semaphore:
            try:
                remote_down = getattr(self.bot, "remote_down", None)
                if remote_down and remote_down.is_enabled:
                    short_title = title[:50]
                    result = await asyncio.wait_for(
                        remote_down.start_download(chapter_url, short_title, job_type="manga"),
                        timeout=30,
                    )
                    if result.get("job_id"):
                        job_id = result["job_id"]
                        final = await asyncio.wait_for(
                            remote_down.wait_for_job(job_id, max_wait_sec=3600),
                            timeout=3600,
                        )
                        main_link = final.get("result") or ""
                    else:
                        main_link = ""
                else:
                    main_link = ""

                cover_url = tracker.get("cover_url")
                if not cover_url:
                    cover_url = await self._fetch_single_cover(tracker["url"])

                container = build_notification_container(
                    series_name=title,
                    series_url=tracker["url"],
                    chapter_num=chapter_num,
                    chapter_url=chapter_url,
                    main_link=main_link or None,
                    cover_url=cover_url,
                    failed=not bool(main_link),
                    failed_reason="" if main_link else "فشل التحميل",
                )
                layout = discord.ui.LayoutView(timeout=None)
                layout.add_item(container)

                mention = bool(tracker.get("mention_on_update"))
                extra_mentions = []
                notify_user = tracker.get("notify_user_id")
                notify_role = tracker.get("notify_role_id")
                if notify_user:
                    extra_mentions.append(f"<@{notify_user}>")
                if notify_role:
                    extra_mentions.append(f"<@&{notify_role}>")

                custom_msg = tracker.get("custom_message")
                content_lines = []
                if mention:
                    mention_parts = [f"<@{uid}>"] + extra_mentions
                    content_lines.append(" ".join(mention_parts))
                if custom_msg:
                    content_lines.append(custom_msg)

                content_str = "\n".join(content_lines) if content_lines else None

                # Button callbacks
                async def dl_callback(interaction: discord.Interaction, tracker_id: int):
                    await self._cog_do_download(interaction, tracker_id, chapter_url)

                # Setup state-aware buttons:
                if main_link:
                    # Success buttons: 📖⚙️
                    row = ui.ActionRow(
                        discord.ui.Button(label="📖 قراءة", url=main_link, style=discord.ButtonStyle.link),
                        PButton("⚙️ إعدادات", discord.ButtonStyle.secondary, f"pt_alert_set_{tracker['id']}", tracker["id"], self._settings_handler),
                    )
                else:
                    # Failed buttons: ⬇️ (retry) ⚙️
                    row = ui.ActionRow(
                        PButton("⬇️ إعادة المحاولة", discord.ButtonStyle.danger, f"pt_alert_dl_{tracker['id']}_{chapter_num}", tracker["id"], dl_callback),
                        PButton("⚙️ إعدادات", discord.ButtonStyle.secondary, f"pt_alert_set_{tracker['id']}", tracker["id"], self._settings_handler),
                    )
                layout.add_item(row)

                target = await self._get_notification_target(tracker)
                sent = False
                if target:
                    sent = await self._send_notification_to_target(target, content_str, layout)

                if not sent and isinstance(target, discord.TextChannel):
                    try:
                        user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                        if user:
                            dm_content = f"⚠️ (فشل إرسال إشعار التحميل في القناة المخصصة)\n{content_str or ''}"
                            await self._send_notification_to_target(user, dm_content.strip() or None, layout)
                    except Exception as e:
                        logger.warning(f"[PersonalTracker] DM fallback failed for user {uid} during auto-download: {e}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.error(f"Auto-download failed for {title} ch {chapter_num}: {e}")
            finally:
                self._active_downloads.discard(dl_key)

    async def _update_panel(self, user_id: int):
        now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
        last = self._last_panel_update.get(user_id, 0)
        if now_ts - last < 2:
            return
        self._last_panel_update[user_id] = now_ts

        async with self._panel_lock:
            panel_data = await database.get_panel_message(user_id)
            if not panel_data:
                return
            try:
                channel = await self._get_or_fetch_channel(int(panel_data["channel_id"]))
                if not channel:
                    await database.clear_panel_message(user_id)
                    return
                try:
                    msg = await channel.fetch_message(int(panel_data["message_id"]))
                except (discord.NotFound, discord.Forbidden):
                    await database.clear_panel_message(user_id)
                    return

                trackers = await database.get_user_trackers(user_id)
                covers = await self._fetch_covers(trackers)
                view = PersonalTrackerView(user_id, trackers, self, covers, self._lock_cache)
                await msg.edit(view=view)
            except Exception as e:
                logger.warning(f"Failed to update panel message: {e}")

    async def _require_owner(self, interaction: discord.Interaction, tracker_id: int) -> bool:
        tracker = await database.get_user_tracker(tracker_id, interaction.user.id)
        if not tracker:
            await interaction.response.send_message("❌ هذه المتابعة ليست لك.", ephemeral=True)
            return False
        return True

    async def _refresh_handler(self, interaction: discord.Interaction, tracker_id: int):
        if not await self._require_owner(interaction, tracker_id):
            return
        await interaction.response.defer(ephemeral=True)
        tracker = await database.get_user_tracker(tracker_id, interaction.user.id)
        if not tracker:
            await interaction.followup.send("❌ المتابعة غير موجودة", ephemeral=True)
            return

        provider_mgr = getattr(self.bot, "provider_mgr", None)
        if not provider_mgr:
            await interaction.followup.send("❌ Provider manager غير متوفر", ephemeral=True)
            return
        try:
            rich = await asyncio.wait_for(provider_mgr.get_chapters_with_lock_info(tracker["url"]), timeout=25)
            if not rich:
                await interaction.followup.send("❌ الموقع ما رد — جرب لاحقاً", ephemeral=True)
                return
            latest = max(rich.keys())
            new_count = max(0, int(latest - float(tracker.get("last_chapter", 0))))
            locked_count = sum(1 for v in rich.values() if isinstance(v, dict) and v.get("locked"))
            latest_info = rich.get(latest, {})
            locked = latest_info.get("locked", False) if isinstance(latest_info, dict) else False
            self._lock_cache[tracker_id] = {
                "locked": locked,
                "unlock_time": latest_info.get("unlock_time") if isinstance(latest_info, dict) else None,
                "latest_chapter": latest,
            }
            dl_msg = ""
            if new_count > 0:
                can_dl = not locked
                if locked:
                    pm = getattr(self.bot, "provider_mgr", None)
                    if pm and hasattr(pm, "has_auth_cookies") and pm.has_auth_cookies(tracker["url"]):
                        can_dl = True
                if can_dl:
                    chapter_url = latest_info.get("url", "") if isinstance(latest_info, dict) else ""
                    if chapter_url:
                        await self._try_auto_download(tracker, latest, chapter_url, tracker["title"])
                        dl_msg = " ⬇️ جاري التحميل"
            await database.update_user_tracker(tracker_id, interaction.user.id,
                                                last_chapter=latest,
                                                last_checked=datetime.datetime.now(datetime.timezone.utc).isoformat())
            msg = f"✅ **{tracker['title']}** — آخر فصل: `{chapter_label(latest)}`"
            if new_count:
                msg += f" (+{new_count} جديد)"
            if locked_count:
                msg += f" 🔒 {locked_count} مقفول"
            msg += dl_msg
            await interaction.followup.send(msg, ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("❌ انتهت مهلة الفحص — الموقع بطيء أو معطل", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ فشل الفحص: `{str(e)[:150]}`", ephemeral=True)
        await self._update_panel(interaction.user.id)

    async def _browse_handler(self, interaction: discord.Interaction, tracker_id: int):
        if not await self._require_owner(interaction, tracker_id):
            return
        tracker = await database.get_user_tracker(tracker_id, interaction.user.id)
        if not tracker:
            try:
                await interaction.response.send_message("❌ المتابعة غير موجودة.", ephemeral=True)
            except discord.errors.InteractionResponded as e:
                logger.debug(f"Interaction already responded: {e}")
            return
        await interaction.response.defer(ephemeral=True)
        provider_mgr = getattr(self.bot, "provider_mgr", None)
        rich = None
        if provider_mgr:
            try:
                rich = await asyncio.wait_for(provider_mgr.get_chapters_with_lock_info(tracker["url"]), timeout=20)
            except Exception as e:
                logger.warning(f"Failed to fetch chapters for {tracker.get('title')}: {e}")
        if not rich:
            await interaction.followup.send("❌ ما قدرت أجيب الفصول.", ephemeral=True)
            return
        chapters = sorted(
            [(float(k), v if isinstance(v, dict) else {"url": str(v), "locked": False}) for k, v in rich.items()],
            key=lambda x: x[0], reverse=True,
        )
        await self._show_chapter_picker(interaction, chapters, tracker_id, tracker["url"])

    async def _show_chapter_picker(self, interaction: discord.Interaction, chapters: list, tracker_id: int, series_url: str = ""):
        view = PTChapterPickerView(chapters, self, tracker_id, series_url=series_url)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def _remind_handler(self, interaction: discord.Interaction, tracker_id: int):
        if not await self._require_owner(interaction, tracker_id):
            return
        await interaction.response.send_modal(RemindModal(tracker_id, interaction.user.id))

    async def _pause_handler(self, interaction: discord.Interaction, tracker_id: int):
        if not await self._require_owner(interaction, tracker_id):
            return
        tracker = await database.get_user_tracker(tracker_id, interaction.user.id)
        if not tracker:
            try:
                await interaction.response.send_message("❌ المتابعة غير موجودة.", ephemeral=True)
            except discord.errors.InteractionResponded:
                pass
            return
        new_paused = 0 if tracker["paused"] else 1
        await database.update_user_tracker(tracker_id, interaction.user.id, paused=new_paused)
        await interaction.response.send_message(
            f"{'⏸️' if new_paused else '▶️'} تم {'إيقاف' if new_paused else 'استئناف'} تتبع **{tracker['title']}**",
            ephemeral=True,
        )
        await self._update_panel(interaction.user.id)

    async def _channel_handler(self, interaction: discord.Interaction, tracker_id: int):
        if not await self._require_owner(interaction, tracker_id):
            return
        embed = discord.Embed(
            title="🔔 تعيين روم الإشعارات المخصص",
            description="اختر القناة التي ترغب في تلقي تحديثات الفصول وتحميلاتها التلقائية بها من القائمة المنسدلة أدناه.",
            color=C_GOLD,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        view = TrackerChannelSelectView(tracker_id, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def _settings_handler(self, interaction: discord.Interaction, tracker_id: int):
        if not await self._require_owner(interaction, tracker_id):
            return
        tracker = await database.get_user_tracker(tracker_id, interaction.user.id)
        if not tracker:
            try:
                await interaction.response.send_message("❌ المتابعة غير موجودة.", ephemeral=True)
            except discord.errors.InteractionResponded:
                pass
            return
        modal = SettingsModal(
            tracker_id=tracker_id,
            user_id=interaction.user.id,
            current_title=tracker.get("title", ""),
            current_interval=tracker["interval_minutes"],
            current_auto=bool(tracker["auto_download"]),
            current_mention=bool(tracker["mention_on_update"]),
        )
        await interaction.response.send_modal(modal)

    async def _delete_handler(self, interaction: discord.Interaction, tracker_id: int):
        if not await self._require_owner(interaction, tracker_id):
            return
        tracker = await database.get_user_tracker(tracker_id, interaction.user.id)
        if not tracker:
            try:
                await interaction.response.send_message("❌ المتابعة غير موجودة.", ephemeral=True)
            except discord.errors.InteractionResponded:
                pass
            return
        view = DeleteConfirmView(self, tracker_id, interaction.user.id, tracker["title"])
        await interaction.response.send_message(
            content=f"⚠️ هل أنت متأكد من رغبتك في حذف **{tracker['title']}** من متابعاتك؟",
            view=view,
            ephemeral=True
        )

    async def _add_handler(self, interaction: discord.Interaction, _tracker_id: int):
        modal = AddTrackerModal(interaction.user.id)
        await interaction.response.send_modal(modal)

    async def _check_single_tracker(self, t: dict, user_id: int, provider_mgr) -> bool:
        if not provider_mgr:
            return False
        try:
            url = t["url"]
            rich = await asyncio.wait_for(provider_mgr.get_chapters_with_lock_info(url), timeout=20)
            if rich:
                latest = max(rich.keys())
                latest_info = rich.get(latest, {})
                locked = latest_info.get("locked", False) if isinstance(latest_info, dict) else False
                self._lock_cache[t["id"]] = {
                    "locked": locked,
                    "unlock_time": latest_info.get("unlock_time") if isinstance(latest_info, dict) else None,
                    "latest_chapter": latest,
                }
                await database.update_user_tracker(t["id"], user_id, last_chapter=latest)
                return True
        except Exception as e:
            logger.debug(f"Failed checking tracker {t.get('title')} during refresh: {e}")
        return False

    async def _refresh_all_handler(self, interaction: discord.Interaction, _tracker_id: int):
        if not interaction.user:
            return
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        trackers = await database.get_user_trackers(user_id)
        if not trackers:
            await interaction.followup.send("❌ لا توجد متابعات.", ephemeral=True)
            return
        provider_mgr = getattr(self.bot, "provider_mgr", None)
        msg = await interaction.followup.send(f"⏳ يتم الآن فحص المتابعات (0/{len(trackers)})...", ephemeral=True)
        done = 0
        total = len(trackers)
        batch_size = 3
        for i in range(0, total, batch_size):
            batch = trackers[i : i + batch_size]
            tasks = [self._check_single_tracker(t, user_id, provider_mgr) for t in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if r is True:
                    done += 1
            try:
                await msg.edit(content=f"⏳ جاري الفحص... تم إكمال ({min(i + batch_size, total)}/{total})")
            except Exception as e:
                logger.debug(f"Failed to edit progress message: {e}")
            await asyncio.sleep(0.5)

        try:
            await msg.edit(content=f"✅ تم فحص كل المتابعات! نجح تحديث {done}/{total} متابعة.")
        except Exception as e:
            logger.debug(f"Failed to edit final progress message: {e}")
        await self._update_panel(user_id)

    async def _import_from_radar_handler(self, interaction: discord.Interaction, _tracker_id: int):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        guild_ids = {g.id for g in user.mutual_guilds} if hasattr(user, "mutual_guilds") else set()
        if not guild_ids:
            await interaction.followup.send("❌ أنت لست في أي سيرفر مشترك مع البوت.", ephemeral=True)
            return

        try:
            all_radar = await database.get_all_trackers()
        except Exception as e:
            await interaction.followup.send(f"❌ فشل قراءة الرادار القديم: `{e}`", ephemeral=True)
            return

        if not all_radar:
            await interaction.followup.send("❌ لا توجد متتبعات في الرادار القديم.", ephemeral=True)
            return

        existing_urls = {t["url"] for t in await database.get_user_trackers(user.id)}
        imported = 0

        for row in all_radar:
            tid, gid, cid, url, last_ch, msg, ih, last_ck, dl_en, paused, title, mention_str, imin = row
            if gid not in guild_ids:
                continue
            if url in existing_urls:
                continue
            channel_id_str = str(cid) if cid else ""
            try:
                ch = self.bot.get_channel(cid) if cid else None
                if ch:
                    try:
                        perms = ch.permissions_for(interaction.user)
                        if not perms.read_messages:
                            channel_id_str = ""
                    except Exception as e:
                        logger.debug(f"Failed checking channel perms for import: {e}")
                        channel_id_str = ""
            except Exception as e:
                logger.debug(f"Failed fetching channel for import: {e}")
                channel_id_str = ""

            interval = int(imin or 0) if imin else max(5, min(1440, int(ih or 1) * 60))
            await database.add_user_tracker(
                user_id=user.id,
                url=url,
                title=str(title or ""),
                last_chapter=float(last_ch or 0),
                interval_minutes=interval,
                auto_download=int(dl_en or 0),
                notification_channel_id=channel_id_str,
                mention_on_update=1 if mention_str else 0,
            )
            existing_urls.add(url)
            imported += 1

        await interaction.followup.send(f"✅ تم نقل **{imported}** متتبع من الرادار القديم إلى متابعاتك الشخصية.", ephemeral=True)
        await self._update_panel(user.id)

    # track = app_commands.Group(name="track", description="📡 التتبع الشخصي للمسلسلات")

    async def _fetch_single_cover(self, url: str) -> str | None:
        # Check memory cache first
        now = datetime.datetime.now(datetime.timezone.utc)
        if url in self._cover_cache:
            cover, expiry = self._cover_cache[url]
            if now < expiry:
                return cover

        # Not in cache/expired, fetch it
        remote_down = getattr(self.bot, "remote_down", None)
        provider_mgr = getattr(self.bot, "provider_mgr", None)
        cover = None
        if remote_down and remote_down.is_enabled:
            try:
                cover = await asyncio.wait_for(remote_down.radar_cover(url), timeout=8)
            except Exception as e:
                logger.debug(f"Failed to fetch cover from remote worker: {e}")
        if not cover and provider_mgr:
            try:
                cover = await asyncio.wait_for(provider_mgr.get_series_cover(url), timeout=8)
            except Exception as e:
                logger.debug(f"Failed to fetch cover from provider: {e}")

        # Update cache (1 hour TTL)
        self._cover_cache[url] = (cover, now + datetime.timedelta(hours=1))
        return cover

    async def _fetch_covers(self, trackers: list[dict]) -> dict[int, str | None]:
        """يجلب الأغلفة بـ asyncio.gather وتخزين مؤقت."""
        covers: dict[int, str | None] = {}
        fetch_tasks = []
        trackers_to_fetch = []

        for t in trackers[:10]:
            # Try DB first
            db_cover = t.get("cover_url")
            if db_cover:
                covers[t["id"]] = db_cover
                continue

            # Check memory cache (via _fetch_single_cover)
            trackers_to_fetch.append(t)
            fetch_tasks.append(self._fetch_single_cover(t["url"]))

        if fetch_tasks:
            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            for t, result in zip(trackers_to_fetch, results):
                if isinstance(result, Exception):
                    result = None
                covers[t["id"]] = result
                # Update DB to persist cover_url if we found one
                if result:
                    await database.update_user_tracker(t["id"], t["user_id"], cover_url=result)

        return covers

    @app_commands.command(name="mytracker", description="فتح لوحة التتبع الشخصي السريعة الخاصة بك")
    @user_only()
    async def track_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        trackers = await database.get_user_trackers(user_id)
        covers = await self._fetch_covers(trackers)
        view = PersonalTrackerView(user_id, trackers, self, covers, self._lock_cache)
        await interaction.followup.send(view=view, ephemeral=True)
        msg = await interaction.original_response()
        await database.set_panel_message(user_id, interaction.channel_id, msg.id)

    # track list command (disabled slash command decorator)
    @user_only()
    async def track_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        trackers = await database.get_user_trackers(user_id)
        if not trackers:
            await interaction.followup.send("📭 لا توجد متابعات.\nاستخدم `/track panel` للإضافة.", ephemeral=True)
            return
        
        total = len(trackers)
        paused_count = sum(1 for t in trackers if t["paused"])
        active_count = total - paused_count
        locked_count = sum(1 for t in trackers if self._lock_cache.get(t["id"], {}).get("locked", False))
        
        stats_line = (
            f"📊 **إحصائيات المتابعة:**\n"
            f"├─ إجمالي السلاسل: `{total}`\n"
            f"├─ نشط: `🟢 {active_count}`\n"
            f"├─ متوقف مؤقتاً: `⏸️ {paused_count}`\n"
            f"└─ فصول مغلقة: `🔒 {locked_count}`\n"
        )
        
        lines = []
        for i, t in enumerate(trackers, 1):
            paused = bool(t["paused"])
            lc = self._lock_cache.get(t["id"], {})
            locked = lc.get("locked", False)
            icon = "⏸️" if paused else ("🔒" if locked else "🟢")
            
            lc_label = chapter_label(t["last_chapter"])
            domain = display_name(t["url"])
            title = (t["title"] or "بدون عنوان")[:40]
            lines.append(
                f"{icon} **{i:02d}.** **[{title}]({t['url']})**\n"
                f"└─ آخر فصل: `{lc_label}` · المصدر: `{domain}` · تلقائي: `{'✅' if t['auto_download'] else '❌'}`"
            )
            
        embed = discord.Embed(
            title=f"📋 قائمة المتابعات الشخصية ({total})",
            description=stats_line + "\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(lines),
            color=C_GOLD,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.set_footer(text=f"طلب بواسطة {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @track_panel.error
    async def track_panel_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            msg = "❌ هذا الأمر للمستخدمين فقط."
        else:
            msg = f"❌ حدث خطأ: `{error}`"
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except discord.errors.InteractionResponded:
            await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PersonalTrackerCog(bot))
