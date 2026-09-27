import asyncio
import time
import logging
import discord
import database

# ──────────────────────────────────────────────
# Color Constants
# ──────────────────────────────────────────────
C_BLUE   = discord.Color.from_rgb(88,  101, 242)
C_GREEN  = discord.Color.from_rgb(35,  165,  89)
C_RED    = discord.Color.from_rgb(237,  66,  69)
C_YELLOW = discord.Color.from_rgb(254, 231,  92)


# ──────────────────────────────────────────────
# Layout Helpers
# ──────────────────────────────────────────────

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
    layout = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=colour)
    container.add_item(discord.ui.TextDisplay(f"## 💫 {title}"))
    container.add_item(discord.ui.TextDisplay(progress_bar))
    container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
    container.add_item(discord.ui.TextDisplay(f"**{description}**"))
    layout.add_item(container)
    return layout


def _done_layout(folder_name: str, drive_link: str, pages: int, elapsed: float) -> discord.ui.LayoutView:
    layout = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=C_GREEN)
    container.add_item(discord.ui.TextDisplay(f"## 🎉  اكتمال استخراج النصوص"))
    container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
    container.add_item(discord.ui.TextDisplay(f"📂 **المجلد**: `{folder_name}`\n📄 **الصفحات المعالجة**: `{pages}/{pages}` صفحة"))
    container.add_item(discord.ui.TextDisplay(
        f"⏱️ **الزمن**: `{elapsed:.1f} ثانية`\n"
        f"💎 **الحالة**: تم استخراج وترتيب النصوص وتوليد ملف Word بنجاح."
    ))
    
    if drive_link:
        btn = discord.ui.Button(label="📂 فتح الملف في Drive", style=discord.ButtonStyle.link, url=drive_link, emoji="☁️")
        section = discord.ui.Section(
            discord.ui.TextDisplay("تم رفع المستند إلى Google Drive:"), accessory=btn
        )
        container.add_item(section)
        
    layout.add_item(container)
    return layout


def _error_layout(message: str) -> discord.ui.LayoutView:
    layout = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=C_RED)
    container.add_item(discord.ui.TextDisplay("## ❌ فشل العملية"))
    container.add_item(discord.ui.TextDisplay(message))
    layout.add_item(container)
    return layout


# ──────────────────────────────────────────────
# Dashboard UI
# ──────────────────────────────────────────────

class DashboardUI:
    def __init__(self, interaction: discord.Interaction, chapters: list[dict]):
        self.interaction = interaction
        self.chapters = chapters
        self.statuses = {c['id']: "⚪ في الانتظار" for c in chapters}
        self.progress_bars = {c['id']: "" for c in chapters}
        self.drive_links = {c['id']: None for c in chapters}
        self.is_cancelled = False
        self.start_time = time.time()
        
    def generate_layout(self) -> discord.ui.LayoutView:
        layout = discord.ui.LayoutView(timeout=None)
        
        any_failed = any("❌" in s or "خطأ" in s for s in self.statuses.values())
        all_done = all("✅" in s or "اكتمل" in s for s in self.statuses.values())
        
        if any_failed:
            color = C_RED
        elif all_done:
            color = C_GREEN
        else:
            color = C_BLUE
            
        container = discord.ui.Container(accent_color=color)
        container.add_item(discord.ui.TextDisplay("## 💫 استخراج النصوص"))
        
        elapsed = time.time() - self.start_time
        time_label = "الوقت المستغرق" if all_done else "الوقت المنقضي"
        container.add_item(discord.ui.TextDisplay(f"⏱️ **{time_label}**: `{elapsed:.0f} ثانية`"))
        
        for i, c in enumerate(self.chapters):
            if i > 0:
                container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
            
            bar_desc = f"\n  {self.progress_bars[c['id']]}" if self.progress_bars[c['id']] else ""
            status_text = (
                f"📂 **الفصل**: `{c['name']}`\n"
                f"⚡ **الحالة**: {self.statuses[c['id']]}{bar_desc}"
            )
            container.add_item(discord.ui.TextDisplay(status_text))
            
        layout.add_item(container)
        
        if all_done:
            for c in self.chapters:
                link = self.drive_links.get(c['id'])
                if link:
                    btn_label = f"📂 فتح {c['name'][:20]} في Drive" if len(self.chapters) > 1 else "📂 فتح في Drive"
                    btn = discord.ui.Button(
                        label=btn_label,
                        style=discord.ButtonStyle.link,
                        url=link,
                        emoji="☁️"
                    )
                    layout.add_item(btn)
                    
        return layout
        
    async def update(self, chap_id: str, status: str, bar: str = "", drive_link: str = None):
        if self.is_cancelled: return
        self.statuses[chap_id] = status
        self.progress_bars[chap_id] = bar
        if drive_link:
            self.drive_links[chap_id] = drive_link
        try:
            await self.interaction.edit_original_response(content=None, embed=None, view=self.generate_layout())
        except Exception:
            pass


