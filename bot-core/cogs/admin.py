from __future__ import annotations

import contextlib
import datetime

import aiohttp
import database
import discord
from bot_config import Config
from discord import app_commands
from discord.ext import commands
from services.worker_sync import sync_custom_data_to_worker
from user_system import RANK_COLORS, RANK_LABELS, owner_only, get_rank

C_BLUE = discord.Color.from_rgb(88, 101, 242)
C_GREEN = discord.Color.from_rgb(87, 242, 135)
C_RED = discord.Color.from_rgb(237, 66, 69)
C_TEAL = discord.Color.from_rgb(32, 178, 170)
C_INDIGO = discord.Color.from_rgb(99, 102, 241)


def parse_cookie_string(cookie_str: str) -> dict:
    cookie_str = cookie_str.strip()
    if not cookie_str:
        return {}
    if cookie_str.startswith("{") and cookie_str.endswith("}"):
        try:
            import json

            return json.loads(cookie_str)
        except Exception:
            pass
    cookies = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


async def _reload_and_sync(interaction: discord.Interaction) -> None:
    provider_mgr = getattr(interaction.client, "provider_mgr", None)
    if provider_mgr:
        # refresh site auth cache
        try:
            await provider_mgr._load_custom_sites()
        except Exception:
            with contextlib.suppress(Exception):
                await provider_mgr.reload_custom_sites()
    await sync_custom_data_to_worker(interaction.client, database)


class CustomSelectorModal(discord.ui.Modal, title="🧩 Custom Selector Rule"):
    selector = discord.ui.TextInput(
        label="Selector (css:... أو xpath:...)",
        style=discord.TextStyle.short,
        placeholder="css:.chapter-list a:first-child",
        required=True,
        max_length=200,
    )
    url_attr = discord.ui.TextInput(
        label="URL attribute (عادة href)",
        style=discord.TextStyle.short,
        placeholder="href",
        required=False,
        max_length=30,
        default="href",
    )
    number_regex = discord.ui.TextInput(
        label="Regex لاستخراج رقم الفصل (اختياري)",
        style=discord.TextStyle.short,
        placeholder=r"Chapter\s+(\d+(?:\.\d+)?)",
        required=False,
        max_length=200,
        default="",
    )
    get_first = discord.ui.TextInput(
        label="اختيار أول نتيجة؟ (yes/no)",
        style=discord.TextStyle.short,
        placeholder="no",
        required=False,
        max_length=5,
        default="no",
    )
    use_browser = discord.ui.TextInput(
        label="استخدم Browser (Playwright)؟ (yes/no)",
        style=discord.TextStyle.short,
        placeholder="no",
        required=False,
        max_length=5,
        default="no",
    )

    def __init__(self, domain: str):
        super().__init__()
        self.domain = domain

    async def on_submit(self, interaction: discord.Interaction):
        gf = str(self.get_first.value or "").strip().lower() in (
            "1",
            "yes",
            "y",
            "true",
            "on",
        )
        ub = str(self.use_browser.value or "").strip().lower() in (
            "1",
            "yes",
            "y",
            "true",
            "on",
        )
        await database.set_custom_selector_rule(
            domain=self.domain,
            selector=str(self.selector.value).strip(),
            url_attr=str(self.url_attr.value or "href").strip(),
            number_regex=str(self.number_regex.value or "").strip(),
            get_first=1 if gf else 0,
            use_browser=1 if ub else 0,
        )
        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        if provider_mgr:
            await provider_mgr.reload_custom_sites()
        await sync_custom_data_to_worker(interaction.client, database)
        await interaction.response.send_message(
            f"✅ تم حفظ selector لـ `{self.domain}` ومزامنة التغيير إلى HF Worker.",
            ephemeral=True,
        )


def _extract_folder_id(url: str) -> Optional[str]:
    import re
    DRIVE_RE = re.compile(r"https://drive\.google\.com/(?:drive/folders/|open\?id=)([\w-]+)")
    m = DRIVE_RE.search(url)
    if m: return m.group(1)
    parts = url.strip("/").split("/")
    for part in reversed(parts):
        if len(part) >= 25 and re.match(r'^[a-zA-Z0-9_-]+$', part):
            return part
    return None


def update_env_variable(key: str, value: str):
    import os
    from dotenv import load_dotenv
    lines = []
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as f:
            lines = f.readlines()
    
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
            
    if not found:
        new_lines.append(f"{key}={value}\n")
        
    with open(".env", "w", encoding="utf-8") as f:
        f.writelines(new_lines)
        
    os.environ[key] = value
    load_dotenv(override=True)
    if hasattr(Config, key):
        setattr(Config, key, value)


async def is_owner_level(interaction: discord.Interaction) -> bool:
    from bot_config import Config
    user_id = interaction.user.id
    if user_id in (Config.ALLOWED_USER_IDS or []):
        return True
    if await interaction.client.is_owner(interaction.user):
        return True
    if interaction.guild and user_id == interaction.guild.owner_id:
        return True
    rank = await get_rank(user_id)
    if rank >= 4:
        return True
    return False


def build_site_provider_health_matrix(bot: commands.Bot) -> str:
    """Site Provider Health Matrix table (ONLINE/DEGRADED/OFFLINE status, success rate %, response time ms)."""
    provider_mgr = getattr(bot, "provider_mgr", None)
    health_matrix = {}
    if provider_mgr and hasattr(provider_mgr, "get_provider_health_matrix"):
        try:
            health_matrix = provider_mgr.get_provider_health_matrix() or {}
        except Exception:
            pass

    if not health_matrix:
        from services.metrics import get_provider_health_matrix
        try:
            health_matrix = get_provider_health_matrix() or {}
        except Exception:
            health_matrix = {}

    default_providers = [
        "MangaDex",
        "Comick",
        "MangaFire",
        "MangaPlus",
        "AsuraScans",
        "LekManga",
        "Shinigami",
    ]

    all_providers = list(default_providers)
    for p_name in health_matrix.keys():
        if p_name not in all_providers:
            all_providers.append(p_name)

    matrix = "```\n"
    matrix += f"{'Provider':<12} | {'Status':<8} | {'Success':<7} | {'Latency':<7}\n"
    matrix += "-" * 44 + "\n"

    for p_name in all_providers:
        info = health_matrix.get(p_name)
        if not info:
            for k, v in health_matrix.items():
                if k.lower() == p_name.lower():
                    info = v
                    break

        if info:
            status = str(info.get("status", "ONLINE"))
            succ_val = info.get("success_rate", 100.0)
            succ_str = f"{succ_val:.1f}%" if isinstance(succ_val, (int, float)) else str(succ_val)
            lat_val = info.get("response_time_ms", 0.0)
            lat_str = f"{int(round(lat_val))}ms" if isinstance(lat_val, (int, float)) else str(lat_val)
        else:
            status = "ONLINE"
            succ_str = "100.0%"
            lat_str = "0ms"

        matrix += f"{p_name:<12} | {status:<8} | {succ_str:<7} | {lat_str:<7}\n"

    matrix += "```"
    return matrix


