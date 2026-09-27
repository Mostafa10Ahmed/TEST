from __future__ import annotations

import datetime

import database
import discord

from services.intervals import format_check_interval, parse_check_interval


def _row_interval_label(r: tuple) -> str:
    if len(r) > 12 and r[12]:
        return format_check_interval(int(r[12]))
    return format_check_interval(max(60, int(r[6] or 1) * 60))

C_BLUE = discord.Color.from_rgb(88, 101, 242)
C_GREEN = discord.Color.from_rgb(35, 165, 89)
C_RED = discord.Color.from_rgb(242, 63, 66)
C_GOLD = discord.Color.from_rgb(255, 184, 0)


def _lbl(num: float) -> str:
    return str(int(num)) if float(num).is_integer() else str(num)


def _series_name(url: str) -> str:
    if "?" in url:
        url = url.split("?")[0]
    if "#" in url:
        url = url.split("#")[0]
    parts = [p for p in url.rstrip("/").split("/") if p]
    ignored = {"status", "detail", "chapters", "list", "webtoon", "manga", "series"}
    while parts and parts[-1].lower() in ignored:
        parts.pop()
    return parts[-1].replace("-", " ").replace("_", " ").title() if parts else "Series"


class RadarAddModal(discord.ui.Modal, title="📡 إضافة متتبع (Radar v2)"):
    url = discord.ui.TextInput(
        label="رابط السلسلة",
        placeholder="https://site.com/series/xxx",
        required=True,
        max_length=1024,
    )
    check_interval = discord.ui.TextInput(
        label="فترة الفحص (30m · 2h · 90)",
        placeholder="30m",
        required=True,
        max_length=12,
        default="1h",
    )
    current_chapter = discord.ui.TextInput(
        label="الفصل الحالي", placeholder="0", required=True, max_length=20, default="0"
    )
    custom_message = discord.ui.TextInput(
        label="رسالة مرفقة (اختياري)", required=False, max_length=180, default=""
    )
    auto_download = discord.ui.TextInput(
        label="تحميل تلقائي؟ (yes/no)", required=False, max_length=8, default="no"
    )

    def __init__(self, guild_id: int, channel_id: int, parent_view: RadarPanelV2View):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            interval_minutes = parse_check_interval(str(self.check_interval.value))
        except ValueError:
            await interaction.followup.send(
                "❌ فترة غير صالحة. أمثلة: `30m` · `2h` · `90`",
                ephemeral=True,
            )
            return
        interval_hours = max(1, (interval_minutes + 59) // 60)

        try:
            cur = float(str(self.current_chapter.value).strip())
        except Exception:
            await interaction.followup.send(
                "❌ current_chapter غير صالح.", ephemeral=True
            )
            return

        url = str(self.url.value).strip()
        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        if provider_mgr:
            try:
                url = await provider_mgr.resolve_series_url_async(url)
            except Exception:
                pass
        ad = str(self.auto_download.value or "").strip().lower()
        auto_dl = 1 if ad in ("1", "true", "yes", "y", "on") else 0
        mention_str = interaction.user.mention

        # Get actual series title from provider manager
        title = None
        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        if provider_mgr:
            try:
                import asyncio
                title = await asyncio.wait_for(provider_mgr.get_series_title(url), timeout=15)
            except Exception:
                pass
        if not title:
            title = _series_name(url)

        # Create subscription role automatically
        role_name = f"🔔 Sub: {title[:80]}"
        guild = interaction.guild
        if guild:
            role = discord.utils.get(guild.roles, name=role_name)
            if not role:
                try:
                    await guild.create_role(
                        name=role_name,
                        mentionable=True,
                        reason=f"Auto-created subscription role for manga tracker: {title}"
                    )
                except Exception as e:
                    print(f"[Radar Panel] Failed to create role {role_name}: {e}")

        await database.add_tracker(
            self.guild_id,
            self.channel_id,
            url,
            str(self.custom_message.value or "").strip(),
            interval_hours,
            cur,
            auto_dl,
            title,
            mention_str,
            interval_minutes=interval_minutes,
        )

        # Update the parent view lists
        self.parent_view.rows = [
            r for r in await database.get_all_trackers() if r[1] == self.guild_id
        ]
        self.parent_view._rebuild()
        
        em = discord.Embed(
            title="✅ تم إضافة المتتبع بنجاح",
            color=C_GREEN,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        em.add_field(name="Series", value=title, inline=False)
        em.add_field(name="Channel", value=f"<#{self.channel_id}>", inline=True)
        em.add_field(
            name="Interval", value=format_check_interval(interval_minutes), inline=True
        )
        em.add_field(name="Start at", value=f"Ch. {_lbl(cur)}", inline=True)
        em.add_field(name="AutoDL", value="✅" if auto_dl else "❌", inline=True)
        em.add_field(name="Notify", value=mention_str, inline=True)
        
        await interaction.followup.send(embed=em, ephemeral=True)
        try:
            await interaction.message.edit(view=self.parent_view)
        except Exception:
            pass


class RadarIntervalModal(discord.ui.Modal, title="⏱️ تعديل فترة الفحص"):
    check_interval = discord.ui.TextInput(
        label="فترة الفحص الجديدة (30m · 2h · 90)",
        placeholder="30m",
        required=True,
        max_length=12,
    )

    def __init__(self, tracker_id: int, guild_id: int, parent_view: RadarPanelV2View):
        super().__init__()
        self.tracker_id = tracker_id
        self.guild_id = guild_id
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            interval_minutes = parse_check_interval(str(self.check_interval.value))
        except ValueError:
            await interaction.response.send_message(
                "❌ فترة غير صالحة. أمثلة: `30m` · `2h` · `90`",
                ephemeral=True,
            )
            return
        interval_hours = max(1, (interval_minutes + 59) // 60)

        await database.update_tracker(
            self.tracker_id,
            self.guild_id,
            interval_hours=interval_hours,
            interval_minutes=interval_minutes,
        )

        # Reload the view rows
        self.parent_view.rows = [
            r for r in await database.get_all_trackers() if r[1] == self.guild_id
        ]
        self.parent_view._rebuild()

        await interaction.response.edit_message(view=self.parent_view)
        await interaction.followup.send("✅ تم تحديث فترة الفحص بنجاح.", ephemeral=True)


class TrackerSelect(discord.ui.Select):
    def __init__(self, rows: list[tuple], selected_tracker_id: int | None = None):
        options: list[discord.SelectOption] = []
        for r in rows[:25]:
            tid = r[0]
            url = r[3]
            paused = int(r[9] or 0) if len(r) > 9 else 0
            prefix = "⏸️" if paused else "📡"
            title = r[10] if len(r) > 10 and r[10] else _series_name(url)
            
            is_default = (tid == selected_tracker_id)
            options.append(
                discord.SelectOption(
                    label=f"{prefix} {tid} · {title[:70]}",
                    value=str(tid),
                    default=is_default
                )
            )
        super().__init__(
            placeholder="اختر متتبعاً لإدارته...", min_values=1, max_values=1, options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: RadarPanelV2View = self.view  # type: ignore
        view.selected_tracker_id = int(self.values[0])
        view._rebuild()
        await interaction.response.edit_message(view=view)


class RadarChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="اختر روم الإشعارات للمتتبع المختار أو الجديد...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: RadarPanelV2View = self.view  # type: ignore
        if self.values:
            view.selected_channel_id = int(self.values[0].id)
            
            # If a tracker is currently selected, update its notify channel instantly in the DB
            if view.selected_tracker_id:
                await database.update_tracker(
                    view.selected_tracker_id,
                    view.guild_id,
                    channel_id=view.selected_channel_id
                )
                view.rows = [
                    r for r in await database.get_all_trackers() if r[1] == view.guild_id
                ]
                await interaction.response.send_message(
                    f"✅ تم تغيير قناة إشعارات المتتبع إلى <#{view.selected_channel_id}>",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"✅ تم تحديد روم الإشعارات للمتتبعات الجديدة: <#{view.selected_channel_id}>",
                    ephemeral=True
                )
                
        view._rebuild()
        await interaction.message.edit(view=view)


class RadarPanelButton(discord.ui.Button):
    def __init__(
        self,
        label: str,
        style: discord.ButtonStyle,
        callback_func,
        emoji: str | None = None,
    ):
        super().__init__(label=label, style=style, emoji=emoji)
        self.callback_func = callback_func

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.callback_func(interaction)


class RadarPanelV2View(discord.ui.LayoutView):
    def __init__(self, guild_id: int, rows: list[tuple]):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.rows = rows
        self.selected_channel_id: int | None = None
        self.selected_tracker_id: int | None = None
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        container = discord.ui.Container(accent_color=C_GOLD)
        
        # 1. Statistics
        total_trackers = len(self.rows)
        paused_count = sum(1 for r in self.rows if len(r) > 9 and r[9])
        autodl_count = sum(1 for r in self.rows if len(r) > 8 and r[8])
        
        desc = (
            f"**📡 إحصاءات الرادار بالسيرفر:**\n"
            f"• إجمالي المتتبعات: `{total_trackers}`\n"
            f"• متوقفة مؤقتاً: `{paused_count}` ⏸️\n"
            f"• تحميل تلقائي نشط: `{autodl_count}` 📥\n"
        )
        
        if self.selected_channel_id:
            desc += f"🔔 قناة الإشعارات المحددة: <#{self.selected_channel_id}>\n"
            
        desc += "\n"
        
        # Details of the selected tracker
        selected_row = None
        if self.selected_tracker_id:
            selected_row = next((r for r in self.rows if r[0] == self.selected_tracker_id), None)
            
        if selected_row:
            tid = selected_row[0]
            cid = selected_row[2]
            url = selected_row[3]
            lch = selected_row[4]
            iv = _row_interval_label(selected_row)
            dl = selected_row[8] if len(selected_row) > 8 else 0
            paused = selected_row[9] if len(selected_row) > 9 else 0
            title = selected_row[10] if len(selected_row) > 10 and selected_row[10] else _series_name(url)
            mention_str = selected_row[11] if len(selected_row) > 11 else ""
            
            desc += (
                f"📋 **بيانات المتتبع المختار:**\n"
                f"**الاسم**: `{title}`\n"
                f"**الرابط**: [اضغط لتصفح الموقع]({url})\n"
                f"**الفصل الحالي**: `Ch. {_lbl(lch)}`\n"
                f"**فترة الفحص**: `{iv}`\n"
                f"**التحميل التلقائي**: `{'مفعل ✅' if dl else 'معطل ❌'}`\n"
                f"**حالة التتبع**: `{'⏸️ متوقف مؤقتاً' if paused else '🟢 نشط ويعمل'}`\n"
                f"**قناة الإشعارات**: <#{cid}>\n"
                f"**المنشن المرفق**: {mention_str if mention_str else 'لا يوجد منشن'}\n"
            )
        else:
            desc += "💡 اختر متتبعاً من القائمة أدناه لعرض خياراته وإدارته أو أضف متتبعاً جديداً."

        container.add_item(discord.ui.TextDisplay(f"# 📡 لوحة إدارة الرادار الموحدة\n{desc}"))

        # Row 1: Channel selection dropdown
        container.add_item(discord.ui.ActionRow(RadarChannelSelect()))

        # Row 2: Tracker selection dropdown
        if self.rows:
            container.add_item(discord.ui.ActionRow(TrackerSelect(self.rows, self.selected_tracker_id)))

        # Row 3: Action Buttons (Add, Pause/Resume, Auto-DL)
        row_actions = discord.ui.ActionRow()
        row_actions.add_item(RadarPanelButton("Add Tracker", discord.ButtonStyle.success, self.add_btn, emoji="➕"))
        
        btn_pause = RadarPanelButton("Pause/Resume", discord.ButtonStyle.secondary, self.pause_btn, emoji="⏸️")
        btn_autodl = RadarPanelButton("Auto-DL", discord.ButtonStyle.primary, self.autodl_btn, emoji="📥")
        
        if not self.selected_tracker_id:
            btn_pause.disabled = True
            btn_autodl.disabled = True
            
        row_actions.add_item(btn_pause)
        row_actions.add_item(btn_autodl)
        container.add_item(row_actions)

        # Row 4: Action Buttons (Change Interval, Remove, Refresh)
        row_actions2 = discord.ui.ActionRow()
        btn_interval = RadarPanelButton("Interval", discord.ButtonStyle.secondary, self.interval_btn, emoji="⏱️")
        btn_remove = RadarPanelButton("Remove", discord.ButtonStyle.danger, self.remove_btn, emoji="🗑️")
        
        if not self.selected_tracker_id:
            btn_interval.disabled = True
            btn_remove.disabled = True
            
        row_actions2.add_item(btn_interval)
        row_actions2.add_item(btn_remove)
        row_actions2.add_item(RadarPanelButton("Refresh", discord.ButtonStyle.secondary, self.refresh_btn, emoji="🔄"))
        container.add_item(row_actions2)

        self.add_item(container)

    async def add_btn(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message(
                "❌ هذا الأمر داخل سيرفر فقط.", ephemeral=True
            )
            return
        channel_id = self.selected_channel_id or getattr(
            interaction.channel, "id", None
        )
        if not channel_id:
            await interaction.response.send_message(
                "❌ يرجى تحديد روم إشعارات أولاً.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            RadarAddModal(interaction.guild_id, int(channel_id), self)
        )

    async def pause_btn(self, interaction: discord.Interaction) -> None:
        if not self.selected_tracker_id:
            return
        tr = await database.get_tracker(self.selected_tracker_id, self.guild_id)
        if not tr:
            await interaction.response.send_message("❌ المتتبع غير موجود.", ephemeral=True)
            return
        paused = int(tr[9] or 0)
        new_paused = 0 if paused else 1
        await database.set_tracker_paused(self.selected_tracker_id, self.guild_id, new_paused)
        
        self.rows = [
            r for r in await database.get_all_trackers() if r[1] == self.guild_id
        ]
        self._rebuild()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"✅ تم {'تنشيط' if not new_paused else 'إيقاف'} التتبع مؤقتاً.",
            ephemeral=True
        )

    async def autodl_btn(self, interaction: discord.Interaction) -> None:
        if not self.selected_tracker_id:
            return
        tr = await database.get_tracker(self.selected_tracker_id, self.guild_id)
        if not tr:
            await interaction.response.send_message("❌ المتتبع غير موجود.", ephemeral=True)
            return
        dl = int(tr[8] or 0)
        new_dl = 0 if dl else 1
        await database.update_tracker(self.selected_tracker_id, self.guild_id, download_enabled=new_dl)
        
        self.rows = [
            r for r in await database.get_all_trackers() if r[1] == self.guild_id
        ]
        self._rebuild()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"✅ تم {'تفعيل' if new_dl else 'إلغاء'} التحميل التلقائي للمتتبع المختار.",
            ephemeral=True
        )

    async def interval_btn(self, interaction: discord.Interaction) -> None:
        if not self.selected_tracker_id:
            return
        await interaction.response.send_modal(
            RadarIntervalModal(self.selected_tracker_id, self.guild_id, self)
        )

    async def remove_btn(self, interaction: discord.Interaction) -> None:
        if not self.selected_tracker_id:
            return
        ok = await database.remove_tracker(self.selected_tracker_id, self.guild_id)
        self.selected_tracker_id = None
        self.rows = [
            r for r in await database.get_all_trackers() if r[1] == self.guild_id
        ]
        self._rebuild()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "✅ تم حذف المتتبع بنجاح." if ok else "❌ فشل حذف المتتبع.",
            ephemeral=True
        )

    async def refresh_btn(self, interaction: discord.Interaction) -> None:
        self.rows = [
            r for r in await database.get_all_trackers() if r[1] == self.guild_id
        ]
        self._rebuild()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("🔄 تم تحديث قائمة المتتبعات والبيانات.", ephemeral=True)
