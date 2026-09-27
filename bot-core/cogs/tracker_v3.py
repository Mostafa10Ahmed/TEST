"""
cogs/tracker_v3.py — Unified server-wide manga tracker (v3).

Commands:
  /tracker   — لوحة تتبع المانجا التفاعلية الشاملة (إضافة، تعديل، فحص، حذف)
"""
from __future__ import annotations

import asyncio
import logging
import re
import json
import datetime
import io
from typing import Optional


import discord
from discord import app_commands
from discord.ext import commands

import database
from services.tracker_engine_v3 import TrackerEngineV3
from ui.tracker_containers_v3 import (
    build_panel_list,
    build_panel_empty,
    build_tracker_detail,
    build_new_chapter_alert,
    build_download_complete,
    _slug_to_name,
    _domain,
    _ch_label,
)
from user_system import check_rank

logger = logging.getLogger("sv3.cog")

MAX_TRACKERS_PER_GUILD = 50


def _tracker_status_icon(tracker: dict) -> str:
    return "⏸️" if tracker.get("paused") else "🟢"


def _tracker_mode_label(tracker: dict) -> str:
    return "تلقائي" if tracker.get("auto_download") else "يدوي"


def _tracker_domain(tracker: dict) -> str:
    return _domain(tracker.get("url", ""))


async def _resolve_tracker_draft(bot: commands.Bot, engine: TrackerEngineV3 | None, interaction: discord.Interaction, url: str) -> dict:
    draft = {
        "url": url,
        "title": None,
        "cover_url": None,
        "last_chapter": 0.0,
        "notification_channel_id": str(interaction.channel_id or interaction.channel.id if interaction.channel else ""),
        "mention_role_id": None,
        "auto_download": 1,
    }

    provider_mgr = getattr(interaction.client, "provider_mgr", None)
    if provider_mgr:
        try:
            title = await asyncio.wait_for(provider_mgr.get_series_title(url), timeout=15)
            if title:
                draft["title"] = str(title).strip()
        except Exception:
            pass
        try:
            cover = await asyncio.wait_for(provider_mgr.get_series_cover(url), timeout=10)
            if cover and str(cover).startswith("http"):
                draft["cover_url"] = str(cover)
        except Exception:
            pass

    if engine:
        try:
            result = await engine._fetch_latest(url)
            if result:
                draft["last_chapter"] = float(result[0] or 0)
        except Exception:
            pass

    if not draft["title"]:
        draft["title"] = _slug_to_name(url)

    return draft


# ═══════════════════════════════════════════════════════════════════════════════
# Dynamic Permissions Helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def has_tracker_privilege(interaction: discord.Interaction, tracker: dict) -> bool:
    """يفحص صلاحية تعديل تراكر معين."""
    if interaction.user.id == interaction.guild.owner_id:
        return True
    perms = interaction.user.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    
    # تحقق من الرتب المخصصة للتراكر
    admin_roles_str = tracker.get("admin_roles", "[]") or "[]"
    try:
        roles_list = json.loads(admin_roles_str)
    except Exception:
        roles_list = []
        
    if roles_list:
        user_role_ids = [str(r.id) for r in interaction.user.roles]
        if any(rid in user_role_ids for rid in roles_list):
            return True
            
    return False


async def has_global_admin_privilege(interaction: discord.Interaction) -> bool:
    """يفحص ما إذا كان المستخدم يملك صلاحية إدارة التتبع بشكل عام في السيرفر."""
    if interaction.user.id == interaction.guild.owner_id:
        return True
    perms = interaction.user.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    
    # تحقق من الرتب العالمية لإدارة البوت في السيرفر
    global_roles_str = await database.get_setting(f"guild_admin_roles_{interaction.guild_id}", "[]")
    try:
        global_list = json.loads(global_roles_str)
    except Exception:
        global_list = []
        
    user_role_ids = [str(r.id) for r in interaction.user.roles]
    if global_list and any(rid in user_role_ids for rid in global_list):
        return True

    # أو إذا كان مسموحاً له بإدارة أي تراكر
    db = await database._get_db()
    async with db.execute("SELECT admin_roles FROM server_trackers WHERE guild_id=?", (interaction.guild_id,)) as c:
        rows = await c.fetchall()
        for r in rows:
            if r[0]:
                try:
                    roles_list = json.loads(r[0])
                    if any(rid in user_role_ids for rid in roles_list):
                        return True
                except Exception:
                    pass
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Tracker Dropdown Select component
# ═══════════════════════════════════════════════════════════════════════════════

class TrackerSelect(discord.ui.Select):
    def __init__(self, trackers: list[dict]):
        options = []
        for t in trackers[:25]:
            title = t.get("title") or _slug_to_name(t.get("url", ""))
            chapter = _ch_label(t.get("last_chapter", 0))
            domain = _tracker_domain(t)
            status = "موقوف" if t.get("paused") else "نشط"
            tid = t.get("id") or t.get("tracker_id")
            options.append(discord.SelectOption(
                label=f"{_tracker_status_icon(t)} {title[:22]}",
                description=f"{status} · Ch. {chapter} · {domain}"[:100],
                value=str(tid)
            ))
        super().__init__(placeholder="اختر سلسلة لعرض التفاصيل والتعديل...", options=options)

    async def callback(self, interaction: discord.Interaction):
        tid = int(self.values[0])
        view: PanelView = self.view  # type: ignore
        if view.mode == "guild":
            tracker = await database.sv3_get(tid, interaction.guild_id)
        else:
            tracker = await database.get_user_tracker(tid, interaction.user.id)
            
        if not tracker:
            return await interaction.response.send_message("❌ لم يتم العثور على التراكر.", ephemeral=True)
        
        subs = await database.get_series_subscribers(tid)
        detail_view = TrackerDetailView(interaction.guild_id, interaction.guild.name, view.engine, tracker, parent_view=view, sub_count=len(subs))
        await detail_view.refresh()
        await interaction.response.edit_message(view=detail_view)


# ═══════════════════════════════════════════════════════════════════════════════
# Bulk Import / Export Components
# ═══════════════════════════════════════════════════════════════════════════════