def build_worker_performance_metrics(bot: commands.Bot) -> str:
    """Worker performance metrics."""
    remote_down = getattr(bot, "remote_down", None)
    metrics = getattr(bot, "metrics", None)
    m = metrics.snapshot() if metrics else {}
    
    worker_st = "🟢 ONLINE / READY"
    active_jobs = 0
    queued_jobs = 0
    max_workers = 10
    
    if remote_down and getattr(remote_down, "is_enabled", False):
        w_health = getattr(remote_down, "health_cache", {}) or {}
        active_jobs = w_health.get("running_jobs", 0)
        queued_jobs = w_health.get("queued_jobs", 0)
        max_workers = w_health.get("max_concurrent_jobs", 10)
        if "error" in w_health:
            worker_st = f"🟠 DEGRADED ({w_health['error'][:24]})"
    else:
        worker_st = "⚪ LOCAL ENGINE"

    avg_dl = m.get("download_avg_sec", 0.0)
    dl_ok = m.get("download_ok", 0)
    dl_fail = m.get("download_fail", 0)
    
    return (
        f"• **Status**: `{worker_st}`\n"
        f"• **Max Concurrent Workers**: `{max_workers}` workers\n"
        f"• **Jobs Running / Queued**: `{active_jobs}` running | `{queued_jobs}` queued\n"
        f"• **Download Metrics**: `✅ {dl_ok} succeeded | ❌ {dl_fail} failed`\n"
        f"• **Avg Response / DL Time**: `{avg_dl:.2f}s` per chapter"
    )


async def send_main_admin_dashboard(interaction: discord.Interaction, bot: commands.Bot, heal_status: dict | None = None):
    # Fetch status
    start_time = getattr(bot, "start_time", datetime.datetime.now(datetime.timezone.utc))
    uptime = str(datetime.datetime.now(datetime.timezone.utc) - start_time).split(".")[0]
    trackers = await database.get_tracker_count()
    users = await database.get_user_count()

    is_owner = await is_owner_level(interaction)
    view = AdminDashboardView(bot, is_owner=is_owner)
    
    container = discord.ui.Container(accent_color=C_BLUE)
    container.add_item(discord.ui.TextDisplay("# 👑 لوحة الإدارة والتحكم الشاملة (Admin Dashboard)"))
    
    dash_body = (
        "مرحباً بك في لوحة تحكم البوت الشاملة. يرجى اختيار القسم المراد إدارته بالضغط على الأزرار أدناه:\n\n"
        "📊 **إحصائيات النظام الحالية:**\n"
        f"• **حالة البوت:** `🟢 يعمل`\n"
        f"• **وقت التشغيل (Uptime):** `{uptime}`\n"
        f"• **عدد السلاسل المتتبعة:** `{trackers}` سلسلة\n"
        f"• **الأعضاء بقاعدة البيانات:** `{users}` عضو\n\n"
        "🖥️ **أداء الـ Worker وأداء الشبكة (Worker Performance Metrics):**\n"
        f"{build_worker_performance_metrics(bot)}\n\n"
        "🌐 **جدول حالة المزودات والمواقع (Site Provider Health Matrix):**\n"
        f"{build_site_provider_health_matrix(bot)}"
    )
    
    if heal_status:
        dash_body += (
            "\n\n🔧 **نتائج عملية الفحص والإصلاح (System Heal Diagnostic):**\n"
            f"• **سلامة قاعدة البيانات (PRAGMA quick_check):** `{'🟢 OK' if heal_status.get('db_ok') else '🔴 FAIL'}` (`{heal_status.get('db_check_result')}`)\n"
            f"• **تجميع السجلات غير النشطة:** `{heal_status.get('stale_logs_cleared', 0)}` سجلاً\n"
            f"• **إصلاح أحداث التتبع اليتيمة:** `{heal_status.get('orphaned_events_repaired', 0)}` حدثاً\n"
            f"• **تطهير التذكيرات اليتيمة:** `{heal_status.get('orphaned_reminders_cleared', 0)}` تذكيراً\n"
            f"• **تعديل حالات السلاسل المفصولة:** `{heal_status.get('repaired_trackers_count', 0)}` تراكر"
        )
        
    container.add_item(discord.ui.TextDisplay(dash_body))
    view.add_item(container)

    if not interaction.response.is_done():
        await interaction.response.send_message(view=view, ephemeral=True)
    else:
        await interaction.edit_original_response(content=None, embed=None, view=view)


async def reload_trackers_view(interaction: discord.Interaction, bot: commands.Bot):
    trackers = await database.sv3_list(interaction.guild_id)
    if not trackers:
        await interaction.response.edit_message(content="❌ لا توجد أي سلاسل متتبعة حالياً في هذا السيرفر.", embed=None, view=None)
        return
    view = AdminTrackersView(bot, trackers)
    await interaction.response.edit_message(
        content=None,
        embed=None,
        view=view
    )


class AdminDashboardView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot, is_owner: bool):
        super().__init__(timeout=600)
        self.bot = bot
        self.is_owner = is_owner

        if not is_owner:
            allowed_labels = {"🔗 إدارة التتبع والرادار", "👥 الأعضاء والترقيات", "💰 شحن الأرصدة والنقاط"}
            to_remove = [
                child for child in self.children
                if getattr(child, "label", "") not in allowed_labels
            ]
            for child in to_remove:
                self.remove_item(child)

    @discord.ui.button(label="👥 الأعضاء والترقيات", style=discord.ButtonStyle.primary, row=0)
    async def members_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = AdminMembersView(self.bot)
        await interaction.response.edit_message(
            content=None,
            embed=None,
            view=view
        )

    @discord.ui.button(label="💰 شحن الأرصدة والنقاط", style=discord.ButtonStyle.success, row=0)
    async def credits_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cur_rank = await get_rank(interaction.user.id)
        if cur_rank < 3:
            return await interaction.response.send_message("❌ هذا القسم مخصص لإدارة البوت فقط.", ephemeral=True)
        view = AdminCreditsView(self.bot)
        await interaction.response.edit_message(
            content=None,
            embed=None,
            view=view
        )

    @discord.ui.button(label="🔗 إدارة التتبع والرادار", style=discord.ButtonStyle.primary, row=1)
    async def trackers_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        # Fetch trackers
        trackers = await database.sv3_list(interaction.guild_id)
        if not trackers:
            await interaction.followup.send("❌ لا توجد أي سلاسل متتبعة حالياً في هذا السيرفر.", ephemeral=True)
            return
        
        view = AdminTrackersView(self.bot, trackers)
        await interaction.edit_original_response(
            content=None,
            embed=None,
            view=view
        )

    @discord.ui.button(label="⚙️ الإعدادات العامة", style=discord.ButtonStyle.secondary, row=1)
    async def settings_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.is_owner:
            return await interaction.response.send_message("❌ هذا القسم مخصص لمالك البوت فقط.", ephemeral=True)
        view = AdminSettingsView(self.bot)
        container = discord.ui.Container(accent_color=C_TEAL)
        container.add_item(discord.ui.TextDisplay("# ⚙️ الإعدادات العامة للبوت (.env)"))
        container.add_item(discord.ui.TextDisplay(
            f"• **INPAINTING_SPACE_URL:** `{Config.INPAINTING_SPACE_URL or '—'}`\n"
            f"• **INPAINTING_SPACE_KEY:** `{'***' if Config.INPAINTING_SPACE_KEY else '—'}`\n"
            f"• **HF_TOKEN:** `{'***' if Config.HF_TOKEN else '—'}`\n"
            f"• **GOOGLE_DRIVE_FOLDER_ID:** `{Config.GOOGLE_DRIVE_FOLDER_ID or '—'}`"
        ))
        view.add_item(container)
        await interaction.response.edit_message(
            content=None,
            embed=None,
            view=view
        )

    @discord.ui.button(label="🚨 السجلات والشبكة", style=discord.ButtonStyle.danger, row=2)
    async def logs_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.is_owner:
            return await interaction.response.send_message("❌ هذا القسم مخصص لمالك البوت فقط.", ephemeral=True)
        await interaction.response.defer()
        # Read last 15 lines of bot logs
        logs = await database.get_recent_logs(limit=15)
        log_text = "\n".join(f"[{l[3]}] {l[1]}: {l[2]}" for l in reversed(logs)) if logs else "لا توجد سجلات حالياً."
        
        view = AdminLogsView(self.bot)
        container = discord.ui.Container(accent_color=C_RED)
        container.add_item(discord.ui.TextDisplay("# 🚨 سجل التشغيل والتشخيص (Diagnostic)"))
        container.add_item(discord.ui.TextDisplay(f"```\n{log_text[:1800]}\n```"))
        view.add_item(container)
        await interaction.edit_original_response(
            content=None,
            embed=None,
            view=view
        )

    @discord.ui.button(label="🔐 كوكيز وحماية (Auth)", style=discord.ButtonStyle.secondary, row=2)
    async def auth_panel_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.is_owner:
            return await interaction.response.send_message("❌ هذا القسم مخصص لمالك البوت فقط.", ephemeral=True)
        domains: set[str] = {"asurascans.com"}
        for d, _t, *_ in await database.get_custom_sites():
            if d: domains.add(str(d).lower())
        for d, _updated in await database.get_all_site_auth():
            if d: domains.add(str(d).lower())

        options = [
            discord.SelectOption(label=dom, value=dom) for dom in sorted(domains)
        ]
        view = AuthPanelView(options, default_domain="asurascans.com")
        container = discord.ui.Container(accent_color=C_INDIGO)
        container.add_item(discord.ui.TextDisplay("# 🔐 لوحة التحكم الشاملة (Auth Panel)"))
        container.add_item(discord.ui.TextDisplay(
            "اختر الدومين من القائمة أدناه ثم استخدم الأزرار لتحديث الكوكيز.\n\n"
            "**💡 مميزات:**\n"
            "• يمكنك وضع `User-Agent` مخصص للموقع (مهم لـ Asura/Cloudflare).\n"
            "• يمكنك اختبار تحميل الفصول المقفلة مباشرة من Asura.\n\n"
            "⚠️ لا ترسل الكوكيز في الشات العام، استخدم زر **إضافة / استيراد كوكيز** فقط."
        ))
        view.add_item(container)
        await interaction.response.edit_message(content=None, embed=None, view=view)

    @discord.ui.button(label="🔧 Check & Heal System", style=discord.ButtonStyle.success, row=3)
    async def heal_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        heal_status = await database.heal_system()
        await send_main_admin_dashboard(interaction, self.bot, heal_status=heal_status)