# ──────────────────────────────────────────────
# Chapter Select View
# ──────────────────────────────────────────────

class ChapterSelectView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, subfolders: list[dict], cog, lang: str):
        super().__init__(timeout=60)
        self.interaction = interaction
        self.subfolders = subfolders
        self.cog = cog
        self.lang = lang
        
        options = []
        for sf in subfolders[:25]: # Discord limit
            options.append(discord.SelectOption(label=sf['name'], value=sf['id']))
            
        self.select = discord.ui.Select(
            placeholder="اختر الفصول المراد استخراجها (يمكنك اختيار أكثر من فصل)",
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
        
        # Start Dashboard
        dashboard = DashboardUI(self.interaction, selected_folders)
        await dashboard.update(selected_folders[0]['id'], "⏳ جاري التجهيز...")
        
        # Fetch active settings
        active_settings = await database.get_active_user_settings(self.interaction.user.id)
        
        # Process in background
        asyncio.create_task(self.cog.process_queue(self.interaction, dashboard, selected_folders, self.lang, active_settings))
        self.stop()


# ──────────────────────────────────────────────
# Interactive User Settings Layout Views
# ──────────────────────────────────────────────

class LegendModal(discord.ui.Modal):
    def __init__(self, key: str, label_name: str, current_val: str, view: 'SettingsView'):
        super().__init__(title=f"تعديل رمز {label_name}")
        self.key = key
        self.view = view
        self.input_field = discord.ui.TextInput(
            label="أدخل الرمز الجديد:",
            default=current_val,
            placeholder="مثال: [ ]",
            max_length=15,
            required=True
        )
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        new_val = self.input_field.value.strip()
        self.view.settings[self.key] = new_val
        await database.save_user_settings(self.view.user_id, self.view.active_profile, self.view.settings)
        # Re-render Legend Settings
        await interaction.followup.edit_message(
            message_id=interaction.message.id,
            view=self.view.generate_legend_layout()
        )


class CreateProfileModal(discord.ui.Modal):
    def __init__(self, view: 'SettingsView'):
        super().__init__(title="إنشاء ملف تعريف جديد")
        self.view = view
        self.input_field = discord.ui.TextInput(
            label="اسم ملف التعريف (رقم أو اسم قصير):",
            placeholder="مثال: 1",
            max_length=10,
            required=True
        )
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        profile_name = self.input_field.value.strip()
        if not profile_name:
            return
            
        success = await database.create_user_profile(self.view.user_id, profile_name)
        if not success:
            await interaction.followup.send("❌ تم الوصول للحد الأقصى لملفات التعريف (4 ملفات مخصصة).", ephemeral=True)
            return
            
        await database.set_active_profile(self.view.user_id, profile_name)
        self.view.active_profile = profile_name
        self.view.profiles = await database.get_user_profiles(self.view.user_id)
        for p in self.view.profiles:
            if p["profile_name"] == profile_name:
                self.view.settings = p
                break
                
        # Re-render main layout
        await interaction.followup.edit_message(
            message_id=interaction.message.id,
            content=f"✅ **تم إنشاء وتحميل ملف التعريف `{profile_name}` بنجاح!**",
            view=self.view.generate_main_layout()
        )


class SettingsView(discord.ui.LayoutView):
    def __init__(self, interaction: discord.Interaction, user_id: int, profiles: list[dict]):
        super().__init__(timeout=180)
        self.interaction = interaction
        self.user_id = user_id
        self.profiles = profiles
        
        # Determine active profile
        self.active_profile = "Default"
        self.settings = profiles[0]
        for p in profiles:
            if p["is_active"]:
                self.active_profile = p["profile_name"]
                self.settings = p
                break

    def generate_main_layout(self) -> discord.ui.LayoutView:
        self.clear_items()
        
        # Container Card
        container = discord.ui.Container(accent_color=C_BLUE)
        container.add_item(discord.ui.TextDisplay("## ⚙️ إعدادات استخراج النصوص (User Settings)"))
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        custom_count = sum(1 for p in self.profiles if p["profile_name"] != "Default")
        desc = (
            f"اختر ملف تعريف لتعديل وتخصيص إعدادات استخراج النصوص الخاصة بك.\n"
            f"📋 **ملفات التعريف المخصصة**: `{custom_count}/4`"
        )
        container.add_item(discord.ui.TextDisplay(desc))
        self.add_item(container)
        
        # Profile Selector Select Dropdown
        options = [
            discord.SelectOption(
                label=f"ملف {p['profile_name']}",
                value=p['profile_name'],
                description="ملف التعريف الافتراضي" if p['profile_name'] == "Default" else "ملف تعريف مخصص",
                emoji="⭐" if p['is_active'] else "📄",
                default=p['is_active']
            )
            for p in self.profiles
        ]
        
        select = discord.ui.Select(
            placeholder="اختر ملف التعريف المراد تحميله",
            options=options,
            id=100
        )
        
        async def on_profile_select(inter: discord.Interaction):
            await inter.response.defer()
            selected = select.values[0]
            await database.set_active_profile(self.user_id, selected)
            self.profiles = await database.get_user_profiles(self.user_id)
            self.active_profile = selected
            for p in self.profiles:
                if p["profile_name"] == selected:
                    self.settings = p
                    break
            # Refresh layout
            await inter.followup.edit_message(
                message_id=inter.message.id,
                content=f"✅ **تم تحميل ملف التعريف `{selected}` بنجاح!**",
                view=self.generate_main_layout()
            )
            
        select.callback = on_profile_select
        self.add_item(select)
        
        # Navigation Buttons
        btn_extract = discord.ui.Button(label="🛠️ Extraction Settings", style=discord.ButtonStyle.secondary, id=101)
        async def go_to_extraction(inter: discord.Interaction):
            await inter.response.defer()
            await inter.followup.edit_message(message_id=inter.message.id, content=None, view=self.generate_extraction_layout())
        btn_extract.callback = go_to_extraction
        self.add_item(btn_extract)
        
        btn_legend = discord.ui.Button(label="✍️ Legend Settings", style=discord.ButtonStyle.secondary, id=102)
        async def go_to_legend(inter: discord.Interaction):
            await inter.response.defer()
            await inter.followup.edit_message(message_id=inter.message.id, content=None, view=self.generate_legend_layout())
        btn_legend.callback = go_to_legend
        self.add_item(btn_legend)
        
        btn_create = discord.ui.Button(label="➕ Create Profile", style=discord.ButtonStyle.success, id=103)
        async def open_create_modal(inter: discord.Interaction):
            modal = CreateProfileModal(self)
            await inter.response.send_modal(modal)
        btn_create.callback = open_create_modal
        self.add_item(btn_create)
        
        return self

    def generate_extraction_layout(self) -> discord.ui.LayoutView:
        self.clear_items()
        
        container = discord.ui.Container(accent_color=C_BLUE)
        container.add_item(discord.ui.TextDisplay(f"## 🛠️ إعدادات الاستخراج لملف: `{self.active_profile}`"))
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        spaces_chk = "✅" if self.settings["add_spaces"] else "❌"
        sfx_chk = "✅" if self.settings["remove_sfx"] else "❌"
        legends_chk = "✅" if self.settings["remove_legends"] else "❌"
        conn_chk = "✅" if self.settings["connected_slashes"] else "❌"
        
        status_text = (
            f"قم بتفعيل أو إلغاء تفعيل الخيارات التالية لملف التعريف الخاص بك:\n\n"
            f"**Add Spaces** (إضافة أسطر فارغة): {spaces_chk}\n"
            f"**Remove SFX** (إزالة المؤثرات الصوتية): {sfx_chk}\n"
            f"**Remove Legends** (إخفاء الرموز والتصنيف): {legends_chk}\n"
            f"**Connected //** (دمج أسطر الفقاعة بـ //): {conn_chk}\n"
            f"**Output Format** (تنسيق الملف): `{self.settings['output_format']}`\n"
            f"**Model Mode** (وضع الموديل): `{self.settings['model_mode']}`"
        )
        container.add_item(discord.ui.TextDisplay(status_text))
        self.add_item(container)
        
        async def toggle_setting(inter: discord.Interaction, key: str):
            await inter.response.defer()
            self.settings[key] = 0 if self.settings[key] else 1
            await database.save_user_settings(self.user_id, self.active_profile, self.settings)
            await inter.followup.edit_message(message_id=inter.message.id, view=self.generate_extraction_layout())
            
        btn_spaces = discord.ui.Button(
            label="Add Spaces", 
            style=discord.ButtonStyle.success if self.settings["add_spaces"] else discord.ButtonStyle.danger,
            id=201
        )
        btn_spaces.callback = lambda inter: toggle_setting(inter, "add_spaces")
        self.add_item(btn_spaces)
        
        btn_sfx = discord.ui.Button(
            label="Remove SFX", 
            style=discord.ButtonStyle.success if self.settings["remove_sfx"] else discord.ButtonStyle.danger,
            id=202
        )
        btn_sfx.callback = lambda inter: toggle_setting(inter, "remove_sfx")
        self.add_item(btn_sfx)
        
        btn_legends = discord.ui.Button(
            label="Remove Legends", 
            style=discord.ButtonStyle.success if self.settings["remove_legends"] else discord.ButtonStyle.danger,
            id=203
        )
        btn_legends.callback = lambda inter: toggle_setting(inter, "remove_legends")
        self.add_item(btn_legends)
        
        btn_conn = discord.ui.Button(
            label="Connected //", 
            style=discord.ButtonStyle.success if self.settings["connected_slashes"] else discord.ButtonStyle.danger,
            id=204
        )
        btn_conn.callback = lambda inter: toggle_setting(inter, "connected_slashes")
        self.add_item(btn_conn)
        
        btn_format = discord.ui.Button(
            label=f"Format: {self.settings['output_format']}",
            style=discord.ButtonStyle.secondary,
            id=205
        )
        async def toggle_format(inter: discord.Interaction):
            await inter.response.defer()
            fmts = ["TXT", "DOCX", "BOTH"]
            curr_idx = fmts.index(self.settings["output_format"])
            next_fmt = fmts[(curr_idx + 1) % len(fmts)]
            self.settings["output_format"] = next_fmt
            await database.save_user_settings(self.user_id, self.active_profile, self.settings)
            await inter.followup.edit_message(message_id=inter.message.id, view=self.generate_extraction_layout())
        btn_format.callback = toggle_format
        self.add_item(btn_format)
        
        btn_mode = discord.ui.Button(
            label=f"Mode: {self.settings['model_mode']}",
            style=discord.ButtonStyle.secondary,
            id=206
        )
        async def toggle_mode(inter: discord.Interaction):
            await inter.response.defer()
            modes = ["ADVANCED", "FAST"]
            curr_idx = modes.index(self.settings["model_mode"])
            next_mode = modes[(curr_idx + 1) % len(modes)]
            self.settings["model_mode"] = next_mode
            await database.save_user_settings(self.user_id, self.active_profile, self.settings)
            await inter.followup.edit_message(message_id=inter.message.id, view=self.generate_extraction_layout())
        btn_mode.callback = toggle_mode
        self.add_item(btn_mode)
        
        btn_back = discord.ui.Button(label="⬅️ Back", style=discord.ButtonStyle.primary, id=207)
        async def go_back(inter: discord.Interaction):
            await inter.response.defer()
            self.profiles = await database.get_user_profiles(self.user_id)
            await inter.followup.edit_message(message_id=inter.message.id, content=None, view=self.generate_main_layout())
        btn_back.callback = go_back
        self.add_item(btn_back)
        
        return self

    def generate_legend_layout(self) -> discord.ui.LayoutView:
        self.clear_items()
        
        container = discord.ui.Container(accent_color=C_BLUE)
        container.add_item(discord.ui.TextDisplay(f"## ✍️ رموز تصنيف الفقاعات للملف: `{self.active_profile}`"))
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        
        symbols_text = (
            f"اضغط على الأزرار لتخصيص رموز التصنيف المكتوبة في بداية الجمل:\n\n"
            f"**Speech Bubble** (كلام عادي): `{self.settings['legend_speech']}`\n"
            f"**Shouting Bubble** (صراخ): `{self.settings['legend_shouting']}`\n"
            f"**Small Text** (نص جانبي/أصوات): `{self.settings['legend_small']}`\n"
            f"**Thinking Bubble** (تفكير): `{self.settings['legend_thinking']}`\n"
            f"**Box Bubble** (صناديق/سرد): `{self.settings['legend_box']}`\n"
            f"**System/UI** (نظام/لعبة): `{self.settings['legend_system']}`\n"
            f"**Outer Text** (نص حر بالخارج): `{self.settings['legend_outer']}`\n"
            f"**SFX** (مؤثرات صوتية): `{self.settings['legend_sfx']}`"
        )
        container.add_item(discord.ui.TextDisplay(symbols_text))
        self.add_item(container)
        
        categories = [
            ("legend_speech", "Speech Bubble"),
            ("legend_shouting", "Shouting Bubble"),
            ("legend_small", "Small Text"),
            ("legend_thinking", "Thinking Bubble"),
            ("legend_box", "Box Bubble"),
            ("legend_system", "System/UI"),
            ("legend_outer", "Outer Text"),
            ("legend_sfx", "SFX")
        ]
        
        for idx, (key, label) in enumerate(categories, start=301):
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, id=idx)
            def make_callback(k=key, l=label):
                async def edit_legend(inter: discord.Interaction):
                    modal = LegendModal(k, l, self.settings[k], self)
                    await inter.response.send_modal(modal)
                return edit_legend
            btn.callback = make_callback()
            self.add_item(btn)
            
        btn_reset = discord.ui.Button(label="🔄 Reset Defaults", style=discord.ButtonStyle.danger, id=400)
        async def reset_defaults(inter: discord.Interaction):
            await inter.response.defer()
            defaults = {
                "legend_speech": '""',
                "legend_shouting": '::',
                "legend_small": 'ST',
                "legend_thinking": '()',
                "legend_box": '[]',
                "legend_system": '<>',
                "legend_outer": 'OT',
                "legend_sfx": 'SFX',
            }
            for k, v in defaults.items():
                self.settings[k] = v
            await database.save_user_settings(self.user_id, self.active_profile, self.settings)
            await inter.followup.edit_message(message_id=inter.message.id, view=self.generate_legend_layout())
        btn_reset.callback = reset_defaults
        self.add_item(btn_reset)
        
        btn_back = discord.ui.Button(label="⬅️ Back", style=discord.ButtonStyle.primary, id=401)
        async def go_back(inter: discord.Interaction):
            await inter.response.defer()
            self.profiles = await database.get_user_profiles(self.user_id)
            await inter.followup.edit_message(message_id=inter.message.id, content=None, view=self.generate_main_layout())
        btn_back.callback = go_back
        self.add_item(btn_back)
        
        return self