class BulkImportModal(discord.ui.Modal, title="📥 استيراد قائمة التتبع (JSON / URLs)"):
    json_input = discord.ui.TextInput(
        label="بيانات JSON أو قائمة روابط (رابط لكل سطر)",
        style=discord.TextStyle.paragraph,
        placeholder='[{"url": "https://site.com/series/name", "title": "Series Name"}]',
        required=True,
        max_length=4000,
    )

    def __init__(self, parent_view: "PanelView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raw_text = str(self.json_input.value or "").strip()
        imported = 0
        failed = 0
        items = []

        if raw_text.startswith("[") and raw_text.endswith("]"):
            try:
                items = json.loads(raw_text)
            except Exception:
                pass
        if not items:
            lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
            for l in lines:
                if l.startswith("http"):
                    items.append({"url": l})

        for item in items:
            url = item.get("url") if isinstance(item, dict) else str(item)
            if not url or not str(url).startswith("http"):
                continue
            title = item.get("title") if isinstance(item, dict) else ""
            if not title:
                title = _slug_to_name(url)
            last_ch = float(item.get("last_chapter", 0.0)) if isinstance(item, dict) else 0.0
            auto_dl = int(item.get("auto_download", 1)) if isinstance(item, dict) else 1

            if self.parent_view.mode == "guild":
                res = await database.sv3_add(
                    guild_id=interaction.guild_id,
                    url=url,
                    notification_channel_id=str(interaction.channel_id),
                    title=title,
                    added_by_user_id=interaction.user.id,
                    last_chapter=last_ch,
                    auto_download=auto_dl,
                )
                if res: imported += 1
                else: failed += 1
            else:
                try:
                    await database.add_user_tracker(
                        user_id=interaction.user.id,
                        url=url,
                        title=title,
                        last_chapter=last_ch,
                        auto_download=auto_dl,
                        notification_channel_id=str(interaction.channel_id),
                    )
                    imported += 1
                except Exception:
                    failed += 1

        await self.parent_view.refresh()
        await interaction.followup.send(
            f"📦 **نتائج الاستيراد:**\n✅ تم إدراج `{imported}` سلسلة.\n❌ فشل/مكرر `{failed}` سلسلة.",
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Interactive Panel Views
# ═══════════════════════════════════════════════════════════════════════════════

class PanelView(discord.ui.LayoutView):
    """لوحة التتبع الرئيسية الموحدة (Guild & Personal Control Center)."""

    def __init__(self, guild_id: int, guild_name: str, engine: "TrackerEngineV3", user_id: int | None = None, mode: str = "guild"):
        super().__init__(timeout=600)
        self.guild_id   = guild_id
        self.guild_name = guild_name
        self.engine     = engine
        self.bot        = engine.bot
        self.user_id    = user_id
        self.mode       = mode  # "guild" or "personal"
        self.page       = 0
        self.page_size  = 25
        self._trackers: list[dict] = []

    async def refresh(self) -> None:
        if self.mode == "guild":
            self._trackers = await database.sv3_list(self.guild_id)
        else:
            uid = self.user_id or 0
            self._trackers = await database.get_user_trackers(uid)
        self.clear_items()
        self._build()

    def _build(self) -> None:
        total = len(self._trackers)
        total_pages = max(1, (total + self.page_size - 1) // self.page_size)
        self.page = max(0, min(self.page, total_pages - 1))
        page_start = self.page * self.page_size
        page_end = min(total, page_start + self.page_size)
        page_items = self._trackers[page_start:page_end]

        header_title = f"🌐 متابعات السيرفر — {self.guild_name}" if self.mode == "guild" else "👤 المتابعات الشخصية"

        if not self._trackers:
            empty_container = build_panel_empty()
            empty_container.children[0] = discord.ui.TextDisplay(
                f"# {header_title}\n\n"
                f"لا توجد متابعات {'في هذا السيرفر' if self.mode == 'guild' else 'في قائمة المتابعة الشخصية'}.\n"
                "استخدم زر `➕ إضافة تتبع` لإنشاء أول متابعة."
            )
            self.add_item(empty_container)
        else:
            container = build_panel_list(
                self._trackers,
                page=self.page,
                per_page=self.page_size,
                guild_name=header_title,
            )
            self.add_item(container)
            if page_items:
                self.add_item(TrackerSelect(page_items))

        btn_prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
        btn_page = discord.ui.Button(label=f"{self.page + 1}/{total_pages}", style=discord.ButtonStyle.secondary, disabled=True)
        btn_next = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, disabled=self.page >= total_pages - 1)
        btn_mode = discord.ui.Button(
            label="👤 Personal Trackers" if self.mode == "guild" else "🌐 Guild Trackers",
            style=discord.ButtonStyle.primary,
        )
        btn_import_export = discord.ui.Button(label="📦 Bulk Import/Export", style=discord.ButtonStyle.secondary)
        btn_close = discord.ui.Button(label="إغلاق", style=discord.ButtonStyle.danger)
        btn_add = discord.ui.Button(label="➕ إضافة تتبع", style=discord.ButtonStyle.success)
        btn_refresh = discord.ui.Button(label="🔄 تحديث", style=discord.ButtonStyle.primary)

        async def prev_cb(interaction: discord.Interaction):
            if self.page > 0:
                self.page -= 1
            await self.refresh()
            await interaction.response.edit_message(view=self)

        async def next_cb(interaction: discord.Interaction):
            total_pages_local = max(1, (len(self._trackers) + self.page_size - 1) // self.page_size)
            if self.page < total_pages_local - 1:
                self.page += 1
            await self.refresh()
            await interaction.response.edit_message(view=self)

        async def mode_cb(interaction: discord.Interaction):
            self.mode = "personal" if self.mode == "guild" else "guild"
            self.user_id = interaction.user.id
            self.page = 0
            await self.refresh()
            await interaction.response.edit_message(view=self)

        async def import_export_cb(interaction: discord.Interaction):
            parent_view = self
            class ImportExportOptionView(discord.ui.LayoutView):
                def __init__(self):
                    super().__init__(timeout=180)
                    c = discord.ui.Container(accent_color=discord.Color.from_rgb(88, 101, 242))
                    c.add_item(discord.ui.TextDisplay("# 📦 Bulk JSON Import / Export\nاختر الإجراء المطلوب:"))
                    self.add_item(c)

                @discord.ui.button(label="📤 Export JSON", style=discord.ButtonStyle.primary)
                async def export_btn(self, inter: discord.Interaction, _: discord.ui.Button):
                    await inter.response.defer(ephemeral=True)
                    trackers = parent_view._trackers
                    if not trackers:
                        await inter.followup.send("❌ لا توجد أي سلاسل متتبعة للتصدير.", ephemeral=True)
                        return
                    export_list = []
                    for t in trackers:
                        export_list.append({
                            "url": t.get("url"),
                            "title": t.get("title"),
                            "last_chapter": t.get("last_chapter", 0.0),
                            "cover_url": t.get("cover_url"),
                            "auto_download": t.get("auto_download", 1),
                            "paused": t.get("paused", 0),
                        })
                    b_data = json.dumps(export_list, indent=2, ensure_ascii=False).encode("utf-8")
                    file = discord.File(io.BytesIO(b_data), filename="trackers_export.json")
                    await inter.followup.send(
                        content=f"📤 **تم تصدير {len(export_list)} سلسلة في الملف المرفق:**",
                        file=file,
                        ephemeral=True
                    )

                @discord.ui.button(label="📥 Import JSON", style=discord.ButtonStyle.success)
                async def import_btn(self, inter: discord.Interaction, _: discord.ui.Button):
                    await inter.response.send_modal(BulkImportModal(parent_view))

            await interaction.response.send_message(view=ImportExportOptionView(), ephemeral=True)

        async def close_cb(interaction: discord.Interaction):
            await interaction.response.edit_message(
                content="✅ تم إغلاق لوحة التتبع.",
                view=None,
                embed=None,
            )

        async def refresh_cb(interaction: discord.Interaction):
            await self.refresh()
            await interaction.response.edit_message(view=self)

        async def add_cb(interaction: discord.Interaction):
            await interaction.response.send_modal(AddTrackerModal(self))

        btn_prev.callback = prev_cb
        btn_next.callback = next_cb
        btn_mode.callback = mode_cb
        btn_import_export.callback = import_export_cb
        btn_close.callback = close_cb
        btn_refresh.callback = refresh_cb
        btn_add.callback = add_cb

        row1 = discord.ui.ActionRow(btn_prev, btn_page, btn_next, btn_mode)
        row2 = discord.ui.ActionRow(btn_add, btn_import_export, btn_refresh, btn_close)
        self.add_item(row1)
        self.add_item(row2)



# ═══════════════════════════════════════════════════════════════════════════════
# Add Tracker Wizard
# ═══════════════════════════════════════════════════════════════════════════════

class AddTrackerModal(discord.ui.Modal, title="➕ إضافة تتبع جديد"):
    url = discord.ui.TextInput(
        label="رابط العمل / الفصل",
        placeholder="https://site.com/series/...",
        required=True,
        max_length=500,
    )

    def __init__(self, parent_view: "PanelView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        url_val = str(self.url.value or "").strip()
        if not url_val.startswith("http"):
            await interaction.followup.send("❌ أرسل رابط صحيح يبدأ بـ http أو https.", ephemeral=True)
            return

        all_trackers = await database.sv3_list(interaction.guild_id)
        if len(all_trackers) >= MAX_TRACKERS_PER_GUILD:
            await interaction.followup.send(
                f"❌ تم بلوغ الحد الأقصى للمتابعات في هذا السيرفر ({MAX_TRACKERS_PER_GUILD}).",
                ephemeral=True,
            )
            return

        if any(t["url"] == url_val for t in all_trackers):
            await interaction.followup.send("❌ هذه السلسلة مضافة للتتبع بالفعل في هذا السيرفر.", ephemeral=True)
            return

        draft = await _resolve_tracker_draft(self.parent_view.bot, self.parent_view.engine, interaction, url_val)
        preview = TrackerDraftView(parent_view=self.parent_view, draft=draft)
        await preview.refresh()
        await interaction.followup.send(
            "✅ تم التقاط الرابط. راجع المعاينة التالية ثم أكّد الإضافة أو عدّل البيانات إذا لزم الأمر.",
            view=preview,
            ephemeral=True,
        )


class TrackerDraftEditModal(discord.ui.Modal, title="✏️ تعديل بيانات التتبع"):
    title_override = discord.ui.TextInput(
        label="اسم العمل",
        required=False,
        max_length=150,
    )
    channel = discord.ui.TextInput(
        label="روم الإشعارات (ID أو الاسم)",
        required=False,
        max_length=100,
    )
    role = discord.ui.TextInput(
        label="رول المنشن (ID أو الاسم)",
        required=False,
        max_length=100,
    )
    auto_dl = discord.ui.TextInput(
        label="تحميل تلقائي؟ (yes / no)",
        required=False,
        default="yes",
        max_length=10,
    )

    def __init__(self, draft_view: "TrackerDraftView"):
        super().__init__()
        self.draft_view = draft_view
        self.title_override.default = str(draft_view.draft.get("title") or "")
        self.channel.default = str(draft_view.draft.get("notification_channel_id") or "")
        self.role.default = str(draft_view.draft.get("mention_role_id") or "")
        self.auto_dl.default = "yes" if int(draft_view.draft.get("auto_download", 1)) else "no"

    async def on_submit(self, interaction: discord.Interaction):
        draft = self.draft_view.draft
        title = str(self.title_override.value or "").strip()
        channel_val = str(self.channel.value or "").strip()
        role_val = str(self.role.value or "").strip()
        auto_dl_val = str(self.auto_dl.value or "yes").strip().lower() in ("yes", "y", "true", "1")

        if title:
            draft["title"] = title

        if channel_val:
            channel_id = None
            if channel_val.isdigit():
                channel_id = channel_val
            else:
                chan = discord.utils.get(interaction.guild.text_channels, name=channel_val)
                if chan:
                    channel_id = str(chan.id)
            if channel_id:
                draft["notification_channel_id"] = channel_id

        if role_val:
            role_id = None
            if role_val.isdigit():
                role_id = role_val
            else:
                role = discord.utils.get(interaction.guild.roles, name=role_val)
                if role:
                    role_id = str(role.id)
            draft["mention_role_id"] = role_id

        draft["auto_download"] = 1 if auto_dl_val else 0
        draft["edited"] = True
        await self.draft_view.refresh()
        await interaction.response.edit_message(view=self.draft_view)


class TrackerDraftView(discord.ui.LayoutView):
    def __init__(self, parent_view: "PanelView", draft: dict):
        super().__init__(timeout=600)
        self.parent_view = parent_view
        self.draft = draft

    async def refresh(self) -> None:
        self.clear_items()

        title = self.draft.get("title") or _slug_to_name(self.draft["url"])
        source = _domain(self.draft["url"])
        chapter = self.draft.get("last_chapter", 0.0) or 0.0
        channel_id = self.draft.get("notification_channel_id") or ""
        role_id = self.draft.get("mention_role_id") or ""
        auto_dl = bool(self.draft.get("auto_download", 1))
        cover_url = self.draft.get("cover_url")

        c = discord.ui.Container(accent_color=C_GOLD)
        if cover_url and str(cover_url).startswith("http"):
            c.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media=cover_url)))

        c.add_item(discord.ui.TextDisplay(
            "## ➕ مراجعة التتبع الجديد\n"
            "-# تأكد من الاسم والمصدر وروم الإشعارات قبل الحفظ."
        ))
        c.add_item(discord.ui.TextDisplay(
            f"**الاسم:** `{title}`\n"
            f"**المصدر:** `{source}`\n"
            f"**الرابط:** {self.draft['url']}\n"
            f"**آخر فصل مكتشف:** `Ch. {_ch_label(chapter)}`\n"
            f"**روم الإشعارات:** {f'<#{channel_id}>' if channel_id else 'سيُستخدم الروم الحالي'}\n"
            f"**رول المنشن:** {f'<@&{role_id}>' if role_id else 'لا يوجد'}\n"
            f"**التحميل التلقائي:** `{'مفعّل' if auto_dl else 'متوقف'}`"
        ))
        if self.draft.get("edited"):
            c.add_item(discord.ui.TextDisplay("✏️ تم تعديل البيانات يدويًا بعد التحليل التلقائي."))

        self.add_item(c)

        btn_confirm = discord.ui.Button(label="✅ تأكيد الإضافة", style=discord.ButtonStyle.success)
        btn_edit = discord.ui.Button(label="✏️ تعديل البيانات", style=discord.ButtonStyle.primary)
        btn_cancel = discord.ui.Button(label="❌ إلغاء", style=discord.ButtonStyle.secondary)

        async def confirm_cb(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            current_title = self.draft.get("title") or _slug_to_name(self.draft["url"])
            all_trackers = await database.sv3_list(interaction.guild_id)
            if any(t["url"] == self.draft["url"] for t in all_trackers):
                await interaction.followup.send("❌ السلسلة مضافة بالفعل.", ephemeral=True)
                return

            tid = await database.sv3_add(
                guild_id=interaction.guild_id,
                url=self.draft["url"],
                notification_channel_id=self.draft.get("notification_channel_id") or str(interaction.channel_id),
                title=current_title,
                mention_role_id=self.draft.get("mention_role_id"),
                added_by_user_id=interaction.user.id,
                last_chapter=float(self.draft.get("last_chapter", 0.0) or 0.0),
                auto_download=int(self.draft.get("auto_download", 1)),
            )

            if not tid:
                await interaction.followup.send("❌ فشل حفظ التتبع. قد يكون الرابط مكررًا.", ephemeral=True)
                return

            try:
                if self.parent_view.engine:
                    await self.parent_view.engine.bootstrap_tracker(tid, interaction.guild_id)
            except Exception as exc:
                logger.warning("Bootstrap tracker failed for %s: %s", tid, exc)

            await self.parent_view.refresh()
            await interaction.followup.send(f"✅ تم إضافة **{current_title}** إلى التتبع بنجاح.", ephemeral=True)
            try:
                await interaction.edit_original_response(
                    content="✅ تم إنشاء التتبع وحفظه.",
                    view=None,
                    embed=None,
                )
            except Exception:
                pass

        async def edit_cb(interaction: discord.Interaction):
            await interaction.response.send_modal(TrackerDraftEditModal(self))

        async def cancel_cb(interaction: discord.Interaction):
            try:
                await interaction.response.edit_message(
                    content="❎ تم إلغاء عملية الإضافة.",
                    view=None,
                    embed=None,
                )
            except Exception:
                await interaction.response.send_message("❎ تم إلغاء العملية.", ephemeral=True)

        btn_confirm.callback = confirm_cb
        btn_edit.callback = edit_cb
        btn_cancel.callback = cancel_cb

        row = discord.ui.ActionRow(btn_confirm, btn_edit, btn_cancel)
        self.add_item(row)


class TrackerEditModal(discord.ui.Modal, title="✏️ تعديل التتبع"):
    title_override = discord.ui.TextInput(
        label="اسم العمل",
        required=False,
        max_length=150,
    )
    channel = discord.ui.TextInput(
        label="روم الإشعارات (ID أو الاسم)",
        required=False,
        max_length=100,
    )
    role = discord.ui.TextInput(
        label="رول المنشن (ID أو الاسم)",
        required=False,
        max_length=100,
    )
    auto_dl = discord.ui.TextInput(
        label="تحميل تلقائي؟ (yes / no)",
        required=False,
        default="yes",
        max_length=10,
    )

    def __init__(self, tracker_view: "TrackerDetailView"):
        super().__init__()
        self.tracker_view = tracker_view
        self.title_override.default = str(tracker_view.tracker.get("title") or "")
        self.channel.default = str(tracker_view.tracker.get("notification_channel_id") or "")
        self.role.default = str(tracker_view.tracker.get("mention_role_id") or "")
        self.auto_dl.default = "yes" if int(tracker_view.tracker.get("auto_download", 1)) else "no"

    async def on_submit(self, interaction: discord.Interaction):
        title = str(self.title_override.value or "").strip()
        channel_val = str(self.channel.value or "").strip()
        role_val = str(self.role.value or "").strip()
        auto_dl_val = str(self.auto_dl.value or "yes").strip().lower() in ("yes", "y", "true", "1")

        updates: dict = {}
        if title:
            updates["title"] = title
        if channel_val:
            channel_id = None
            if channel_val.isdigit():
                channel_id = channel_val
            else:
                chan = discord.utils.get(interaction.guild.text_channels, name=channel_val)
                if chan:
                    channel_id = str(chan.id)
            if channel_id:
                updates["notification_channel_id"] = channel_id
        if role_val:
            role_id = None
            if role_val.isdigit():
                role_id = role_val
            else:
                role = discord.utils.get(interaction.guild.roles, name=role_val)
                if role:
                    role_id = str(role.id)
            updates["mention_role_id"] = role_id

        updates["auto_download"] = 1 if auto_dl_val else 0
        await database.sv3_update(self.tracker_view.tracker["id"], self.tracker_view.guild_id, **updates)
        updated = await database.sv3_get(self.tracker_view.tracker["id"], self.tracker_view.guild_id)
        if updated:
            self.tracker_view.tracker = updated
        await self.tracker_view.refresh()
        await interaction.response.edit_message(view=self.tracker_view)


# ═══════════════════════════════════════════════════════════════════════════════
# Global Settings Dashboard
# ═══════════════════════════════════════════════════════════════════════════════

class TrackerSettingsView(discord.ui.LayoutView):
    """لوحة إعدادات التتبع للسيرفر بالكامل."""

    def __init__(self, guild_id: int, guild_name: str, engine: "TrackerEngineV3", parent_view: PanelView):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.engine = engine
        self.parent_view = parent_view

    async def refresh(self) -> None:
        self.clear_items()

        # Load configs
        trackers = await database.sv3_list(self.guild_id)
        active_count = sum(1 for t in trackers if not t.get("paused"))
        paused_count = len(trackers) - active_count
        auto_count = sum(1 for t in trackers if t.get("auto_download"))
        channel_id_str = await database.get_setting(f"guild_notification_channel_{self.guild_id}", "")
        ping_on_val = await database.get_setting(f"guild_ping_on_update_{self.guild_id}", "1")
        ping_on = ping_on_val == "1"
        roles_val = await database.get_setting(f"guild_admin_roles_{self.guild_id}", "[]")
        try:
            roles_list = json.loads(roles_val)
        except Exception:
            roles_list = []
        channel_mention = f"<#{channel_id_str}>" if channel_id_str else "غير محدد"
        
        container = discord.ui.Container(accent_color=discord.Color.from_rgb(124, 92, 252))
        container.add_item(discord.ui.TextDisplay(
            f"# ⚙️ مركز التتبع — {self.guild_name}\n"
            f"-# {len(trackers)} متابعة · {active_count} نشطة · {paused_count} موقوفة · {auto_count} تحميل تلقائي"
        ))
        container.add_item(discord.ui.TextDisplay(
            f"**الإشعارات:** {channel_mention}\n"
            f"**المنشن عند التحديث:** {'مفعّل' if ping_on else 'متوقف'}\n"
            f"**رتب الإدارة:** `{len(roles_list)}` رتبة"
        ))

        btn_close = discord.ui.Button(
            label="إغلاق",
            style=discord.ButtonStyle.danger,
            custom_id=f"pt_close_{self.guild_id}"
        )
        async def close_cb(interaction: discord.Interaction):
            await self.parent_view.refresh()
            await interaction.response.edit_message(view=self.parent_view)
        btn_close.callback = close_cb

        btn_refresh = discord.ui.Button(
            label="تحديث",
            style=discord.ButtonStyle.secondary,
            custom_id=f"pt_refresh_{self.guild_id}"
        )
        async def refresh_cb(interaction: discord.Interaction):
            await self.refresh()
            await interaction.response.edit_message(view=self)
        btn_refresh.callback = refresh_cb

        btn_add = discord.ui.Button(
            label="➕ إضافة تتبع",
            style=discord.ButtonStyle.success,
            custom_id=f"pt_add_radar_{self.guild_id}"
        )
        async def add_cb(interaction: discord.Interaction):
            await interaction.response.send_modal(AddTrackerModal(self.parent_view))
        btn_add.callback = add_cb

        container.add_item(discord.ui.ActionRow(btn_close, btn_refresh, btn_add))

        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

        container.add_item(discord.ui.TextDisplay("### 🔔 الإشعارات"))
        container.add_item(discord.ui.TextDisplay("اختر روم الإشعارات الموحّد الذي ستصل إليه تنبيهات الفصول الجديدة."))
        
        # ChannelSelect Dropdown
        channel_select = discord.ui.ChannelSelect(
            placeholder="اختر روم الإشعارات...",
            channel_types=[discord.ChannelType.text],
            custom_id=f"pt_chan_sel_{self.guild_id}"
        )
        async def chan_select_cb(interaction: discord.Interaction):
            selected_chan = channel_select.values[0]
            await database.set_setting(f"guild_notification_channel_{self.guild_id}", str(selected_chan.id))
            # Update all trackers in server to this channel
            db = await database._get_db()
            await db.execute(
                "UPDATE server_trackers SET notification_channel_id=? WHERE guild_id=?",
                (str(selected_chan.id), self.guild_id)
            )
            await db.commit()
            await interaction.response.send_message(f"✅ تم تغيير قناة الإشعارات لجميع المتابعات إلى <#{selected_chan.id}>", ephemeral=True)
            await self.refresh()
            await interaction.edit_original_response(view=self)
        channel_select.callback = chan_select_cb
        
        select_row = discord.ui.ActionRow(channel_select)
        container.add_item(select_row)
        
        btn_toggle_ping = discord.ui.Button(
            label="📲 المنشن عند التحديث" if ping_on else "📴 المنشن عند التحديث",
            style=discord.ButtonStyle.success if ping_on else discord.ButtonStyle.danger,
            custom_id=f"pt_ping_toggle_{self.guild_id}"
        )
        async def toggle_ping_cb(interaction: discord.Interaction):
            new_ping = "0" if ping_on else "1"
            await database.set_setting(f"guild_ping_on_update_{self.guild_id}", new_ping)
            # Update all trackers in this guild to use this ping setting
            db = await database._get_db()
            await db.execute(
                "UPDATE server_trackers SET ping_on_update=? WHERE guild_id=?",
                (int(new_ping), self.guild_id)
            )
            await db.commit()
            await self.refresh()
            await interaction.response.edit_message(view=self)
        btn_toggle_ping.callback = toggle_ping_cb
        
        container.add_item(discord.ui.Section("تبديل إرسال المنشن مع كل تحديث", accessory=btn_toggle_ping))
        
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        container.add_item(discord.ui.TextDisplay("### 🛡️ رتب الإدارة"))
        container.add_item(discord.ui.TextDisplay("أضف رتبًا إضافية يمكنها إدارة التتبع في هذا السيرفر، بدون الحاجة لصلاحيات الأدمن الكاملة."))
        roles_str = ", ".join(f"<@&{rid}>" for rid in roles_list) if roles_list else "لا توجد رتب مخصصة حالياً."
        container.add_item(discord.ui.TextDisplay(
            f"**الحالي:** {roles_str}\n"
            "-# من يملك أي رتبة مختارة يحصل على صلاحية إدارة التتبع. مالك السيرفر و`Administrator` و`Manage Server` مسموح لهم دائمًا."
        ))
        
        role_select = discord.ui.RoleSelect(
            placeholder="اختر رتب الإدارة (حتى 10)...",
            min_values=0,
            max_values=10,
            custom_id=f"pt_role_sel_{self.guild_id}"
        )
        async def role_select_cb(interaction: discord.Interaction):
            selected_roles = [str(r.id) for r in role_select.values]
            await database.set_setting(f"guild_admin_roles_{self.guild_id}", json.dumps(selected_roles))
            await interaction.response.send_message("✅ تم تحديث الرتب المسموح لها بإدارة البوت.", ephemeral=True)
            await self.refresh()
            await interaction.edit_original_response(view=self)
        role_select.callback = role_select_cb
        
        role_row = discord.ui.ActionRow(role_select)
        container.add_item(role_row)
        
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        btn_make_role = discord.ui.Button(
            label="➕ إنشاء رول جديدة",
            style=discord.ButtonStyle.success,
            custom_id=f"pt_make_role_{self.guild_id}"
        )
        async def make_role_cb(interaction: discord.Interaction):
            class RoleNameModal(discord.ui.Modal, title="➕ إنشاء رول مخصصة"):
                role_name_input = discord.ui.TextInput(
                    label="اسم الرول المخصصة للتحكم",
                    placeholder="Manga Manager",
                    required=True,
                    max_length=100
                )
                async def on_submit(self, modal_inter: discord.Interaction):
                    rname = self.role_name_input.value.strip()
                    try:
                        new_role = await modal_inter.guild.create_role(
                            name=rname,
                            mentionable=True,
                            reason="Created via Make Custom Role settings panel"
                        )
                        await modal_inter.response.send_message(f"✅ تم إنشاء رول مخصصة بنجاح: {new_role.mention}", ephemeral=True)
                    except Exception as err:
                        await modal_inter.response.send_message(f"❌ فشل إنشاء الرول: `{err}`", ephemeral=True)
            
            await interaction.response.send_modal(RoleNameModal())
            
        btn_make_role.callback = make_role_cb
        container.add_item(discord.ui.Section("إنشاء رول مخصصة لإدارة التتبع", accessory=btn_make_role))
        
        self.add_item(container)


# ═══════════════════════════════════════════════════════════════════════════════
# Tracker Detail & Management View
# ═══════════════════════════════════════════════════════════════════════════════

class TrackerDetailView(discord.ui.LayoutView):
    """لوحة التحكم وإعدادات السلسلة المحددة."""

    def __init__(self, guild_id: int, guild_name: str, engine: "TrackerEngineV3", tracker: dict, parent_view: PanelView, sub_count: int = 0):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.engine = engine
        self.tracker = tracker
        self.parent_view = parent_view
        self.sub_count = sub_count

    async def refresh(self) -> None:
        self.clear_items()
        tid = self.tracker.get("id") or self.tracker.get("tracker_id")
        
        # Load updated tracker data
        if self.parent_view.mode == "guild":
            updated = await database.sv3_get(tid, self.guild_id)
        else:
            updated = await database.get_user_tracker(tid, self.parent_view.user_id or 0)
        if updated:
            self.tracker = updated

        subs = await database.get_series_subscribers(tid)
        self.sub_count = len(subs)

        paused = bool(self.tracker.get("paused"))
        auto_dl = bool(self.tracker.get("auto_download"))
        ping_on = bool(self.tracker.get("ping_on_update", 1))
        
        color = discord.Color.from_rgb(255, 193, 7)
        container = discord.ui.Container(accent_color=color)
        
        # Details & Cover Info (displays target manga details at top)
        det_container = build_tracker_detail(self.tracker, sub_count=self.sub_count)
        for item in det_container.children:
            container.add_item(item)
            
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        # 1. Header with Close Button
        btn_close = discord.ui.Button(
            label="إغلاق",
            style=discord.ButtonStyle.danger,
            custom_id=f"pt_close_{tid}"
        )
        async def close_cb(interaction: discord.Interaction):
            await self.parent_view.refresh()
            await interaction.response.edit_message(view=self.parent_view)
        btn_close.callback = close_cb
        container.add_item(discord.ui.Section("إغلاق لوحة تفاصيل التتبع", accessory=btn_close))
        
        # 2. Add Track to Radar (global shortcut)
        btn_add = discord.ui.Button(
            label="➕ إضافة",
            style=discord.ButtonStyle.success,
            custom_id=f"pt_add_radar_{tid}"
        )
        async def add_cb(interaction: discord.Interaction):
            await interaction.response.send_modal(AddTrackerModal(self.parent_view))
        btn_add.callback = add_cb
        container.add_item(discord.ui.Section("إضافة تتبع آخر إلى الرادار", accessory=btn_add))
        
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        # 3. Notifications heading
        container.add_item(discord.ui.TextDisplay("### 🔔 الإشعارات"))
        container.add_item(discord.ui.TextDisplay("غيّر روم الإشعارات مباشرة من القائمة أدناه."))
        
        # ChannelSelect Dropdown
        channel_select = discord.ui.ChannelSelect(
            placeholder="اختر روم الإشعارات...",
            channel_types=[discord.ChannelType.text],
            custom_id=f"pt_chan_sel_{tid}"
        )
        async def chan_select_cb(interaction: discord.Interaction):
            if self.parent_view.mode == "guild" and not await has_tracker_privilege(interaction, self.tracker):
                await interaction.response.send_message("❌ لا تملك صلاحية تعديل هذا التراكر.", ephemeral=True)
                return
            selected_chan = channel_select.values[0]
            if self.parent_view.mode == "guild":
                await database.sv3_update(tid, self.guild_id, notification_channel_id=str(selected_chan.id))
            else:
                await database.update_user_tracker(tid, interaction.user.id, notification_channel_id=str(selected_chan.id))
            self.tracker["notification_channel_id"] = str(selected_chan.id)
            await interaction.response.send_message(f"✅ تم تغيير روم الإشعارات لهذه السلسلة إلى <#{selected_chan.id}>", ephemeral=True)
            await self.refresh()
            await interaction.edit_original_response(view=self)
        channel_select.callback = chan_select_cb
        
        select_row = discord.ui.ActionRow(channel_select)
        container.add_item(select_row)
        
        self.add_item(container)
        
        # Action controls buttons per requirements: Pause/Resume, Check, Edit, Delete, Subscribe
        btn_pause = discord.ui.Button(
            label="▶️ Resume" if paused else "⏸️ Pause",
            style=discord.ButtonStyle.secondary,
        )
        btn_check = discord.ui.Button(
            label="🔄 Check",
            style=discord.ButtonStyle.primary,
        )
        btn_edit = discord.ui.Button(
            label="✏️ Edit",
            style=discord.ButtonStyle.secondary,
        )
        btn_delete = discord.ui.Button(
            label="🗑️ Delete",
            style=discord.ButtonStyle.danger,
        )
        btn_subscribe = discord.ui.Button(
            label=f"🔔 Subscribe ({self.sub_count})",
            style=discord.ButtonStyle.success,
        )
        btn_back = discord.ui.Button(
            label="🔙 العودة للقائمة",
            style=discord.ButtonStyle.secondary,
        )
        
        async def pause_cb(interaction: discord.Interaction):
            if self.parent_view.mode == "guild" and not await has_tracker_privilege(interaction, self.tracker):
                await interaction.response.send_message("❌ لا تملك صلاحية تعديل هذا التراكر.", ephemeral=True)
                return
            new_paused = 0 if self.tracker.get("paused") else 1
            if self.parent_view.mode == "guild":
                await database.sv3_update(tid, self.guild_id, paused=new_paused)
            else:
                await database.update_user_tracker(tid, interaction.user.id, paused=new_paused)
            self.tracker["paused"] = new_paused
            await self.refresh()
            await interaction.response.edit_message(view=self)

        async def edit_cb(interaction: discord.Interaction):
            if self.parent_view.mode == "guild" and not await has_tracker_privilege(interaction, self.tracker):
                await interaction.response.send_message("❌ لا تملك صلاحية تعديل هذا التراكر.", ephemeral=True)
                return
            await interaction.response.send_modal(TrackerEditModal(self))

        async def check_cb(interaction: discord.Interaction):
            if self.parent_view.mode == "guild" and not await has_tracker_privilege(interaction, self.tracker):
                await interaction.response.send_message("❌ لا تملك صلاحية فحص هذا التراكر.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send("🔄 جاري فحص السلسلة الآن في الخلفية...", ephemeral=True)
            await self.engine._scrape_tracker(self.tracker)
            if self.parent_view.mode == "guild":
                updated = await database.sv3_get(tid, self.guild_id)
            else:
                updated = await database.get_user_tracker(tid, interaction.user.id)
            if updated:
                self.tracker = updated
            await self.refresh()
            await interaction.edit_original_response(view=self)

        async def delete_cb(interaction: discord.Interaction):
            if self.parent_view.mode == "guild" and not await has_tracker_privilege(interaction, self.tracker):
                await interaction.response.send_message("❌ لا تملك صلاحية حذف هذا التراكر.", ephemeral=True)
                return
            if self.parent_view.mode == "guild":
                await database.sv3_delete(tid, self.guild_id)
            else:
                await database.delete_user_tracker(tid, interaction.user.id)
            await interaction.response.send_message("🗑️ تم حذف السلسلة بنجاح.", ephemeral=True)
            await self.parent_view.refresh()
            await interaction.edit_original_response(view=self.parent_view)

        async def subscribe_cb(interaction: discord.Interaction):
            subbed = await database.toggle_user_subscription(tid, interaction.user.id)
            msg = f"🔔 تم الإشتراك في إشعارات **{self.tracker.get('title')}** الشخصية!" if subbed else f"🔕 تم إلغاء الإشتراك في إشعارات **{self.tracker.get('title')}**."
            await self.refresh()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(msg, ephemeral=True)

        async def back_cb(interaction: discord.Interaction):
            await self.parent_view.refresh()
            await interaction.response.edit_message(view=self.parent_view)

        btn_pause.callback = pause_cb
        btn_check.callback = check_cb
        btn_edit.callback = edit_cb
        btn_delete.callback = delete_cb
        btn_subscribe.callback = subscribe_cb
        btn_back.callback = back_cb

        row1 = discord.ui.ActionRow(btn_pause, btn_check, btn_edit, btn_delete, btn_subscribe)
        row2 = discord.ui.ActionRow(btn_back)
        self.add_item(row1)
        self.add_item(row2)


# ═══════════════════════════════════════════════════════════════════════════════
# Dynamic button handler (handles sv3_dl_{id}_{ch}, sv3_doc_{id}_{ch}, etc.)
# ═══════════════════════════════════════════════════════════════════════════════

class DynamicTrackerView(discord.ui.LayoutView):
    """
    Persistent view لمعالجة كل الأزرار الديناميكية في رسائل التتبع.
    يُسجَّل مرة واحدة عند بدء البوت بـ custom_id prefix.
    """

    def __init__(self, engine: "TrackerEngineV3"):
        super().__init__(timeout=None)
        self.engine = engine

    @discord.ui.button(custom_id="sv3_dl_", label="Download", style=discord.ButtonStyle.success)
    async def handle_download(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_download(interaction, button.custom_id)

    @discord.ui.button(custom_id="sv3_doc_", label="📄", style=discord.ButtonStyle.secondary)
    async def handle_document(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_document(interaction, button.custom_id)

    @discord.ui.button(custom_id="sv3_pause_", label="⏸️ إيقاف", style=discord.ButtonStyle.secondary)
    async def handle_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_pause(interaction, button.custom_id)

    @discord.ui.button(custom_id="sv3_details_", label="📋 View details", style=discord.ButtonStyle.secondary)
    async def handle_details(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        parts = button.custom_id.split("_")
        if len(parts) >= 3 and parts[2].isdigit():
            tid = int(parts[2])
            tracker = await database.sv3_get(tid, interaction.guild_id)
            if tracker:
                c = build_tracker_detail(tracker)
                v = discord.ui.LayoutView(timeout=60)
                v.add_item(c)
                await interaction.followup.send(view=v, ephemeral=True)
                return
        await interaction.followup.send("❌ لم يتم العثور على التراكر.", ephemeral=True)

    async def _do_download(self, interaction: discord.Interaction, custom_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            _, _, tid_str, ch_str = custom_id.split("_", 3)
            tid = int(tid_str)
            ch_num = float(ch_str)
        except Exception:
            await interaction.followup.send("❌ تعذر تحديد الفصل.", ephemeral=True)
            return

        tracker = await database.sv3_get(tid, interaction.guild_id)
        if not tracker:
            await interaction.followup.send("❌ التراكر غير موجود.", ephemeral=True)
            return

        event = await database.tevt_get(tid, ch_num)
        if not event:
            await database.tevt_try_insert(tid, ch_num, "")
            event = await database.tevt_get(tid, ch_num)

        if event and event.get("dl_status") == "completed" and event.get("dl_result"):
            await interaction.followup.send(
                f"✅ هذا الفصل محمّل مسبقاً: {event['dl_result']}", ephemeral=True
            )
            return

        # Enqueue the download
        queued = await self.engine.dl_queue.enqueue(tracker, event)
        if queued:
            # Edit original message immediately to reflect Queued... status
            try:
                view = discord.ui.LayoutView.from_message(interaction.message)
                for item in view.children:
                    if isinstance(item, discord.ui.Button) and item.custom_id and item.custom_id.startswith("sv3_dl_"):
                        item.label = "⏳ Queued..."
                        item.disabled = True
                        item.style = discord.ButtonStyle.secondary
                await interaction.message.edit(view=view)
            except Exception:
                pass
            await interaction.followup.send("⬇️ تم إضافة الفصل لطابور التحميل!", ephemeral=True)
        else:
            await interaction.followup.send("⏳ الفصل في طابور التحميل بالفعل.", ephemeral=True)

    async def _do_document(self, interaction: discord.Interaction, custom_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            _, _, tid_str, ch_str = custom_id.split("_", 3)
            tid = int(tid_str)
            ch_num = float(ch_str)
        except Exception:
            await interaction.followup.send("❌ تعذر تحديد الفصل.", ephemeral=True)
            return
        
        event = await database.tevt_get(tid, ch_num)
        if not event or not event.get("detected_at"):
            await interaction.followup.send("📅 وقت صدور الفصل غير مسجل بعد.", ephemeral=True)
            return
        
        try:
            # Parse ISO date string to timestamp
            iso_str = event["detected_at"].replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(iso_str)
            ts = int(dt.timestamp())
            await interaction.followup.send(
                f"📅 تم صدور هذا الفصل في: <t:{ts}:F> (<t:{ts}:R>)",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"[DynamicTrackerView] Failed to parse timestamp: {e}")
            await interaction.followup.send("📅 فشل جلب وقت الصدور.", ephemeral=True)

    async def _do_pause(self, interaction: discord.Interaction, custom_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            tid = int(custom_id.split("_")[-1])
        except Exception:
            await interaction.followup.send("❌ تعذر.", ephemeral=True)
            return
        
        tracker = await database.sv3_get(tid, interaction.guild_id)
        if not tracker:
            await interaction.followup.send("❌ التراكر غير موجود.", ephemeral=True)
            return
            
        if not await has_tracker_privilege(interaction, tracker):
            await interaction.followup.send("❌ لا تملك صلاحية تعديل هذا التراكر.", ephemeral=True)
            return

        new_paused = 0 if tracker["paused"] else 1
        await database.sv3_update(tid, interaction.guild_id, paused=new_paused)
        status = "⏸️ موقوف" if new_paused else "▶️ مُستأنَف"
        await interaction.followup.send(f"{status} تتبع **{tracker.get('title') or tid}**", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Main Cog
# ═══════════════════════════════════════════════════════════════════════════════

class TrackerV3Cog(commands.Cog, name="TrackerV3"):
    """نظام التتبع الموحد v3."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.engine: TrackerEngineV3 | None = None
        self.persistent_view: DynamicTrackerView | None = None

    async def cog_load(self) -> None:
        remote_down = getattr(self.bot, "remote_down", None)
        if remote_down is None:
            logger.error("[TrackerV3] remote_down not found on bot!")
            return

        self.engine = TrackerEngineV3(self.bot, remote_down)
        self.engine.start()
        asyncio.create_task(self.engine.startup_recovery())
        
        # Register persistent view
        self.persistent_view = DynamicTrackerView(self.engine)
        self.bot.add_view(self.persistent_view)
        
        logger.info("[TrackerV3] Cog loaded & engine started & view registered")

    async def cog_unload(self) -> None:
        if self.engine:
            await self.engine.stop()
        if self.persistent_view:
            self.persistent_view.stop()
        logger.info("[TrackerV3] Cog unloaded")

    def _engine_check(self) -> "TrackerEngineV3":
        if not self.engine:
            raise RuntimeError("TrackerEngineV3 not initialized")
        return self.engine

    # ── Command ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="tracker",
        description="📡 فتح لوحة تتبع المانجا التفاعلية (Guild & Personal Control Center)",
    )
    @app_commands.describe(
        search="بحث عن سلسلة محددة بالاسم أو الرابط لفتح بطاقتها فوراً",
    )
    @app_commands.guild_only()
    async def tracker_cmd(
        self,
        interaction: discord.Interaction,
        search: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await has_global_admin_privilege(interaction):
            await interaction.followup.send("❌ لا تملك صلاحية استخدام لوحة التتبع في هذا السيرفر.", ephemeral=True)
            return
            
        engine = self._engine_check()

        if search and search.isdigit():
            tid = int(search)
            tracker = await database.sv3_get(tid, interaction.guild_id)
            if not tracker:
                tracker = await database.get_user_tracker(tid, interaction.user.id)
            if tracker:
                panel = PanelView(interaction.guild_id, interaction.guild.name, engine, user_id=interaction.user.id)
                subs = await database.get_series_subscribers(tid)
                detail_view = TrackerDetailView(
                    interaction.guild_id, interaction.guild.name, engine, tracker, parent_view=panel, sub_count=len(subs)
                )
                await detail_view.refresh()
                await interaction.followup.send(view=detail_view, ephemeral=True)
                return

        panel = PanelView(interaction.guild_id, interaction.guild.name, engine, user_id=interaction.user.id)
        await panel.refresh()
        await interaction.followup.send(view=panel, ephemeral=True)

    @tracker_cmd.autocomplete("search")
    async def tracker_search_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        trackers = await database.sv3_list(interaction.guild_id)
        choices = []
        cur_low = current.lower().strip()
        for t in trackers:
            title = t.get("title") or _slug_to_name(t.get("url", ""))
            if not cur_low or cur_low in title.lower() or cur_low in t.get("url", "").lower():
                choices.append(app_commands.Choice(
                    name=f"{title[:80]} (Ch. {_ch_label(t.get('last_chapter', 0))})",
                    value=str(t["id"])
                ))
                if len(choices) >= 25:
                    break
        return choices



async def setup(bot: commands.Bot) -> None:
    cog = TrackerV3Cog(bot)
    await bot.add_cog(cog)
    logger.info("[TrackerV3] Cog registered")