class AdminMembersView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=600)
        self.bot = bot
        self.selected_user_id: int | None = None
        
        container = discord.ui.Container(accent_color=C_BLUE)
        container.add_item(discord.ui.TextDisplay("# 👥 إدارة الأعضاء والترقيات\nاختر العضو من القائمة المنسدلة للتحكم في رتبته وصلاحياته."))
        self.add_item(container)
        
        self.add_item(discord.ui.UserSelect(placeholder="اختر العضو لتعديل رتبته...", min_values=1, max_values=1, custom_id="member_select"))
        
    @discord.ui.button(label="⚠️ مسح جميع الأعضاء (تصفير)", style=discord.ButtonStyle.danger, row=2)
    async def clear_all_members_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cur_rank = await get_rank(interaction.user.id)
        if cur_rank < 4:
            return await interaction.response.send_message("❌ هذا الإجراء مخصص لمالك البوت فقط.", ephemeral=True)
            
        from bot_config import Config
        db = await database._get_db()
        async with db.execute("SELECT COUNT(*) FROM user_permissions") as cursor:
            row = await cursor.fetchone()
            count_before = row[0] if row else 0
            
        allowed = Config.ALLOWED_USER_IDS or []
        if allowed:
            placeholders = ",".join("?" for _ in allowed)
            query = f"DELETE FROM user_permissions WHERE user_id NOT IN ({placeholders})"
            await db.execute(query, tuple(allowed))
        else:
            await db.execute("DELETE FROM user_permissions")
            
        await db.commit()
        await interaction.response.send_message(f"✅ تم حذف جميع الأعضاء من قاعدة البيانات بنجاح (تم تصفير {count_before} عضو).", ephemeral=True)
        await interaction.followup.edit_message(
            message_id=interaction.message.id,
            content=None,
            embed=None,
            view=AdminMembersView(self.bot)
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cur_rank = await get_rank(interaction.user.id)
        if cur_rank < 3:
            await interaction.response.send_message("❌ هذا القسم مخصص لإدارة البوت فقط.", ephemeral=True)
            return False

        custom_id = interaction.data.get("custom_id")
        if custom_id == "member_select":
            values = interaction.data.get("values")
            if values:
                self.selected_user_id = int(values[0])
                # Fetch profile
                rank = await get_rank(self.selected_user_id)
                credits_data = await database.get_user_credits(self.selected_user_id)
                
                view = AdminMemberActionView(self.bot, self.selected_user_id)
                container = discord.ui.Container(accent_color=RANK_COLORS.get(rank, discord.Color.blue()))
                container.add_item(discord.ui.TextDisplay(f"# 👥 ملف العضو: <@{self.selected_user_id}>"))
                
                expiry_val = credits_data.get("vip_expiry")
                expiry_str = f"`{expiry_val.split('T')[0]}`" if expiry_val else "`لا يوجد انتهاء صلاحية (دائم)`"
                
                container.add_item(discord.ui.TextDisplay(
                    f"• **الرتبة الحالية:** **{RANK_LABELS.get(rank, str(rank))}**\n"
                    f"• **صلاحية الـ VIP:** {expiry_str}\n"
                    f"• **نقاط التبييض (Clean):** `{credits_data['inpainting_credits']}` نقطة\n"
                    f"• **نقاط الاستخراج (Extract):** `{credits_data['extraction_credits']}` نقطة\n"
                    f"• **تجربة التبييض:** `{'مستخدمة' if credits_data['used_trial_clean'] else 'غير مستخدمة'}`\n"
                    f"• **تجربة الاستخراج:** `{'مستخدمة' if credits_data['used_trial_extract'] else 'غير مستخدمة'}`"
                ))
                view.add_item(container)
                
                await interaction.response.edit_message(content=None, embed=None, view=view)
                return False
        return True

    @discord.ui.button(label="📋 عرض جميع الأعضاء", style=discord.ButtonStyle.primary, row=1)
    async def list_members_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        users_list = await database.get_all_users()
        view = AdminMembersListView(self.bot, users_list, page=0)
        await interaction.edit_original_response(content=None, embed=None, view=view.generate_view())

    @discord.ui.button(label="⬅️ العودة للرئيسية", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await send_main_admin_dashboard(interaction, self.bot)


class AdminMembersListView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot, users_list: list, page: int = 0):
        super().__init__(timeout=600)
        self.bot = bot
        self.users_list = users_list
        self.page = page
        self.per_page = 10

    def generate_view(self) -> discord.ui.LayoutView:
        self.clear_items()
        
        start = self.page * self.per_page
        end = start + self.per_page
        page_users = self.users_list[start:end]
        total_pages = max(1, (len(self.users_list) + self.per_page - 1) // self.per_page)
        
        container = discord.ui.Container(accent_color=C_BLUE)
        container.add_item(discord.ui.TextDisplay(f"# 📋 قائمة أعضاء البوت (صفحة {self.page + 1}/{total_pages})"))
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        desc_lines = []
        options = []
        
        for u in page_users:
            uid, rank, note, _ = u
            rank_name = RANK_LABELS.get(rank, str(rank))
            note_str = f" ({note})" if note else ""
            desc_lines.append(f"• <@{uid}> (`{uid}`) ── **{rank_name}**{note_str}")
            
            options.append(discord.SelectOption(
                label=f"عضو: {uid}",
                description=f"الرتبة: {rank_name}",
                value=str(uid)
            ))
            
        if not desc_lines:
            container.add_item(discord.ui.TextDisplay("لا يوجد أعضاء مسجلين في هذه الصفحة."))
        else:
            container.add_item(discord.ui.TextDisplay("\n".join(desc_lines)))
            
        self.add_item(container)
        
        if options:
            select = discord.ui.Select(placeholder="اختر عضواً من هذه الصفحة لإدارته...", options=options, custom_id="member_select_list")
            
            async def on_select_member(inter: discord.Interaction):
                selected_uid = int(select.values[0])
                rank = await get_rank(selected_uid)
                credits_data = await database.get_user_credits(selected_uid)
                
                view = AdminMemberActionView(self.bot, selected_uid)
                container_profile = discord.ui.Container(accent_color=RANK_COLORS.get(rank, discord.Color.blue()))
                container_profile.add_item(discord.ui.TextDisplay(f"# 👥 ملف العضو: <@{selected_uid}>"))
                
                expiry_val = credits_data.get("vip_expiry")
                expiry_str = f"`{expiry_val.split('T')[0]}`" if expiry_val else "`لا يوجد انتهاء صلاحية (دائم)`"
                
                container_profile.add_item(discord.ui.TextDisplay(
                    f"• **الرتبة الحالية:** **{RANK_LABELS.get(rank, str(rank))}**\n"
                    f"• **صلاحية الـ VIP:** {expiry_str}\n"
                    f"• **نقاط التبييض (Clean):** `{credits_data['inpainting_credits']}` نقطة\n"
                    f"• **نقاط الاستخراج (Extract):** `{credits_data['extraction_credits']}` نقطة\n"
                    f"• **تجربة التبييض:** `{'مستخدمة' if credits_data['used_trial_clean'] else 'غير مستخدمة'}`\n"
                    f"• **تجربة الاستخراج:** `{'مستخدمة' if credits_data['used_trial_extract'] else 'غير مستخدمة'}`"
                ))
                view.add_item(container_profile)
                
                await inter.response.edit_message(content=None, embed=None, view=view)
                
            select.callback = on_select_member
            self.add_item(select)
            
        # Pagination buttons
        if self.page > 0:
            btn_prev = discord.ui.Button(label="⬅️ السابقة", style=discord.ButtonStyle.primary, row=2)
            async def prev_page(inter: discord.Interaction):
                await inter.response.defer()
                view_prev = AdminMembersListView(self.bot, self.users_list, self.page - 1)
                await inter.edit_original_response(view=view_prev.generate_view())
            btn_prev.callback = prev_page
            self.add_item(btn_prev)
            
        if end < len(self.users_list):
            btn_next = discord.ui.Button(label="➡️ التالية", style=discord.ButtonStyle.primary, row=2)
            async def next_page(inter: discord.Interaction):
                await inter.response.defer()
                view_next = AdminMembersListView(self.bot, self.users_list, self.page + 1)
                await inter.edit_original_response(view=view_next.generate_view())
            btn_next.callback = next_page
            self.add_item(btn_next)
            
        btn_back = discord.ui.Button(label="⬅️ عودة للأعضاء", style=discord.ButtonStyle.secondary, row=3)
        async def go_back(inter: discord.Interaction):
            await inter.response.edit_message(content=None, embed=None, view=AdminMembersView(self.bot))
        btn_back.callback = go_back
        self.add_item(btn_back)
        
        return self


class SetVipExpiryModal(discord.ui.Modal, title="⏳ تعيين مدة صلاحية VIP"):
    days = discord.ui.TextInput(
        label="عدد الأيام (أرقام فقط، أو 0 ليكون VIP دائم):",
        style=discord.TextStyle.short,
        placeholder="30",
        required=True,
        max_length=5
    )

    def __init__(self, bot: commands.Bot, target_user_id: int):
        super().__init__()
        self.bot = bot
        self.target_user_id = target_user_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            days_val = int(self.days.value.strip())
        except ValueError:
            return await interaction.response.send_message("❌ يرجى إدخال عدد أيام صحيح (رقم فقط).", ephemeral=True)

        if days_val <= 0:
            await database.set_user_vip_expiry(self.target_user_id, None)
            await interaction.response.send_message(f"✅ تم إلغاء تاريخ انتهاء صلاحية VIP لـ <@{self.target_user_id}> وأصبحت دائمة.", ephemeral=True)
        else:
            expiry_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days_val)
            expiry_iso = expiry_dt.isoformat()
            
            curr_rank = await get_rank(self.target_user_id)
            if curr_rank < 2:
                await database.set_user_rank(self.target_user_id, 2, "Upgraded via VIP Expiry setter")
            await database.set_user_vip_expiry(self.target_user_id, expiry_iso)
            await interaction.response.send_message(f"✅ تم تفعيل VIP لـ <@{self.target_user_id}> لمدة `{days_val}` يوم (تنتهي في `{expiry_dt.date()}`).", ephemeral=True)


class AdminMemberActionView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot, target_user_id: int):
        super().__init__(timeout=600)
        self.bot = bot
        self.target_user_id = target_user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cur_rank = await get_rank(interaction.user.id)
        if cur_rank < 3:
            await interaction.response.send_message("❌ هذا الإجراء مخصص لإدارة البوت فقط.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🛡️ ترقية لأدمن", style=discord.ButtonStyle.danger, row=0)
    async def admin_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cur_rank = await get_rank(interaction.user.id)
        if cur_rank < 4:
            return await interaction.response.send_message("❌ وحدك المالك (Owner) يمكنه تعيين أدمن جديد.", ephemeral=True)
        target_rank = await get_rank(self.target_user_id)
        if target_rank >= cur_rank:
            return await interaction.response.send_message("❌ لا يمكنك تعديل رتبة مستخدم يملك نفس رتبتك أو أعلى.", ephemeral=True)
        await database.set_user_rank(self.target_user_id, 3, "Set via Discord admin panel")
        await interaction.response.send_message(f"🛡️ تم ترقية <@{self.target_user_id}> إلى رتبة **أدمن** بنجاح.", ephemeral=True)
        await interaction.followup.edit_message(
            message_id=interaction.message.id,
            content=None,
            embed=None,
            view=AdminMembersView(self.bot)
        )

    @discord.ui.button(label="⭐ ترقية لـ VIP", style=discord.ButtonStyle.success, row=0)
    async def vip_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cur_rank = await get_rank(interaction.user.id)
        target_rank = await get_rank(self.target_user_id)
        if target_rank >= cur_rank:
            return await interaction.response.send_message("❌ لا يمكنك تعديل رتبة مستخدم يملك نفس رتبتك أو أعلى.", ephemeral=True)
        await database.set_user_rank(self.target_user_id, 2, "Set via Discord admin panel")
        await interaction.response.send_message(f"⭐ تم ترقية <@{self.target_user_id}> إلى رتبة **VIP** بنجاح.", ephemeral=True)
        await interaction.followup.edit_message(
            message_id=interaction.message.id,
            content=None,
            embed=None,
            view=AdminMembersView(self.bot)
        )

    @discord.ui.button(label="👤 تنزيل لعضو عادي", style=discord.ButtonStyle.primary, row=0)
    async def user_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cur_rank = await get_rank(interaction.user.id)
        target_rank = await get_rank(self.target_user_id)
        if target_rank >= cur_rank:
            return await interaction.response.send_message("❌ لا يمكنك تعديل رتبة مستخدم يملك نفس رتبتك أو أعلى.", ephemeral=True)
        await database.set_user_rank(self.target_user_id, 1, "Set via Discord admin panel")
        await database.set_user_vip_expiry(self.target_user_id, None)
        await interaction.response.send_message(f"👤 تم تنزيل <@{self.target_user_id}> إلى رتبة **عضو عادي**.", ephemeral=True)
        await interaction.followup.edit_message(
            message_id=interaction.message.id,
            content=None,
            embed=None,
            view=AdminMembersView(self.bot)
        )

    @discord.ui.button(label="🚫 حظر العضو", style=discord.ButtonStyle.danger, row=1)
    async def block_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cur_rank = await get_rank(interaction.user.id)
        target_rank = await get_rank(self.target_user_id)
        if target_rank >= cur_rank:
            return await interaction.response.send_message("❌ لا يمكنك تعديل رتبة مستخدم يملك نفس رتبتك أو أعلى.", ephemeral=True)
        await database.set_user_rank(self.target_user_id, 0, "Blocked via Discord admin panel")
        await database.set_user_vip_expiry(self.target_user_id, None)
        await interaction.response.send_message(f"🚫 تم حظر <@{self.target_user_id}> من استخدام البوت.", ephemeral=True)
        await interaction.followup.edit_message(
            message_id=interaction.message.id,
            content=None,
            embed=None,
            view=AdminMembersView(self.bot)
        )

    @discord.ui.button(label="🗑️ إعادة تفعيل التجربة", style=discord.ButtonStyle.secondary, row=1)
    async def clear_trial_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cur_rank = await get_rank(interaction.user.id)
        target_rank = await get_rank(self.target_user_id)
        if target_rank >= cur_rank:
            return await interaction.response.send_message("❌ لا يمكنك تعديل رتبة مستخدم يملك نفس رتبتك أو أعلى.", ephemeral=True)
        db = await database._get_db()
        await db.execute("UPDATE user_permissions SET used_trial_clean=0, used_trial_extract=0 WHERE user_id=?", (self.target_user_id,))
        await db.commit()
        await interaction.response.send_message(f"🔄 تم إعادة تفعيل التجربة المجانية لـ <@{self.target_user_id}>.", ephemeral=True)
        await interaction.followup.edit_message(
            message_id=interaction.message.id,
            content=None,
            embed=None,
            view=AdminMembersView(self.bot)
        )

    @discord.ui.button(label="⏳ تعيين مدة VIP", style=discord.ButtonStyle.primary, row=1)
    async def set_vip_expiry_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        cur_rank = await get_rank(interaction.user.id)
        target_rank = await get_rank(self.target_user_id)
        if target_rank >= cur_rank:
            return await interaction.response.send_message("❌ لا يمكنك تعديل رتبة مستخدم يملك نفس رتبتك أو أعلى.", ephemeral=True)
        modal = SetVipExpiryModal(self.bot, self.target_user_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⬅️ العودة للأعضاء", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content=None,
            embed=None,
            view=AdminMembersView(self.bot)
        )


class AdminCreditsView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=600)
        self.bot = bot
        self.selected_user_id: int | None = None
        
        container = discord.ui.Container(accent_color=C_BLUE)
        container.add_item(discord.ui.TextDisplay("# 💰 إدارة الأرصدة ونقاط الاستخدام\nاختر العضو لشحن نقاط التبييض أو الاستخراج له."))
        self.add_item(container)
        
        self.add_item(discord.ui.UserSelect(placeholder="اختر العضو لشحن نقاطه...", min_values=1, max_values=1, custom_id="credit_user_select"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        custom_id = interaction.data.get("custom_id")
        if custom_id == "credit_user_select":
            values = interaction.data.get("values")
            if values:
                self.selected_user_id = int(values[0])
                await interaction.response.send_modal(ManageCreditsModal(self.bot, self.selected_user_id))
                return False
        return True

    @discord.ui.button(label="⬅️ العودة للرئيسية", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await send_main_admin_dashboard(interaction, self.bot)


class ManageCreditsModal(discord.ui.Modal, title="💰 شحن أرصدة ونقاط العضو"):
    clean_add = discord.ui.TextInput(
        label="إضافة نقاط تبييض (أرقام، مثلاً: 10 أو -5)",
        style=discord.TextStyle.short,
        placeholder="0",
        required=False,
        default="0"
    )
    extract_add = discord.ui.TextInput(
        label="إضافة نقاط استخراج (أرقام، مثلاً: 10 أو -5)",
        style=discord.TextStyle.short,
        placeholder="0",
        required=False,
        default="0"
    )

    def __init__(self, bot: commands.Bot, target_user_id: int):
        super().__init__()
        self.bot = bot
        self.target_user_id = target_user_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            clean = int(str(self.clean_add.value or "0").strip())
            extract = int(str(self.extract_add.value or "0").strip())
        except ValueError:
            return await interaction.response.send_message("❌ يرجى إدخال أرقام صحيحة فقط (مثال: 5 أو -3).", ephemeral=True)
        
        await database.add_user_credits(self.target_user_id, clean, extract)
        credits_data = await database.get_user_credits(self.target_user_id)
        
        await interaction.response.send_message(
            f"✅ تم شحن الأرصدة بنجاح لـ <@{self.target_user_id}>:\n"
            f"• تم إضافة: `{clean}` تبييض و `{extract}` استخراج.\n"
            f"• الرصيد الإجمالي الحالي: `{credits_data['inpainting_credits']}` تبييض، `{credits_data['extraction_credits']}` استخراج.",
            ephemeral=True
        )


class AdminTrackersView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot, trackers: list[dict]):
        super().__init__(timeout=600)
        self.bot = bot
        self.trackers = trackers
        self.selected_tracker_id: int | None = None
        
        container = discord.ui.Container(accent_color=C_BLUE)
        container.add_item(discord.ui.TextDisplay("# 🔗 إدارة التتبع والرادار (Tracker v3)\nاختر السلسلة المتتبعة للتحكم فيها:"))
        self.add_item(container)
        
        options = []
        for t in trackers[:25]: # Discord max limit
            status_symbol = "⏸️" if t.get("paused") else "▶️"
            options.append(discord.SelectOption(
                label=f"{t.get('title') or 'Manga'} (Ch. {t.get('last_chapter') or 0})",
                description=f"{status_symbol} {t.get('url')[:50]}",
                value=str(t.get("id"))
            ))
            
        self.select = discord.ui.Select(placeholder="اختر السلسلة للتحكم بها...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        self.selected_tracker_id = int(self.select.values[0])
        # Find tracker
        tracker = next((t for t in self.trackers if t["id"] == self.selected_tracker_id), None)
        if not tracker:
            return await interaction.response.send_message("❌ لم يتم العثور على السلسلة.", ephemeral=True)
        
        status_label = "🔴 متوقف مؤقتاً" if tracker.get("paused") else "🟢 نشط ويتتبع"
        folder_info = "—"
        folder_url = ""
        tdf = await database.tdf_get(tracker["id"])
        if tdf:
            folder_info = f"`{tdf.get('folder_name')}`"
            folder_url = tdf.get("folder_url") or ""

        view = AdminTrackerActionView(self.bot, tracker, folder_url)
        container = discord.ui.Container(accent_color=C_BLUE)
        container.add_item(discord.ui.TextDisplay(f"# 🔗 إدارة السلسلة: {tracker.get('title') or 'Manga'}"))
        container.add_item(discord.ui.TextDisplay(
            f"• **الرابط:** {tracker.get('url')}\n"
            f"• **الحالة:** {status_label}\n"
            f"• **آخر فصل:** `Ch. {tracker.get('last_chapter') or 0}`\n"
            f"• **مجلد Drive:** {folder_info}\n"
            f"• **رقم المجلد:** `{tdf.get('folder_id') if tdf else '—'}`"
        ))
        view.add_item(container)
        
        await interaction.response.edit_message(content=None, embed=None, view=view)

    @discord.ui.button(label="⬅️ العودة للرئيسية", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await send_main_admin_dashboard(interaction, self.bot)


class AdminTrackerActionView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot, tracker: dict, folder_url: str):
        super().__init__(timeout=600)
        self.bot = bot
        self.tracker = tracker
        self.folder_url = folder_url

    @discord.ui.button(label="⏸️ إيقاف / ▶️ تشغيل", style=discord.ButtonStyle.primary, row=0)
    async def pause_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        new_paused = 0 if self.tracker.get("paused") else 1
        await database.sv3_update(self.tracker["id"], interaction.guild_id, paused=new_paused)
        status_word = "تشغيل" if not new_paused else "إيقاف"
        await interaction.response.send_message(f"✅ تم **{status_word}** تتبع السلسلة بنجاح.", ephemeral=True)
        await reload_trackers_view(interaction, self.bot)

    @discord.ui.button(label="🔄 فحص فوري (Force Check)", style=discord.ButtonStyle.success, row=0)
    async def force_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("🔄 جاري فحص السلسلة الآن في الخلفية...", ephemeral=True)
        radar_cog = self.bot.get_cog("RadarV3") or self.bot.get_cog("radar")
        if radar_cog and hasattr(radar_cog, "check_tracker_now"):
            asyncio.create_task(radar_cog.check_tracker_now(self.tracker["id"]))

    @discord.ui.button(label="📁 تعديل مجلد Drive", style=discord.ButtonStyle.secondary, row=0)
    async def drive_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(EditTrackerDriveModal(self.bot, self.tracker))
    @discord.ui.button(label="❌ حذف التتبع بالكامل", style=discord.ButtonStyle.danger, row=1)
    async def delete_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await database.sv3_delete(self.tracker["id"], interaction.guild_id)
        await interaction.response.send_message("🗑️ تم حذف السلسلة من الرادار والتتبع نهائياً.", ephemeral=True)
        await reload_trackers_view(interaction, self.bot)

    @discord.ui.button(label="⬅️ العودة للقائمة", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await reload_trackers_view(interaction, self.bot)


class EditTrackerDriveModal(discord.ui.Modal, title="📁 تعديل مجلد Google Drive"):
    folder_url = discord.ui.TextInput(
        label="رابط مجلد Google Drive الجديد",
        style=discord.TextStyle.short,
        placeholder="https://drive.google.com/drive/folders/...",
        required=True
    )

    def __init__(self, bot: commands.Bot, tracker: dict):
        super().__init__()
        self.bot = bot
        self.tracker = tracker

    async def on_submit(self, interaction: discord.Interaction):
        url = str(self.folder_url.value).strip()
        folder_id = _extract_folder_id(url)
        if not folder_id:
            return await interaction.response.send_message("❌ الرابط غير صحيح. يجب أن يكون رابط مجلد Google Drive.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        try:
            from drive_stitch import build_drive_service
            loop = asyncio.get_event_loop()
            service = await loop.run_in_executor(None, build_drive_service)
            meta = await loop.run_in_executor(None, lambda: service.files().get(fileId=folder_id, fields="name").execute())
            name = meta.get('name', 'Google Folder')
            
            await database.tdf_upsert(self.tracker["id"], name, folder_id, url)
            await interaction.followup.send(f"✅ تم تعديل مجلد Drive بنجاح إلى: **{name}**", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ فشل تحديث المجلد: {e}", ephemeral=True)


class AdminSettingsView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=600)
        self.bot = bot

    @discord.ui.button(label="🔑 مفاتيح الـ API (Space Keys)", style=discord.ButtonStyle.primary, row=0)
    async def apis_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(EditAPIsModal(self.bot))

    @discord.ui.button(label="📁 مجلدات وتكامل Google", style=discord.ButtonStyle.secondary, row=0)
    async def drive_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(EditGoogleModal(self.bot))

    @discord.ui.button(label="🤖 قيود وحدود البوت", style=discord.ButtonStyle.secondary, row=0)
    async def limits_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(EditLimitsModal(self.bot))

    @discord.ui.button(label="⬅️ العودة للرئيسية", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await send_main_admin_dashboard(interaction, self.bot)


class EditAPIsModal(discord.ui.Modal, title="🔑 تعديل مفاتيح الـ API"):
    hf_token = discord.ui.TextInput(
        label="Hugging Face Token (HF_TOKEN)",
        style=discord.TextStyle.short,
        placeholder="hf_...",
        required=False
    )
    space_url = discord.ui.TextInput(
        label="رابط الـ Inpainting Space URL",
        style=discord.TextStyle.short,
        placeholder="https://...",
        required=False
    )
    space_key = discord.ui.TextInput(
        label="مفتاح الـ Inpainting Space Key",
        style=discord.TextStyle.short,
        placeholder="...",
        required=False
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.hf_token.default = Config.HF_TOKEN or ""
        self.space_url.default = Config.INPAINTING_SPACE_URL or ""
        self.space_key.default = Config.INPAINTING_SPACE_KEY or ""

    async def on_submit(self, interaction: discord.Interaction):
        update_env_variable("HF_TOKEN", str(self.hf_token.value).strip())
        update_env_variable("INPAINTING_SPACE_URL", str(self.space_url.value).strip())
        update_env_variable("INPAINTING_SPACE_KEY", str(self.space_key.value).strip())
        await interaction.response.send_message("✅ تم حفظ مفاتيح الـ API وإعادة تحميل الإعدادات بنجاح.", ephemeral=True)


class EditGoogleModal(discord.ui.Modal, title="📁 إعدادات Google Drive"):
    drive_folder = discord.ui.TextInput(
        label="مجلد Drive الافتراضي (GOOGLE_DRIVE_FOLDER_ID)",
        style=discord.TextStyle.short,
        placeholder="...",
        required=False
    )
    gofile_token = discord.ui.TextInput(
        label="توكن GoFile (GOFILE_TOKEN)",
        style=discord.TextStyle.short,
        placeholder="...",
        required=False
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.drive_folder.default = Config.GOOGLE_DRIVE_FOLDER_ID or ""
        self.gofile_token.default = Config.GOFILE_TOKEN or ""

    async def on_submit(self, interaction: discord.Interaction):
        update_env_variable("GOOGLE_DRIVE_FOLDER_ID", str(self.drive_folder.value).strip())
        update_env_variable("GOFILE_TOKEN", str(self.gofile_token.value).strip())
        await interaction.response.send_message("✅ تم حفظ إعدادات Google و GoFile وإعادة تحميل التكوين.", ephemeral=True)


class EditLimitsModal(discord.ui.Modal, title="🤖 حدود وقيود تشغيل البوت"):
    max_images = discord.ui.TextInput(
        label="أقصى عدد صور في الفصل (MAX_IMAGES)",
        style=discord.TextStyle.short,
        placeholder="200",
        required=False,
        default="200"
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.max_images.default = str(getattr(Config, "MAX_IMAGES", "200"))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(str(self.max_images.value).strip())
        except ValueError:
            return await interaction.response.send_message("❌ يرجى إدخال أرقام صحيحة فقط.", ephemeral=True)
        
        update_env_variable("MAX_IMAGES", str(val))
        await interaction.response.send_message(f"✅ تم تعديل أقصى حد للصور إلى: `{val}` صورة.", ephemeral=True)


class AdminLogsView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=600)
        self.bot = bot

    @discord.ui.button(label="🧹 مسح كاش الملفات (Clear Cache)", style=discord.ButtonStyle.primary, row=0)
    async def clear_cache_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        db = await database._get_db()
        await db.execute("DELETE FROM extract_chunks_cache")
        await db.commit()
        await interaction.response.send_message("🧹 تم مسح كاش الصفحات المترجمة بنجاح.", ephemeral=True)

    @discord.ui.button(label="📡 اختبار اتصال الشبكة", style=discord.ButtonStyle.success, row=0)
    async def ping_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        import time
        results = []
        async with aiohttp.ClientSession() as session:
            t0 = time.perf_counter()
            try:
                async with session.get("https://www.googleapis.com", timeout=5) as r:
                    elapsed = (time.perf_counter() - t0) * 1000
                    results.append(f"🟢 **Google APIs:** {elapsed:.0f}ms (Status: {r.status})")
            except Exception as e:
                results.append(f"🔴 **Google APIs:** خطأ ({e})")

            t0 = time.perf_counter()
            try:
                async with session.get("https://api.comick.fun/chapter?limit=1", timeout=5) as r:
                    elapsed = (time.perf_counter() - t0) * 1000
                    results.append(f"🟢 **Comick API:** {elapsed:.0f}ms (Status: {r.status})")
            except Exception as e:
                results.append(f"🔴 **Comick API:** خطأ ({e})")

            t0 = time.perf_counter()
            try:
                url = (Config.INPAINTING_SPACE_URL or "").rstrip("/") + "/health"
                async with session.get(url, timeout=5) as r:
                    elapsed = (time.perf_counter() - t0) * 1000
                    results.append(f"🟢 **HuggingFace Space:** {elapsed:.0f}ms (Status: {r.status})")
            except Exception as e:
                results.append(f"🔴 **HuggingFace Space:** خطأ ({e})")

        report = "\n".join(results)
        await interaction.followup.send(content=f"📡 **تقرير اختبار الشبكة:**\n{report}", ephemeral=True)

    @discord.ui.button(label="🔄 إعادة تشغيل البوت", style=discord.ButtonStyle.danger, row=0)
    async def restart_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("🔄 جاري إعادة تشغيل البوت...", ephemeral=True)
        import sys
        sys.exit(0)

    @discord.ui.button(label="⬅️ العودة للرئيسية", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await send_main_admin_dashboard(interaction, self.bot)


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="admin",
        description="[Owner] فتح لوحة التحكم والإدارة الموحدة للبوت",
    )
    @owner_only()
    async def admin_cmd(self, interaction: discord.Interaction):
        await send_main_admin_dashboard(interaction, self.bot)


class AsuraQuickAuthModal(discord.ui.Modal, title="⚡ Asura Cookies (Quick Auth)"):
    """Modal سريع لإدخال كوكيز Asura مباشرة."""

    access_token = discord.ui.TextInput(
        label="access_token (JWT)",
        style=discord.TextStyle.paragraph,
        placeholder="eyJhbGciOiJIUzI1Ni...",
        required=True,
        max_length=2000,
    )
    refresh_token = discord.ui.TextInput(
        label="refresh_token",
        style=discord.TextStyle.short,
        placeholder="4d68ec765de8a346...",
        required=True,
        max_length=200,
    )
    cf_clearance = discord.ui.TextInput(
        label="cf_clearance",
        style=discord.TextStyle.paragraph,
        placeholder="wlGtryZf...",
        required=True,
        max_length=2000,
    )
    cf_vid = discord.ui.TextInput(
        label="__cf_vid (اختياري)",
        style=discord.TextStyle.short,
        placeholder="7cd9ec26...",
        required=False,
        max_length=100,
    )
    user_agent = discord.ui.TextInput(
        label="User-Agent (اختياري لكن مهم مع Cloudflare)",
        style=discord.TextStyle.paragraph,
        placeholder="Mozilla/5.0 ...",
        required=False,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        domain = "asurascans.com"
        data: dict[str, str] = {}

        at = str(self.access_token.value).strip()
        rt = str(self.refresh_token.value).strip()
        cf = str(self.cf_clearance.value).strip()
        vid = str(self.cf_vid.value or "").strip()
        ua = str(self.user_agent.value or "").strip()

        if at:
            data["access_token"] = at
        if rt:
            data["refresh_token"] = rt
        if cf:
            data["cf_clearance"] = cf
        if vid:
            data["__cf_vid"] = vid
        if ua:
            data["__custom_user_agent"] = ua

        if not data:
            return await interaction.response.send_message(
                "\u274c ما في بيانات صالحة.", ephemeral=True
            )

        # Merge with existing
        current = await database.get_site_auth(domain) or {}
        current.update(data)
        await database.set_site_auth(domain, current)
        await _reload_and_sync(interaction)

        keys_saved = ", ".join(f"`{k}`" for k in data.keys())
        em = discord.Embed(
            title="\u2705 تم حفظ Asura Auth",
            description=(
                f"تم حفظ المفاتيح: {keys_saved}\n\n"
                r"✅ البوت سيجدد **access_token** تلقائيا باستخدام refresh_token لما ينتهي صلاحيته."
            ),
            color=discord.Color.from_rgb(35, 165, 89),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        em.set_footer(text=f"Domain: {domain}")
        await interaction.response.send_message(embed=em, ephemeral=True)


class ImportAuthModal(discord.ui.Modal):
    def __init__(self, domain: str):
        super().__init__(title="🔐 استيراد Cookies / Tokens")
        self.domain_input = discord.ui.TextInput(
            label="الدومين",
            style=discord.TextStyle.short,
            placeholder="asurascans.com",
            required=True,
            max_length=120,
            default=domain,
        )
        self.cookies_input = discord.ui.TextInput(
            label="Raw Cookie Header أو JSON",
            style=discord.TextStyle.paragraph,
            placeholder="cf_clearance=...; access_token=...; refresh_token=...",
            required=True,
            max_length=4000,
        )
        self.user_agent_input = discord.ui.TextInput(
            label="User-Agent (مهم لبعض المواقع مثل Asura)",
            style=discord.TextStyle.paragraph,
            placeholder="Mozilla/5.0 ... (اختياري لكن يفضل وضعه لتجنب الحظر)",
            required=False,
            max_length=1000,
        )
        self.mode_input = discord.ui.TextInput(
            label="الطريقة (merge/replace)",
            style=discord.TextStyle.short,
            placeholder="merge",
            required=False,
            max_length=10,
            default="merge",
        )
        self.add_item(self.domain_input)
        self.add_item(self.cookies_input)
        self.add_item(self.user_agent_input)
        self.add_item(self.mode_input)

    async def on_submit(self, interaction: discord.Interaction):
        domain = (
            str(self.domain_input.value)
            .lower()
            .replace("https://", "")
            .replace("http://", "")
            .split("/")[0]
        )
        data = parse_cookie_string(str(self.cookies_input.value))

        # Save user-agent if provided
        ua = str(self.user_agent_input.value).strip()
        if ua:
            data["__custom_user_agent"] = ua

        if not data:
            return await interaction.response.send_message(
                "❌ ما لقيت بيانات صالحة في المدخلات.", ephemeral=True
            )

        mode = str(self.mode_input.value or "").strip().lower()
        if mode not in ("merge", "replace", ""):
            return await interaction.response.send_message(
                "❌ mode لازم يكون merge أو replace.", ephemeral=True
            )

        if mode == "replace":
            await database.set_site_auth(domain, data)
        else:
            current = await database.get_site_auth(domain) or {}
            current.update(data)
            await database.set_site_auth(domain, current)

        await _reload_and_sync(interaction)
        await interaction.response.send_message(
            f"✅ تم حفظ بيانات `{domain}` (عدد المفاتيح: {len(data)}) ومزامنتها بنجاح.",
            ephemeral=True,
        )


class RemoveKeyModal(discord.ui.Modal):
    def __init__(self, domain: str):
        super().__init__(title="🗑️ حذف Cookie/Token")
        self.domain_input = discord.ui.TextInput(
            label="الدومين",
            style=discord.TextStyle.short,
            placeholder="asurascans.com",
            required=True,
            max_length=120,
            default=domain,
        )
        self.key_input = discord.ui.TextInput(
            label="اسم المفتاح (Cookie name)",
            style=discord.TextStyle.short,
            placeholder="cf_clearance",
            required=True,
            max_length=64,
        )
        self.add_item(self.domain_input)
        self.add_item(self.key_input)

    async def on_submit(self, interaction: discord.Interaction):
        domain = (
            str(self.domain_input.value)
            .lower()
            .replace("https://", "")
            .replace("http://", "")
            .split("/")[0]
        )
        key = str(self.key_input.value).strip()
        auth = await database.get_site_auth(domain) or {}
        if key not in auth:
            return await interaction.response.send_message(
                "❌ المفتاح غير موجود.", ephemeral=True
            )
        auth.pop(key, None)
        if auth:
            await database.set_site_auth(domain, auth)
        else:
            await database.remove_site_auth(domain)
        await _reload_and_sync(interaction)
        await interaction.response.send_message(
            f"✅ تم حذف `{key}` من `{domain}` ومزامنة التغيير.", ephemeral=True
        )


class ClearDomainModal(discord.ui.Modal):
    def __init__(self, domain: str):
        super().__init__(title="⚠️ مسح كل بيانات دومين")
        self.domain_input = discord.ui.TextInput(
            label="الدومين",
            style=discord.TextStyle.short,
            placeholder="asurascans.com",
            required=True,
            max_length=120,
            default=domain,
        )
        self.confirm = discord.ui.TextInput(
            label="اكتب YES للتأكيد",
            style=discord.TextStyle.short,
            placeholder="YES",
            required=True,
            max_length=10,
        )
        self.add_item(self.domain_input)
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction):
        if str(self.confirm.value).strip().upper() != "YES":
            return await interaction.response.send_message(
                "❌ تم الإلغاء (لازم تكتب YES).", ephemeral=True
            )
        domain = (
            str(self.domain_input.value)
            .lower()
            .replace("https://", "")
            .replace("http://", "")
            .split("/")[0]
        )
        await database.remove_site_auth(domain)
        await _reload_and_sync(interaction)
        await interaction.response.send_message(
            f"🗑️ تم مسح كل بيانات `{domain}` ومزامنتها.", ephemeral=True
        )


class DomainSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="اختر دومين من القائمة",
            min_values=1,
            max_values=1,
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AuthPanelView = self.view  # type: ignore
        view.selected_domain = self.values[0]
        await interaction.response.send_message(
            f"✅ تم اختيار `{view.selected_domain}`. يمكنك الآن استخدام الأزرار أدناه.",
            ephemeral=True,
        )


class AuthPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        domain_options: list[discord.SelectOption],
        default_domain: str = "asurascans.com",
    ):
        super().__init__(timeout=600)
        self.selected_domain = default_domain
        self.add_item(DomainSelect(domain_options))

    def _domain(self) -> str:
        return (self.selected_domain or "asurascans.com").lower()

    @discord.ui.button(
        label="📥 إضافة / استيراد كوكيز", style=discord.ButtonStyle.primary, row=1
    )
    async def import_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ImportAuthModal(self._domain()))

    @discord.ui.button(
        label="🗑️ حذف مفتاح معين", style=discord.ButtonStyle.danger, row=1
    )
    async def remove_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(RemoveKeyModal(self._domain()))

    @discord.ui.button(
        label="📜 عرض المفاتيح", style=discord.ButtonStyle.secondary, row=1
    )
    async def list_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        domain = self._domain()
        auth = await database.get_site_auth(domain) or {}
        keys = list(auth.keys())
        if not keys:
            return await interaction.response.send_message(
                f"لا توجد مفاتيح محفوظة لـ `{domain}`.", ephemeral=True
            )
        view = discord.ui.LayoutView(timeout=60)
        container = discord.ui.Container(accent_color=C_TEAL)
        container.add_item(discord.ui.TextDisplay(f"# 🔐 مفاتيح محفوظة — {domain}"))
        container.add_item(discord.ui.TextDisplay(
            "\n".join(f"• `{k}`" for k in keys)[:3800] + "\n\n-# القيم مخفية للأمان"
        ))
        view.add_item(container)
        await interaction.response.send_message(view=view, ephemeral=True)

    @discord.ui.button(
        label="⚠️ مسح كل بيانات الموقع", style=discord.ButtonStyle.danger, row=2
    )
    async def clear_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ClearDomainModal(self._domain()))

    @discord.ui.button(
        label="🔎 اختبار (Asura فقط)", style=discord.ButtonStyle.success, row=2
    )
    async def test_asura_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ):
        domain = self._domain()
        if domain != "asurascans.com":
            return await interaction.response.send_message(
                "❌ هذا الزر مخصص لاختبار AsuraScans فقط.", ephemeral=True
            )

        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        if not provider_mgr:
            return await interaction.response.send_message(
                "❌ Provider manager غير متوفر.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        chapter_url = "https://asurascans.com/comics/initializing-the-sect-system-7b57f74d/chapter/41"
        try:
            auth = await database.get_site_auth(domain) or {}
            keys_debug = ", ".join(f"{k}" for k in auth.keys()) if auth else "—"

            provider = provider_mgr.get_provider(chapter_url)
            html = provider.fetch_html(chapter_url)
            html_len = len(html or "")
            html_hint = ""
            if html:
                if "locked" in html.lower() or "subscribe" in html.lower():
                    html_hint = " (لا زال مقفلاً)"

            imgs = await provider_mgr.get_images(chapter_url)
            if not imgs:
                await interaction.followup.send(
                    f"❌ فشل الاختبار: لم يتم جلب أي صور.\nالمفاتيح المتاحة: {keys_debug}\nطول الصفحة: {html_len} {html_hint}"
                )
                return

            preview = "\n".join(f"• {u[:100]}..." for u in imgs[:3])
            await interaction.followup.send(
                f"✅ تم الاختبار بنجاح! تم العثور على {len(imgs)} صور.\n{preview}"
            )
        except Exception as e:
            await interaction.followup.send(f"❌ حدث خطأ أثناء الاختبار: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
